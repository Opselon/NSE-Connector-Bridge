"""Accounting source-data labelling (spec §9, gap §2.9 / §5 Phase C3).

Raw broker values ONLY — there is no local accounting engine here:
no P/L aggregation, no sums, no statistics, no equity curves, no
reports. This module does two things:

1. **Provenance labelling.** Every accounting value carries where it
   came from: ``mt5_supplied`` (read verbatim from an MT5 record),
   ``locally_derived`` (a local mapping/reconstruction over broker
   values — the underlying numbers are still the broker's), or
   ``not_reported`` (the broker did not supply this value in this
   context). The one correction to the gap analysis worth recording:
   ``margin_level`` IS broker-supplied — ``mt5.account_info()``
   exposes it and the Bridge reads it verbatim
   (:func:`nse_bridge.mt5.types.build_account_model`); the local
   ``get_account_margin_level`` command only formats it.

2. **Balance-operation surfacing.** Deposits, withdrawals, balance
   adjustments, credits and fees are explicit MT5 deal types
   (``DEAL_TYPE_LABELS`` in :mod:`nse_bridge.mt5.types`); they are
   surfaced as classified raw deal records, never summed.

Wire note (dependency D8): CONTRACT v1 §3.3 defines no dedicated
accounting kind, so the labelling rides inside the existing batches —
``provenance`` on the ``account`` items and on each history page, plus
``balance_operations`` on ``history_orders`` deal pages (see
:mod:`nse_bridge.server.sync`). No new sync kind is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..mt5.types import DEAL_ENTRY_LABELS, DEAL_TYPE_LABELS

__all__ = [
    "ACCOUNT_FIELD_PROVENANCE",
    "LOCALLY_DERIVED",
    "MT5_SUPPLIED",
    "NOT_REPORTED",
    "LabelledValue",
    "classify_deal",
    "label_account_snapshot",
    "label_closed_position",
    "label_deal",
    "surface_balance_operations",
]

MT5_SUPPLIED = "mt5_supplied"
LOCALLY_DERIVED = "locally_derived"
NOT_REPORTED = "not_reported"

#: Provenance of each ``account``-batch field. All of these come from
#: one ``mt5.account_info()`` record, read verbatim — except ``type``,
#: which is the Bridge's local label (``demo``/``contest``/``live``)
#: over the broker's integer ``trade_mode``.
ACCOUNT_FIELD_PROVENANCE: dict[str, str] = {
    "login": MT5_SUPPLIED,
    "server": MT5_SUPPLIED,
    "name": MT5_SUPPLIED,
    "type": LOCALLY_DERIVED,
    "currency": MT5_SUPPLIED,
    "balance": MT5_SUPPLIED,
    "equity": MT5_SUPPLIED,
    "margin": MT5_SUPPLIED,
    "margin_free": MT5_SUPPLIED,
    "margin_level": MT5_SUPPLIED,
    "leverage": MT5_SUPPLIED,
    "profit": MT5_SUPPLIED,  # floating P/L of open positions
    "swap": MT5_SUPPLIED,
    "commission": MT5_SUPPLIED,
    "credit": MT5_SUPPLIED,
    # Realized P/L is NOT in the account snapshot: it lives in deal
    # history (closed positions / deals). It is never computed here.
    "realized_profit": NOT_REPORTED,
}

#: MT5 deal ``type`` -> balance-operation kind. Types 0/1 are trades,
#: not balance operations. BALANCE (2) splits by the sign of its
#: ``profit``: positive is a deposit, negative a withdrawal, exactly
#: zero a balance adjustment. COMMISSION_* (7-12) and CHARGE (4) are
#: fees; CREDIT (3) is credit; CORRECTION (5), BONUS (6) and
#: DEAL_SPLIT (13) are balance adjustments.
_BALANCE_KINDS: dict[int, str] = {
    3: "credit",
    4: "fee",
    5: "balance_adjustment",
    6: "balance_adjustment",
    7: "fee",
    8: "fee",
    9: "fee",
    10: "fee",
    11: "fee",
    12: "fee",
    13: "balance_adjustment",
}


@dataclass
class LabelledValue:
    """One accounting value with its provenance (spec §9)."""

    value: Any
    provenance: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "provenance": self.provenance}
        if self.note is not None:
            out["note"] = self.note
        return out


def label_account_snapshot(account: dict[str, Any]) -> dict[str, LabelledValue]:
    """Label every field of an ``account``-batch ``account`` object.

    Only fields present in ``account`` are labelled (nothing is
    invented); ``realized_profit`` is always reported as
    ``not_reported`` — it is not part of the snapshot. ``type`` is the
    local ``trade_mode`` label; ``server_timestamp`` is the connector's
    own receipt time (locally derived, not broker time).
    """
    labelled: dict[str, LabelledValue] = {}
    for key, value in account.items():
        provenance = ACCOUNT_FIELD_PROVENANCE.get(key)
        if provenance is None:
            continue  # unknown field: never label what we do not know
        note = None
        if key == "type":
            note = "local label over the broker's integer trade_mode"
        elif key == "profit":
            note = "floating P/L of open positions (broker value)"
        labelled[key] = LabelledValue(value=value, provenance=provenance, note=note)
    labelled["realized_profit"] = LabelledValue(
        value=None,
        provenance=NOT_REPORTED,
        note="not in the account snapshot; see deal history (closed positions / deals)",
    )
    return labelled


def label_deal(deal: dict[str, Any]) -> dict[str, str]:
    """Per-field provenance of one deal record (model-dict shape).

    Money/identity fields are broker-supplied; ``*_label`` fields are
    the Bridge's local label mapping over broker integer codes.
    """
    provenance: dict[str, str] = {}
    for key in (
        "ticket",
        "order",
        "position_id",
        "symbol",
        "type",
        "entry",
        "volume",
        "price",
        "profit",
        "commission",
        "swap",
        "fee",
        "magic",
        "time",
        "time_utc",
        "reason",
    ):
        if key in deal:
            provenance[key] = MT5_SUPPLIED
    for key in ("type_label", "entry_label"):
        if key in deal:
            provenance[key] = LOCALLY_DERIVED
    return provenance


def label_closed_position(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Provenance of one closed-position summary.

    The summary is RECONSTRUCTED locally from MT5 history deals
    (``get_trading_history_positions`` groups deals by ``position_id``),
    so its volumes and money totals are ``locally_derived`` — the
    underlying deal values are broker-supplied, but the grouping and
    the resulting totals are local work. The server must treat the
    deals, not the summary, as the authoritative raw facts. The map
    describes the summary shape, so it does not depend on which
    summary is passed (``summary`` is accepted for call-site clarity
    and may be omitted).
    """
    return {
        "positions": LOCALLY_DERIVED,
        "positions_source": "reconstructed locally from MT5-supplied history deals",
        "position_id": MT5_SUPPLIED,
        "symbol": MT5_SUPPLIED,
        "open_time_utc": MT5_SUPPLIED,
        "close_time_utc": MT5_SUPPLIED,
    }


