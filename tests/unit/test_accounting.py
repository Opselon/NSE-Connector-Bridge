"""Unit tests for accounting source-data labelling (spec §9, gap §2.9 / §5 C3).

Label: unit. No MT5, no network. These tests assert LABELLING and
surfacing only — there is no local accounting engine, so nothing here
sums, aggregates, or reports.
"""

from __future__ import annotations

from typing import Any

import pytest

from nse_bridge.journal.accounting import (
    LOCALLY_DERIVED,
    MT5_SUPPLIED,
    NOT_REPORTED,
    classify_deal,
    label_account_snapshot,
    label_closed_position,
    label_deal,
    surface_balance_operations,
)

pytestmark = pytest.mark.unit


def _deal(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ticket": 201,
        "order": 55,
        "position_id": 7,
        "symbol": "XAUUSD.a",
        "type": 0,
        "entry": 0,
        "volume": 0.1,
        "price": 2650.5,
        "profit": 12.5,
        "commission": -1.5,
        "swap": 0.0,
        "fee": 0.0,
        "magic": 123,
        "time": 1763280000,
        "time_utc": "2026-11-16T10:00:00",
    }
    record.update(overrides)
    return record


def test_account_snapshot_labels_every_field_mt5_supplied():
    account = {
        "login": "123456",
        "server": "Broker-Demo",
        "name": "Trader",
        "type": "demo",
        "currency": "USD",
        "balance": 10000.0,
        "equity": 10012.5,
        "margin": 100.0,
        "margin_free": 9912.5,
        "profit": 12.5,
        "credit": 0.0,
    }
    labelled = label_account_snapshot(account)
    for key in (
        "login",
        "server",
        "name",
        "currency",
        "balance",
        "equity",
        "margin",
        "margin_free",
        "profit",
        "credit",
    ):
        assert labelled[key].provenance == MT5_SUPPLIED, key
        assert labelled[key].value == account[key]
    # margin_level is broker-supplied (mt5.account_info() exposes it);
    # the local command only formats it.
    assert label_account_snapshot({**account, "margin_level": 10012.5})["margin_level"].provenance == MT5_SUPPLIED
    # "type" is the Bridge's local label over the broker's trade_mode int.
    assert labelled["type"].provenance == LOCALLY_DERIVED
    # Realized P/L is never in the snapshot — it lives in deal history.
    assert labelled["realized_profit"].provenance == NOT_REPORTED
    assert labelled["realized_profit"].value is None
    # Unknown fields are never labelled (nothing invented).
    assert "surprise_field" not in label_account_snapshot({**account, "surprise_field": 1})


def test_deal_provenance_money_fields_supplied_labels_derived():
    provenance = label_deal(_deal(type_label="BUY", entry_label="IN"))
    for key in ("ticket", "volume", "price", "profit", "commission", "swap", "fee", "magic", "time"):
        assert provenance[key] == MT5_SUPPLIED, key
    assert provenance["type_label"] == LOCALLY_DERIVED
    assert provenance["entry_label"] == LOCALLY_DERIVED


def test_closed_position_summary_is_locally_derived():
    provenance = label_closed_position({"position_id": 7, "volume_in": 0.2, "volume_out": 0.2})
    assert provenance["positions"] == LOCALLY_DERIVED
    assert "MT5-supplied" in provenance["positions_source"]


def test_classify_deal_balance_split_by_sign():
    assert classify_deal(_deal(type=2, profit=500.0))["kind"] == "deposit"
    assert classify_deal(_deal(type=2, profit=-200.0))["kind"] == "withdrawal"
    assert classify_deal(_deal(type=2, profit=0.0))["kind"] == "balance_adjustment"
    assert classify_deal(_deal(type=3, profit=100.0))["kind"] == "credit"
    assert classify_deal(_deal(type=5, profit=1.0))["kind"] == "balance_adjustment"
    assert classify_deal(_deal(type=6, profit=50.0))["kind"] == "balance_adjustment"
    assert classify_deal(_deal(type=13, profit=0.0))["kind"] == "balance_adjustment"
    for fee_type in (4, 7, 8, 9, 10, 11, 12):
        assert classify_deal(_deal(type=fee_type, profit=-5.0))["kind"] == "fee"
    # Trades stay trades; unknown types never become trades.
    assert classify_deal(_deal(type=0))["kind"] == "trade"
    assert classify_deal(_deal(type=1))["kind"] == "trade"
    assert classify_deal(_deal(type=99, profit=1.0))["kind"] == "balance_adjustment"


def test_classify_deal_keeps_raw_broker_values():
    classified = classify_deal(_deal(type=2, profit=500.0, commission=-2.0))
    assert classified["amount"] == 500.0
    assert classified["deal_id"] == 201
    assert classified["commission"] == -2.0
    assert classified["time_utc"] == "2026-11-16T10:00:00"
    assert classified["provenance"] == MT5_SUPPLIED
    assert classified["deal_type_label"] == "BALANCE"


def test_surface_balance_operations_groups_raw_deals_without_sums():
    deals = [
        _deal(ticket=301, type=2, profit=500.0),
        _deal(ticket=302, type=2, profit=-200.0),
        _deal(ticket=303, type=2, profit=0.0),
        _deal(ticket=304, type=4, profit=-5.0),
        _deal(ticket=305, type=3, profit=100.0),
        _deal(ticket=306, type=0, profit=12.5),  # a trade: excluded
    ]
    surfaced = surface_balance_operations(deals)
    assert [d["deal_id"] for d in surfaced["deposits"]] == [301]
    assert [d["deal_id"] for d in surfaced["withdrawals"]] == [302]
    assert [d["deal_id"] for d in surfaced["balance_adjustments"]] == [303]
    assert [d["deal_id"] for d in surfaced["fees"]] == [304]
    assert [d["deal_id"] for d in surfaced["credits"]] == [305]
    # No totals anywhere: the Bridge does not aggregate accounting data.
    assert "total" not in str(surfaced).lower()
    assert surface_balance_operations([]) == {
        "deposits": [],
        "withdrawals": [],
        "balance_adjustments": [],
        "fees": [],
        "credits": [],
    }
