"""Broker-provided symbol metadata (spec §4).

Every field is a broker-reported value, copied verbatim from the MT5
``SymbolInfo`` record. A field the broker/terminal does not provide is
``None`` — the Bridge NEVER invents a value (no default contract size,
no guessed tick value, no assumed session schedule).

Per-field MT5 source (see :attr:`SymbolMetadata.MT5_FIELD_MAP`):

* ``trade_exemode`` is the EXECUTION mode (request/instant/market/
  exchange). ``trade_calc_mode`` is the CALCULATION mode
  (forex/futures/CFD/…). They are different properties — do not
  conflate them. (Note: the pre-existing ``SymbolInfoModel`` in
  ``nse_bridge/mt5/types.py`` maps ``trade_execution`` from
  ``trade_calc_mode``; that model is out of scope for this package and
  is NOT used here. ``SymbolMetadata.from_mt5`` reads both properties
  separately.)
* Hourly quote/trade session SCHEDULES are not exposed by the MT5
  Python API. What the terminal does expose is live session
  AGGREGATES (deals, turnover, open/close prices); those are carried
  in :attr:`SymbolMetadata.session_stats`. ``session_schedule`` is
  therefore always ``None`` — "not exposed by the API", never a
  fabricated timetable.
* ``start_time`` / ``expiration_time`` are broker-local epoch seconds
  (0 in MT5 means "none"); they are carried verbatim, never
  re-localized to UTC.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..mt5.types import EXECUTION_LABELS, TRADE_MODE_LABELS
from ..mt5.types import f as _mt5_field

#: Calculation-mode labels (MT5 ``trade_calc_mode``).
#: Raw int is authoritative; the label is display-only.
CALC_MODE_LABELS = {
    0: "FOREX",
    1: "FOREX_NO_LEVERAGE",
    2: "FUTURES",
    3: "CFD",
    4: "CFD_LEVERAGE",
}

#: Swap-mode labels (MT5 ``swap_mode``). Raw int is authoritative.
SWAP_MODE_LABELS = {
    0: "DISABLED",
    1: "POINTS",
    2: "SYMBOL_CURRENCY",
    3: "MARGIN_CURRENCY",
    4: "CLIENT_CURRENCY",
    5: "REOPEN",
    6: "REOPEN_BY_BID",
}

#: Order-GTC-mode labels (MT5 ``order_gtc_mode``). Raw int is authoritative.
GTC_MODE_LABELS = {
    0: "GTC",
    1: "DAILY",
    2: "DAILY_NO_STOPS",
}

#: Filling-mode bitmask bits (MT5 ``filling_mode``): 1 = FOK, 2 = IOC.
#: A zero bitmask means the broker accepts RETURN (fill-or-kill is not
#: requested) — the same convention as ``engine/common.py``.
FILLING_FOK = 1
FILLING_IOC = 2

#: Trade-mode value that means "trading disabled" (MT5 ``trade_mode``).
TRADE_MODE_DISABLED = 0
#: Trade-mode value that means "fully tradable" (MT5 ``trade_mode``).
TRADE_MODE_FULL = 4


def filling_mode_labels(bitmask: int | None) -> list[str]:
    """Human-readable filling modes for an MT5 ``filling_mode`` bitmask."""
    if bitmask is None:
        return []
    modes: list[str] = []
    if bitmask & FILLING_FOK:
        modes.append("FOK")
    if bitmask & FILLING_IOC:
        modes.append("IOC")
    return modes or ["RETURN"]


@dataclass(frozen=True)
class SymbolMetadata:
    """All broker-provided properties of one symbol (spec §4).

    ``name`` is the exact broker symbol name (e.g. ``"XAUUSD.a"``) —
    the string the C2 execution agent must send to MT5 verbatim.
    Every other field is ``None`` when the broker does not report it.
    """

    # -- identity -----------------------------------------------------
    name: str
    description: str | None = None
    #: Market Watch tree path/group (e.g. ``"Metals\\XAUUSD.a"``).
    path: str | None = None
    #: True when the symbol is visible in Market Watch.
    visible: bool | None = None
    #: True when the symbol is selected in Market Watch.
    selected: bool | None = None

    # -- currencies ---------------------------------------------------
    currency_base: str | None = None
    currency_profit: str | None = None
    currency_margin: str | None = None

    # -- pricing ------------------------------------------------------
    digits: int | None = None
    point: float | None = None
    #: Minimum price increment (``trade_tick_size``).
    tick_size: float | None = None
    #: Tick value in profit currency (``trade_tick_value``).
    tick_value: float | None = None
    #: Tick value for a losing position (``trade_tick_value_loss``).
    tick_value_loss: float | None = None
    #: Contract size (``trade_contract_size``).
    contract_size: float | None = None

    # -- volumes ------------------------------------------------------
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    volume_limit: float | None = None

    # -- stops / freeze ----------------------------------------------
    stop_level: int | None = None
    freeze_level: int | None = None

    # -- execution ----------------------------------------------------
    #: Execution mode (MT5 ``trade_exemode``): 0 request, 1 instant,
    #: 2 market, 3 exchange.
    execution_mode: int | None = None
    execution_mode_label: str | None = None
    #: Filling-mode bitmask (MT5 ``filling_mode``): 1 FOK, 2 IOC.
    filling_mode: int | None = None
    #: Human-readable filling modes derived from :attr:`filling_mode`.
    filling_modes: list[str] = field(default_factory=list)
    #: Trade mode (MT5 ``trade_mode``): 0 disabled, 1 long-only,
    #: 2 short-only, 3 close-only, 4 full.
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    #: Calculation mode (MT5 ``trade_calc_mode``): 0 forex, 1 forex
    #: (no leverage), 2 futures, 3 CFD, 4 CFD with leverage.
    calculation_mode: int | None = None
    calculation_mode_label: str | None = None

    # -- swaps ---------------------------------------------------------
    swap_mode: int | None = None
    swap_mode_label: str | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    #: Weekday (0=Sunday … 6=Saturday) on which triple swap is charged.
    swap_rollover3days: int | None = None

    # -- trading sessions ---------------------------------------------
    #: Hourly quote/trade session schedule. ALWAYS ``None``: the MT5
    #: Python API does not expose session schedules (see module
    #: docstring). Live session aggregates are in ``session_stats``.
    session_schedule: None = None
    #: Live session aggregates the terminal reports (session_deals,
    #: session_turnover, session_open/close, …). Empty when the broker
    #: reports none. These are aggregates, NOT a schedule.
    session_stats: dict[str, Any] = field(default_factory=dict)

    # -- expiration ---------------------------------------------------
    #: Broker-local epoch seconds the symbol starts trading (0 = none).
    start_time: int | None = None
    #: Broker-local epoch seconds the symbol expires (0 = none).
    expiration_time: int | None = None

    # -- spread / margin ----------------------------------------------
    #: Broker-reported spread in points (``spread``).
    spread: int | None = None
    #: Broker-reported spread as a float (``spread_float``).
    spread_float: float | None = None
    margin_initial: float | None = None
    margin_maintenance: float | None = None
    margin_hedged: float | None = None

    # -- order modes ---------------------------------------------------
    order_gtc_mode: int | None = None
    order_gtc_mode_label: str | None = None

    #: MT5 SymbolInfo property feeding each field. ``None`` means the
    #: field has no MT5 source (``session_schedule`` is unexposed by
    #: the API; ``filling_modes`` / labels are derived locally).
    MT5_FIELD_MAP: ClassVar[Mapping[str, str | None]] = {
        "name": "name",
        "description": "description",
        "path": "path",
        "visible": "visible",
        "selected": "selected",
        "currency_base": "currency_base",
        "currency_profit": "currency_profit",
        "currency_margin": "currency_margin",
        "digits": "digits",
        "point": "point",
        "tick_size": "trade_tick_size",
        "tick_value": "trade_tick_value",
        "tick_value_loss": "trade_tick_value_loss",
        "contract_size": "trade_contract_size",
        "volume_min": "volume_min",
        "volume_max": "volume_max",
        "volume_step": "volume_step",
        "volume_limit": "volume_limit",
        "stop_level": "trade_stops_level",
        "freeze_level": "trade_freeze_level",
        "execution_mode": "trade_exemode",
        "execution_mode_label": "trade_exemode (label)",
        "filling_mode": "filling_mode",
        "filling_modes": None,
        "trade_mode": "trade_mode",
        "trade_mode_label": "trade_mode (label)",
        "calculation_mode": "trade_calc_mode",
        "calculation_mode_label": "trade_calc_mode (label)",
        "swap_mode": "swap_mode",
        "swap_mode_label": "swap_mode (label)",
        "swap_long": "swap_long",
        "swap_short": "swap_short",
        "swap_rollover3days": "swap_rollover3days",
        "session_schedule": None,
        "session_stats": "session_* aggregates",
        "start_time": "start_time",
        "expiration_time": "expiration_time",
        "spread": "spread",
        "spread_float": "spread_float",
        "margin_initial": "margin_initial",
        "margin_maintenance": "margin_maintenance",
        "margin_hedged": "margin_hedged",
        "order_gtc_mode": "order_gtc_mode",
        "order_gtc_mode_label": "order_gtc_mode (label)",
    }

    # -- construction ---------------------------------------------------
    @classmethod
    def from_mt5(cls, raw: Any) -> SymbolMetadata:
        """Build metadata from one MT5 ``SymbolInfo`` record.

        ``raw`` may be a namedtuple, a dict, or any object the
        ``mt5.types.f`` accessor understands. Missing/``None`` MT5
        properties become ``None`` fields — never invented values.
        """
        trade_mode = _mt5_field(raw, "trade_mode")
        execution_mode = _mt5_field(raw, "trade_exemode")
        calculation_mode = _mt5_field(raw, "trade_calc_mode")
        filling_mode = _mt5_field(raw, "filling_mode")
        swap_mode = _mt5_field(raw, "swap_mode")
        gtc_mode = _mt5_field(raw, "order_gtc_mode")
        session_stats = {
            key: _mt5_field(raw, key)
            for key in (
                "session_deals",
                "session_buy_orders",
                "session_sell_orders",
                "session_turnover",
                "session_interest",
                "session_buy_order_volume",
                "session_sell_order_volume",
                "session_open",
                "session_close",
                "session_aw",
                "session_price_settlement",
                "session_volume",
            )
        }
        session_stats = {k: v for k, v in session_stats.items() if v is not None}
        return cls(
            name=str(_mt5_field(raw, "name", "") or ""),
            description=_mt5_field(raw, "description"),
            path=_mt5_field(raw, "path"),
            visible=_mt5_field(raw, "visible"),
            selected=_mt5_field(raw, "selected"),
            currency_base=_mt5_field(raw, "currency_base"),
            currency_profit=_mt5_field(raw, "currency_profit"),
            currency_margin=_mt5_field(raw, "currency_margin"),
            digits=_mt5_field(raw, "digits"),
            point=_mt5_field(raw, "point"),
            tick_size=_mt5_field(raw, "trade_tick_size"),
            tick_value=_mt5_field(raw, "trade_tick_value"),
            tick_value_loss=_mt5_field(raw, "trade_tick_value_loss"),
            contract_size=_mt5_field(raw, "trade_contract_size"),
            volume_min=_mt5_field(raw, "volume_min"),
            volume_max=_mt5_field(raw, "volume_max"),
            volume_step=_mt5_field(raw, "volume_step"),
            volume_limit=_mt5_field(raw, "volume_limit"),
            stop_level=_mt5_field(raw, "trade_stops_level"),
            freeze_level=_mt5_field(raw, "trade_freeze_level"),
            execution_mode=execution_mode,
            execution_mode_label=EXECUTION_LABELS.get(execution_mode, None) if execution_mode is not None else None,
            filling_mode=filling_mode,
            filling_modes=filling_mode_labels(filling_mode),
            trade_mode=trade_mode,
            trade_mode_label=TRADE_MODE_LABELS.get(trade_mode, None) if trade_mode is not None else None,
            calculation_mode=calculation_mode,
            calculation_mode_label=CALC_MODE_LABELS.get(calculation_mode, None)
            if calculation_mode is not None
            else None,
            swap_mode=swap_mode,
            swap_mode_label=SWAP_MODE_LABELS.get(swap_mode, None) if swap_mode is not None else None,
            swap_long=_mt5_field(raw, "swap_long"),
            swap_short=_mt5_field(raw, "swap_short"),
            swap_rollover3days=_mt5_field(raw, "swap_rollover3days"),
            session_stats=session_stats,
            start_time=_mt5_field(raw, "start_time"),
            expiration_time=_mt5_field(raw, "expiration_time"),
            spread=_mt5_field(raw, "spread"),
            spread_float=_mt5_field(raw, "spread_float"),
            margin_initial=_mt5_field(raw, "margin_initial"),
            margin_maintenance=_mt5_field(raw, "margin_maintenance"),
            margin_hedged=_mt5_field(raw, "margin_hedged"),
            order_gtc_mode=gtc_mode,
            order_gtc_mode_label=GTC_MODE_LABELS.get(gtc_mode, None) if gtc_mode is not None else None,
        )

    # -- derived state --------------------------------------------------
    @property
    def is_trade_enabled(self) -> bool:
        """True unless the broker reports trading disabled (mode 0).

        Long-only / short-only / close-only are *restricted*, not
        disabled — they still accept some orders. ``None`` trade mode
        (broker did not report one) is treated as NOT enabled: the
        Bridge never assumes tradability it cannot see.
        """
        return self.trade_mode is not None and self.trade_mode != TRADE_MODE_DISABLED

    @property
    def is_fully_tradable(self) -> bool:
        """True only for trade mode FULL (4) — no direction restriction."""
        return self.trade_mode == TRADE_MODE_FULL

    def distinguishing_signature(self) -> tuple[Any, ...]:
        """Metadata tuple used to tell two same-named candidates apart.

        Fields the spec §4 ambiguity rule names: contract size, tick
        value, sessions, digits, trade mode. Session *schedules* are
        unexposed by the MT5 API, so they cannot take part — the
        signature uses the fields that are actually available, plus
        tick size and currencies (a candidate whose profit currency
        differs is a different instrument). Two candidates with equal
        signatures are INDISTINGUISHABLE on available metadata and
        must be reported ambiguous, never guessed between.
        """
        return (
            self.contract_size,
            self.tick_value,
            self.tick_size,
            self.digits,
            self.currency_base,
            self.currency_profit,
            self.trade_mode,
            self.calculation_mode,
        )

    def matches_expected(self, expected: Mapping[str, Any]) -> bool:
        """True when every ``expected`` field equals this metadata.

        Keys must be :class:`SymbolMetadata` field names — an unknown
        key raises :class:`ValueError` instead of being silently
        ignored (a typo in a disambiguation constraint must fail
        loudly, not resolve to the wrong symbol). Numeric values are
        compared with ``math.isclose`` (relative tolerance 1e-9);
        ``None`` in ``expected`` requires the field to be ``None``.
        """
        for key, want in expected.items():
            if key not in self.__dataclass_fields__:
                raise ValueError(f"unknown SymbolMetadata field for disambiguation: {key!r}")
            got = getattr(self, key)
            if want is None or got is None:
                if want is not None or got is not None:
                    return False
                continue
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                if not math.isclose(float(got), float(want), rel_tol=1e-9, abs_tol=1e-12):
                    return False
            elif got != want:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form (all fields; ``None`` preserved, never dropped)."""
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "visible": self.visible,
            "selected": self.selected,
            "currency_base": self.currency_base,
            "currency_profit": self.currency_profit,
            "currency_margin": self.currency_margin,
            "digits": self.digits,
            "point": self.point,
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
            "tick_value_loss": self.tick_value_loss,
            "contract_size": self.contract_size,
            "volume_min": self.volume_min,
            "volume_max": self.volume_max,
            "volume_step": self.volume_step,
            "volume_limit": self.volume_limit,
            "stop_level": self.stop_level,
            "freeze_level": self.freeze_level,
            "execution_mode": self.execution_mode,
            "execution_mode_label": self.execution_mode_label,
            "filling_mode": self.filling_mode,
            "filling_modes": list(self.filling_modes),
            "trade_mode": self.trade_mode,
            "trade_mode_label": self.trade_mode_label,
            "calculation_mode": self.calculation_mode,
            "calculation_mode_label": self.calculation_mode_label,
            "swap_mode": self.swap_mode,
            "swap_mode_label": self.swap_mode_label,
            "swap_long": self.swap_long,
            "swap_short": self.swap_short,
            "swap_rollover3days": self.swap_rollover3days,
            "session_schedule": None,
            "session_stats": dict(self.session_stats),
            "start_time": self.start_time,
            "expiration_time": self.expiration_time,
            "spread": self.spread,
            "spread_float": self.spread_float,
            "margin_initial": self.margin_initial,
            "margin_maintenance": self.margin_maintenance,
            "margin_hedged": self.margin_hedged,
            "order_gtc_mode": self.order_gtc_mode,
            "order_gtc_mode_label": self.order_gtc_mode_label,
        }