def classify_deal(deal: dict[str, Any]) -> dict[str, Any]:
    """Classify one deal record as a trade or a balance operation.

    Returns the raw broker values (deal id, amount = broker ``profit``,
    commission/swap/fee, time) plus the classification. Trade deals
    (BUY/SELL) classify as ``trade``; every other MT5 deal type
    classifies as a balance operation. Unknown deal types classify as
    ``balance_adjustment`` with the raw type preserved — never dropped,
    never guessed as a trade.
    """
    deal_type = deal.get("type")
    try:
        type_int = int(deal_type) if deal_type is not None else -1
    except (TypeError, ValueError):
        type_int = -1
    amount = deal.get("profit")
    if type_int in (0, 1):
        kind = "trade"
    elif type_int == 2:  # BALANCE: split by sign of the broker amount
        try:
            signed = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            signed = 0.0
        kind = "deposit" if signed > 0 else ("withdrawal" if signed < 0 else "balance_adjustment")
    else:
        kind = _BALANCE_KINDS.get(type_int, "balance_adjustment")
    return {
        "kind": kind,
        "deal_id": deal.get("ticket"),
        "order_id": deal.get("order"),
        "position_id": deal.get("position_id"),
        "symbol": deal.get("symbol"),
        "amount": amount,
        "commission": deal.get("commission"),
        "swap": deal.get("swap"),
        "fee": deal.get("fee"),
        "currency": None,  # the deal record carries no currency; the account currency applies
        "time": deal.get("time"),
        "time_utc": deal.get("time_utc"),
        "entry": deal.get("entry"),
        "entry_label": DEAL_ENTRY_LABELS.get(deal.get("entry", -1)),
        "deal_type": type_int,
        "deal_type_label": DEAL_TYPE_LABELS.get(type_int),
        "comment": deal.get("comment"),
        "provenance": MT5_SUPPLIED,
    }


def surface_balance_operations(deals: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Explicit deposit/withdrawal/adjustment/fee/credit surfacing.

    Classifies each deal and groups the NON-TRADE ones by kind. Trade
    deals are excluded (they are journal/deal facts, not balance
    operations). Raw broker values only — no sums, no totals, no
    statistics. An empty input yields empty lists, never None.
    """
    out: dict[str, list[dict[str, Any]]] = {
        "deposits": [],
        "withdrawals": [],
        "balance_adjustments": [],
        "fees": [],
        "credits": [],
    }
    key_by_kind = {
        "deposit": "deposits",
        "withdrawal": "withdrawals",
        "balance_adjustment": "balance_adjustments",
        "fee": "fees",
        "credit": "credits",
    }
    for deal in deals:
        classified = classify_deal(deal)
        bucket = key_by_kind.get(str(classified["kind"]))
        if bucket is not None:
            out[bucket].append(classified)
    return out
