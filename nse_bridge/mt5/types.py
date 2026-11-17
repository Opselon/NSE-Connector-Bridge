"""Typed models built from raw MT5 namedtuples (defensive against driver changes)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from ..timeutil import broker_epoch_to_utc, iso


def f(raw: Any, name: str, default: Any = None) -> Any:
    """Universal field accessor: item access first (numpy structured rows, dicts),
    then attribute access (namedtuples). Normalizes numpy scalars to Python types."""
    if raw is None:
        return default
    try:
        val = raw[name]
        return default if val is None else _py(val)
    except Exception:
        pass
    val = getattr(raw, name, None)
    return default if val is None else _py(val)


def _py(val: Any) -> Any:
    """numpy scalar -> python scalar (np.int64 is not a pydantic int)."""
    t = type(val)
    if t.__module__ == "numpy" and hasattr(val, "item"):
        try:
            return val.item()
        except Exception:
            return val
    return val


def ms_to_iso(epoch_s: float, offset_minutes: int = 0) -> str | None:
    if not epoch_s:
        return None
    return iso(broker_epoch_to_utc(epoch_s, offset_minutes))


ORDER_TYPE_LABELS = {
    0: "BUY", 1: "SELL", 2: "BUY_LIMIT", 3: "SELL_LIMIT",
    4: "BUY_STOP", 5: "SELL_STOP", 6: "BUY_STOP_LIMIT", 7: "SELL_STOP_LIMIT",
}
DEAL_TYPE_LABELS = {
    0: "BUY", 1: "SELL", 2: "BALANCE", 3: "CREDIT", 4: "CHARGE", 5: "CORRECTION",
    6: "BONUS", 7: "COMMISSION", 8: "COMMISSION_DAILY", 9: "COMMISSION_MONTHLY",
    10: "COMMISSION_AGENT_DAILY", 11: "COMMISSION_AGENT_MONTHLY", 12: "COMMISSION_AGENT",
    13: "DEAL_SPLIT",
}
DEAL_ENTRY_LABELS = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}
ORDER_STATE_LABELS = {
    0: "STARTED", 1: "PLACED", 2: "CANCELED", 3: "PARTIAL",
    4: "FILLED", 5: "REJECTED", 6: "PENDING_CANCEL", 7: "EXPIRED",
}
TRADE_MODE_LABELS = {
    0: "DISABLED", 1: "LONG_ONLY", 2: "SHORT_ONLY", 3: "CLOSE_ONLY", 4: "FULL",
}
FILLING_LABELS = {0: "FOK", 1: "IOC", 2: "RETURN"}
EXECUTION_LABELS = {0: "REQUEST", 1: "INSTANT", 2: "MARKET", 3: "EXCHANGE"}
TIMEFRAME_NAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
)
TIMEFRAME_SECONDS = {
    "M1": 60, "M2": 120, "M3": 180, "M4": 240, "M5": 300, "M6": 360,
    "M10": 600, "M12": 720, "M15": 900, "M20": 1200, "M30": 1800,
    "H1": 3600, "H2": 7200, "H3": 10800, "H4": 14400, "H6": 21600,
    "H8": 28800, "H12": 43200, "D1": 86400, "W1": 604800, "MN1": 2592000,
}


class TerminalInfoModel(BaseModel):
    name: str | None = None
    company: str | None = None
    path: str | None = None
    data_path: str | None = None
    connected: bool = False
    trade_allowed: bool = False
    trade_expert: bool | None = None
    dlls_allowed: bool | None = None
    login: int | None = None
    maxbars: int | None = None
    portable: bool | None = None


class AccountInfoModel(BaseModel):
    login: int
    name: str | None = None
    server: str | None = None
    currency: str | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    leverage: int | None = None
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    margin_free: float | None = None
    margin_level: float | None = None
    profit: float | None = None
    swap: float | None = None
    commission: float | None = None
    credit: float | None = None
    margin_so_mode: int | None = None
    margin_so_call: float | None = None
    margin_so_so: float | None = None
    trade_allowed: bool | None = None
    trade_expert: bool | None = None


class TickModel(BaseModel):
    symbol: str | None = None
    time: int | None = None
    time_msc: int | None = None
    time_utc: str | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    flags: int | None = None
    spread_points: float | None = None
    freshness_ms: float | None = None
    stale: bool | None = None


class SymbolInfoModel(BaseModel):
    name: str
    description: str | None = None
    path: str | None = None
    visible: bool | None = None
    selected: bool | None = None
    session_deals: int | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    digits: int | None = None
    point: float | None = None
    spread: int | None = None
    spread_float: float | None = None
    trade_tick_size: float | None = None
    trade_tick_value: float | None = None
    trade_tick_value_loss: float | None = None
    trade_contract_size: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    volume_limit: float | None = None
    trade_stops_level: int | None = None
    trade_freeze_level: int | None = None
    trade_execution: int | None = None
    trade_execution_label: str | None = None
    filling_mode: int | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    margin_initial: float | None = None
    margin_maintenance: float | None = None
    currency_base: str | None = None
    currency_profit: str | None = None
    currency_margin: str | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    session_deals: int | None = None
    session_volume: float | None = None


class PositionModel(BaseModel):
    ticket: int
    symbol: str
    type: int
    type_label: str | None = None
    volume: float
    price_open: float
    price_current: float | None = None
    sl: float | None = None
    tp: float | None = None
    profit: float | None = None
    swap: float | None = None
    commission: float | None = None
    magic: int | None = None
    comment: str | None = None
    time: int | None = None
    time_utc: str | None = None


class OrderModel(BaseModel):
    ticket: int
    symbol: str
    type: int
    type_label: str | None = None
    volume_initial: float | None = None
    volume_current: float | None = None
    price_open: float
    price_current: float | None = None
    sl: float | None = None
    tp: float | None = None
    profit: float | None = None
    magic: int | None = None
    comment: str | None = None
    state: int | None = None
    state_label: str | None = None
    type_time: int | None = None
    type_filling: int | None = None
    time_setup: int | None = None
    time_setup_utc: str | None = None
    time_expiration: int | None = None
    position_id: int | None = None


class HistoryOrderModel(BaseModel):
    ticket: int
    symbol: str
    type: int
    type_label: str | None = None
    volume_initial: float | None = None
    volume_current: float | None = None
    price_open: float | None = None
    price_current: float | None = None
    sl: float | None = None
    tp: float | None = None
    state: int | None = None
    state_label: str | None = None
    magic: int | None = None
    comment: str | None = None
    position_id: int | None = None
    time_setup: int | None = None
    time_setup_utc: str | None = None
    time_done: int | None = None
    time_done_utc: str | None = None


class DealModel(BaseModel):
    ticket: int
    order: int | None = None
    position_id: int | None = None
    symbol: str | None = None
    type: int | None = None
    type_label: str | None = None
    entry: int | None = None
    entry_label: str | None = None
    volume: float | None = None
    price: float | None = None
    profit: float | None = None
    commission: float | None = None
    swap: float | None = None
    fee: float | None = None
    magic: int | None = None
    comment: str | None = None
    time: int | None = None
    time_utc: str | None = None
    reason: int | None = None


class RateBarModel(BaseModel):
    time: int
    time_utc: str | None = None
    open: float
    high: float
    low: float
    close: float
    tick_volume: int | None = None
    spread: int | None = None
    real_volume: int | None = None


class MarketBookEntryModel(BaseModel):
    type: int
    type_label: str | None = None
    price: float
    volume: float | None = None
    volume_dbl: float | None = None


class MarketBookModel(BaseModel):
    symbol: str
    entries: list[MarketBookEntryModel] = []
    time_utc: str | None = None


class TickHistoryModel(BaseModel):
    time_msc: int
    time_utc: str | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    flags: int | None = None


# --- builders -----------------------------------------------------------

def build_tick_model(raw: Any, symbol: str | None = None, offset_minutes: int = 0) -> TickModel:
    tick = TickModel(
        symbol=symbol or f(raw, "symbol"),
        time=int(f(raw, "time", 0) or 0),
        time_msc=int(f(raw, "time_msc", 0) or 0),
        bid=float(f(raw, "bid", 0.0) or 0.0),
        ask=float(f(raw, "ask", 0.0) or 0.0),
        last=float(f(raw, "last", 0.0) or 0.0),
        volume=float(f(raw, "volume", 0.0) or 0.0),
        flags=int(f(raw, "flags", 0) or 0),
    )
    if tick.time:
        tick.time_utc = ms_to_iso(tick.time, offset_minutes)
        age = (datetime.now(UTC) - broker_epoch_to_utc(tick.time, offset_minutes)).total_seconds() * 1000.0
        tick.freshness_ms = max(0.0, age)
        tick.stale = tick.freshness_ms > 30_000.0
    if tick.bid > 0 and tick.ask > 0:
        tick.spread_points = round(tick.ask - tick.bid, 8)
    return tick


def build_symbol_model(raw: Any, offset_minutes: int = 0) -> SymbolInfoModel:
    return SymbolInfoModel(
        name=str(f(raw, "name", "")),
        description=f(raw, "description"),
        path=f(raw, "path"),
        visible=bool(f(raw, "visible", False)),
        selected=bool(f(raw, "selected", False)),
        trade_mode=f(raw, "trade_mode"),
        trade_mode_label=TRADE_MODE_LABELS.get(f(raw, "trade_mode", -1)),
        digits=f(raw, "digits"),
        point=f(raw, "point"),
        spread=f(raw, "spread"),
        spread_float=f(raw, "spread_float"),
        trade_tick_size=f(raw, "trade_tick_size"),
        trade_tick_value=f(raw, "trade_tick_value"),
        trade_tick_value_loss=f(raw, "trade_tick_value_loss"),
        trade_contract_size=f(raw, "trade_contract_size"),
        volume_min=f(raw, "volume_min"),
        volume_max=f(raw, "volume_max"),
        volume_step=f(raw, "volume_step"),
        volume_limit=f(raw, "volume_limit"),
        trade_stops_level=f(raw, "trade_stops_level"),
        trade_freeze_level=f(raw, "trade_freeze_level"),
        trade_execution=f(raw, "trade_calc_mode"),
        trade_execution_label=EXECUTION_LABELS.get(f(raw, "trade_calc_mode", -1)),
        filling_mode=f(raw, "filling_mode"),
        swap_long=f(raw, "swap_long"),
        swap_short=f(raw, "swap_short"),
        margin_initial=f(raw, "margin_initial"),
        margin_maintenance=f(raw, "margin_maintenance"),
        currency_base=f(raw, "currency_base"),
        currency_profit=f(raw, "currency_profit"),
        currency_margin=f(raw, "currency_margin"),
        bid=f(raw, "bid"),
        ask=f(raw, "ask"),
        last=f(raw, "last"),
        session_deals=f(raw, "session_deals"),
        session_volume=f(raw, "session_volume"),
    )


def build_account_model(raw: Any) -> AccountInfoModel:
    return AccountInfoModel(
        login=int(f(raw, "login", 0)),
        name=f(raw, "name"),
        server=f(raw, "server"),
        currency=f(raw, "currency"),
        trade_mode=f(raw, "trade_mode"),
        trade_mode_label=TRADE_MODE_LABELS.get(f(raw, "trade_mode", -1)),
        leverage=f(raw, "leverage"),
        balance=f(raw, "balance"),
        equity=f(raw, "equity"),
        margin=f(raw, "margin"),
        margin_free=f(raw, "margin_free"),
        margin_level=f(raw, "margin_level"),
        profit=f(raw, "profit"),
        swap=f(raw, "swap"),
        commission=f(raw, "commission"),
        credit=f(raw, "credit"),
        margin_so_mode=f(raw, "margin_so_mode"),
        margin_so_call=f(raw, "margin_so_call"),
        margin_so_so=f(raw, "margin_so_so"),
        trade_allowed=f(raw, "trade_allowed"),
        trade_expert=f(raw, "trade_expert"),
    )


def build_position_model(raw: Any, offset_minutes: int = 0) -> PositionModel:
    t = int(f(raw, "type", -1))
    return PositionModel(
        ticket=int(f(raw, "ticket", 0)),
        symbol=str(f(raw, "symbol", "")),
        type=t,
        type_label=ORDER_TYPE_LABELS.get(t),
        volume=float(f(raw, "volume", 0.0)),
        price_open=float(f(raw, "price_open", 0.0)),
        price_current=f(raw, "price_current"),
        sl=f(raw, "sl"),
        tp=f(raw, "tp"),
        profit=f(raw, "profit"),
        swap=f(raw, "swap"),
        commission=f(raw, "commission"),
        magic=f(raw, "magic"),
        comment=f(raw, "comment"),
        time=f(raw, "time"),
        time_utc=ms_to_iso(f(raw, "time", 0), offset_minutes),
    )


def build_order_model(raw: Any, offset_minutes: int = 0) -> OrderModel:
    t = int(f(raw, "type", -1))
    return OrderModel(
        ticket=int(f(raw, "ticket", 0)),
        symbol=str(f(raw, "symbol", "")),
        type=t,
        type_label=ORDER_TYPE_LABELS.get(t),
        volume_initial=f(raw, "volume_initial"),
        volume_current=f(raw, "volume_current"),
        price_open=float(f(raw, "price_open", 0.0)),
        price_current=f(raw, "price_current"),
        sl=f(raw, "sl"),
        tp=f(raw, "tp"),
        profit=f(raw, "profit"),
        magic=f(raw, "magic"),
        comment=f(raw, "comment"),
        state=f(raw, "state"),
        state_label=ORDER_STATE_LABELS.get(f(raw, "state", -1)),
        type_time=f(raw, "type_time"),
        type_filling=f(raw, "type_filling"),
        time_setup=f(raw, "time_setup"),
        time_setup_utc=ms_to_iso(f(raw, "time_setup", 0), offset_minutes),
        time_expiration=f(raw, "time_expiration"),
        position_id=f(raw, "position_id"),
    )


def build_history_order_model(raw: Any, offset_minutes: int = 0) -> HistoryOrderModel:
    t = int(f(raw, "type", -1))
    return HistoryOrderModel(
        ticket=int(f(raw, "ticket", 0)),
        symbol=str(f(raw, "symbol", "")),
        type=t,
        type_label=ORDER_TYPE_LABELS.get(t),
        volume_initial=f(raw, "volume_initial"),
        volume_current=f(raw, "volume_current"),
        price_open=f(raw, "price_open"),
        price_current=f(raw, "price_current"),
        sl=f(raw, "sl"),
        tp=f(raw, "tp"),
        state=f(raw, "state"),
        state_label=ORDER_STATE_LABELS.get(f(raw, "state", -1)),
        magic=f(raw, "magic"),
        comment=f(raw, "comment"),
        position_id=f(raw, "position_id"),
        time_setup=f(raw, "time_setup"),
        time_setup_utc=ms_to_iso(f(raw, "time_setup", 0), offset_minutes),
        time_done=f(raw, "time_done"),
        time_done_utc=ms_to_iso(f(raw, "time_done", 0), offset_minutes),
    )


def build_deal_model(raw: Any, offset_minutes: int = 0) -> DealModel:
    return DealModel(
        ticket=int(f(raw, "ticket", 0)),
        order=f(raw, "order"),
        position_id=f(raw, "position_id"),
        symbol=f(raw, "symbol"),
        type=f(raw, "type"),
        type_label=DEAL_TYPE_LABELS.get(f(raw, "type", -1)),
        entry=f(raw, "entry"),
        entry_label=DEAL_ENTRY_LABELS.get(f(raw, "entry", -1)),
        volume=f(raw, "volume"),
        price=f(raw, "price"),
        profit=f(raw, "profit"),
        commission=f(raw, "commission"),
        swap=f(raw, "swap"),
        fee=f(raw, "fee"),
        magic=f(raw, "magic"),
        comment=f(raw, "comment"),
        time=f(raw, "time"),
        time_utc=ms_to_iso(f(raw, "time", 0), offset_minutes),
        reason=f(raw, "reason"),
    )


def build_rate_bar_model(raw: Any, offset_minutes: int = 0) -> RateBarModel:
    t = int(f(raw, "time", 0))
    return RateBarModel(
        time=t,
        time_utc=ms_to_iso(t, offset_minutes),
        open=float(f(raw, "open", 0.0)),
        high=float(f(raw, "high", 0.0)),
        low=float(f(raw, "low", 0.0)),
        close=float(f(raw, "close", 0.0)),
        tick_volume=int(f(raw, "tick_volume", 0) or 0),
        spread=f(raw, "spread"),
        real_volume=int(f(raw, "real_volume", 0) or 0),
    )


def build_tick_history_model(raw: Any, offset_minutes: int = 0) -> TickHistoryModel:
    msc = int(f(raw, "time_msc", 0) or 0)
    utc = None
    if msc:
        utc = iso(broker_epoch_to_utc(msc / 1000.0, offset_minutes))
    return TickHistoryModel(
        time_msc=msc,
        time_utc=utc,
        bid=f(raw, "bid"),
        ask=f(raw, "ask"),
        last=f(raw, "last"),
        volume=f(raw, "volume"),
        flags=f(raw, "flags"),
    )
