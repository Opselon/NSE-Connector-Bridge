"""Category 3 — symbols & market specifications (19-27)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import TRADE_MODE_LABELS, SymbolInfoModel
from ..mt5.types import f as _f
from .common import BridgeContext, datetime_now_iso, require_symbol_info
from .registry import command


class SymbolsListRequest(BaseModel):
    filter: str | None = Field(None, description="Case-insensitive substring filter on the symbol name")
    visible_only: bool = Field(False, description="Only symbols currently visible in Market Watch")
    tradeable_only: bool = Field(False, description="Exclude symbols with trade_mode=DISABLED")
    limit: int = Field(200, ge=1, le=5000, description="Max rows returned (server list can be huge)")


class SymbolListItem(BaseModel):
    name: str
    description: str | None = None
    visible: bool | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    digits: int | None = None
    currency_profit: str | None = None


class SymbolsListResponse(BaseModel):
    total_on_server: int
    returned: int
    symbols: list[SymbolListItem]


class SymbolInfoResponse(BaseModel):
    symbol: SymbolInfoModel


class SymbolPriceResponse(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float | None = None
    spread_price: float | None = None
    time_utc: str | None = None


class TickSizeResponse(BaseModel):
    symbol: str
    tick_size: float


class PointResponse(BaseModel):
    symbol: str
    point: float


class DigitsResponse(BaseModel):
    symbol: str
    digits: int


class SpreadResponse(BaseModel):
    symbol: str
    spread_points_broker: int | None = None
    spread_float_broker: float | None = None
    spread_price_live: float | None = None
    bid: float | None = None
    ask: float | None = None


class SessionsResponse(BaseModel):
    symbol: str
    session_stats: dict[str, Any]
    note: str


class ConstraintsResponse(BaseModel):
    symbol: str
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    volume_limit: float | None = None
    trade_stops_level: int | None = None
    trade_freeze_level: int | None = None
    trade_mode: int | None = None
    trade_mode_label: str | None = None
    filling_mode: int | None = None
    trade_execution: int | None = None
    trade_execution_label: str | None = None
    swap_long: float | None = None
    swap_short: float | None = None
    margin_initial: float | None = None
    margin_maintenance: float | None = None
    order_gtc_mode: int | None = None
    trade_contract_size: float | None = None


@command(
    "get_trading_symbols",
    number=19,
    category="symbols",
    title="Get Trading Symbols",
    description="List broker symbols with optional filter, visibility and tradeability flags.",
    request_model=SymbolsListRequest,
    response_model=SymbolsListResponse,
)
def get_trading_symbols(ctx: BridgeContext, params: SymbolsListRequest | None) -> dict[str, Any]:
    ctx.driver.ensure_connected()
    p = params or SymbolsListRequest()

    def job(mt5: Any):
        return mt5.symbols_get()

    raw = ctx.driver.run(job, op="symbols_get")
    if raw is None:
        raise BridgeError(ErrorCode.MT5_CALL_FAILED, "symbols_get() returned None")
    pattern = p.filter.upper() if p.filter else None
    items: list[SymbolListItem] = []
    for row in raw:
        name = str(_f(row, "name", "") or "")
        if not name:
            continue
        if pattern and pattern not in name.upper():
            continue
        mode = _f(row, "trade_mode")
        if p.tradeable_only and mode == 0:
            continue
        if p.visible_only and not bool(_f(row, "visible", False)):
            continue
        items.append(
            SymbolListItem(
                name=name,
                description=_f(row, "description"),
                visible=bool(_f(row, "visible", False)),
                trade_mode=mode,
                trade_mode_label=TRADE_MODE_LABELS.get(mode if mode is not None else -1),
                digits=_f(row, "digits"),
                currency_profit=_f(row, "currency_profit"),
            )
        )
        if len(items) >= p.limit:
            break
    return {
        "total_on_server": len(raw),
        "returned": len(items),
        "symbols": [i.model_dump() for i in items],
    }


@command(
    "get_symbol_info",
    number=20,
    category="symbols",
    title="Get Symbol Info",
    description="Full broker specification of one symbol (digits, point, tick size/value, volumes, stops levels...).",
    response_model=SymbolInfoResponse,
)
def get_symbol_info(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = (params or {}).get("symbol")
    if not symbol:
        raise BridgeError(ErrorCode.VALIDATION, "symbol is required", http_status=422)
    spec = require_symbol_info(ctx, str(symbol))
    return {"symbol": spec.model_dump()}


def _fresh_tick(ctx: BridgeContext, symbol: str):
    """Live tick with automatic Market Watch selection (ported adapter behavior)."""
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        raw = mt5.symbol_info_tick(symbol)
        if raw is None:
            mt5.symbol_select(symbol, True)  # not in Market Watch — select & retry once
            raw = mt5.symbol_info_tick(symbol)
        return raw

    raw = ctx.driver.run(job, op="symbol_info_tick", context={"symbol": symbol})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            f"No tick available for '{symbol}' (market closed or symbol unknown).",
            details={"symbol": symbol},
        )
    from ..mt5.types import build_tick_model

    return build_tick_model(raw, symbol, ctx.settings.broker_utc_offset_minutes)


def _symbol_param(params: dict[str, Any] | None) -> str:
    symbol = (params or {}).get("symbol")
    if not symbol:
        raise BridgeError(ErrorCode.VALIDATION, "symbol is required", http_status=422)
    return str(symbol).strip()


@command(
    "get_symbol_price",
    number=21,
    category="symbols",
    title="Get Symbol Price",
    description="Live bid/ask (and last deal price) for a symbol.",
    response_model=SymbolPriceResponse,
)
def get_symbol_price(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    tick = _fresh_tick(ctx, symbol)
    return {
        "symbol": symbol,
        "bid": tick.bid,
        "ask": tick.ask,
        "last": tick.last or None,
        "spread_price": tick.spread_points,
        "time_utc": tick.time_utc,
    }


@command(
    "get_symbol_tick_size",
    number=22,
    category="symbols",
    title="Get Symbol Tick Size",
    description="Minimum price increment (trade_tick_size; falls back to point).",
    response_model=TickSizeResponse,
)
def get_symbol_tick_size(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    spec = require_symbol_info(ctx, symbol)
    tick_size = spec.trade_tick_size or spec.point
    return {"symbol": symbol, "tick_size": float(tick_size or 0.0)}


@command(
    "get_symbol_point",
    number=23,
    category="symbols",
    title="Get Symbol Point",
    description="One point value for the symbol (e.g. 0.00001 on EURUSD 5-digit).",
    response_model=PointResponse,
)
def get_symbol_point(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    spec = require_symbol_info(ctx, symbol)
    return {"symbol": symbol, "point": float(spec.point or 0.0)}


@command(
    "get_symbol_digits",
    number=24,
    category="symbols",
    title="Get Symbol Digits",
    description="Number of price decimal digits.",
    response_model=DigitsResponse,
)
def get_symbol_digits(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    spec = require_symbol_info(ctx, symbol)
    return {"symbol": symbol, "digits": int(spec.digits or 0)}


@command(
    "get_symbol_spread",
    number=25,
    category="symbols",
    title="Get Symbol Spread",
    description="Broker-reported spread (points) + live ask-bid spread (price).",
    response_model=SpreadResponse,
)
def get_symbol_spread(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    return {
        "symbol": symbol,
        "spread_points_broker": spec.spread,
        "spread_float_broker": spec.spread_float,
        "spread_price_live": tick.spread_points,
        "bid": tick.bid,
        "ask": tick.ask,
    }


@command(
    "get_symbol_trading_sessions",
    number=26,
    category="symbols",
    title="Get Symbol Trading Sessions",
    description="Session statistics for the current trading session. "
    "Note: the official MT5 API does not expose hourly quote/trade session schedules — "
    "these are the live session aggregates the terminal exposes.",
    response_model=SessionsResponse,
)
def get_symbol_trading_sessions(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        raw = mt5.symbol_info(symbol)
        if raw is None:
            mt5.symbol_select(symbol, True)
            raw = mt5.symbol_info(symbol)
        return raw

    raw = ctx.driver.run(job, op="symbol_info", context={"symbol": symbol})
    if raw is None:
        raise BridgeError(ErrorCode.SYMBOL_NOT_FOUND, f"Symbol '{symbol}' not found", details={"symbol": symbol})
    stats = {
        "session_deals": _f(raw, "session_deals"),
        "session_buy_orders": _f(raw, "session_buy_orders"),
        "session_sell_orders": _f(raw, "session_sell_orders"),
        "session_turnover": _f(raw, "session_turnover"),
        "session_interest": _f(raw, "session_interest"),
        "session_buy_order_volume": _f(raw, "session_buy_order_volume"),
        "session_sell_order_volume": _f(raw, "session_sell_order_volume"),
        "session_open": _f(raw, "session_open"),
        "session_close": _f(raw, "session_close"),
        "session_aw": _f(raw, "session_aw"),
        "session_price_settlement": _f(raw, "session_price_settlement"),
        "prevsession_price_settlement": _f(raw, "prevsession_price_settlement"),
    }
    return {
        "symbol": symbol,
        "session_stats": stats,
        "note": "Hourly session schedules are not exposed by the MT5 Python API; "
        "terminal-level session aggregates are provided instead.",
        "probed_at": datetime_now_iso(),
    }


@command(
    "get_symbol_trade_constraints",
    number=27,
    category="symbols",
    title="Get Symbol Trade Constraints",
    description="Trade constraints: volume min/max/step, stops/freeze levels, trade mode, filling, swaps, margins.",
    response_model=ConstraintsResponse,
)
def get_symbol_trade_constraints(ctx: BridgeContext, params: dict[str, Any] | None) -> dict[str, Any]:
    symbol = _symbol_param(params)
    spec = require_symbol_info(ctx, symbol)
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.symbol_info(symbol)

    raw = ctx.driver.run(job, op="symbol_info", context={"symbol": symbol})
    return {
        "symbol": symbol,
        "volume_min": spec.volume_min,
        "volume_max": spec.volume_max,
        "volume_step": spec.volume_step,
        "volume_limit": spec.volume_limit,
        "trade_stops_level": spec.trade_stops_level,
        "trade_freeze_level": spec.trade_freeze_level,
        "trade_mode": spec.trade_mode,
        "trade_mode_label": spec.trade_mode_label,
        "filling_mode": spec.filling_mode,
        "trade_execution": spec.trade_execution,
        "trade_execution_label": spec.trade_execution_label,
        "swap_long": spec.swap_long,
        "swap_short": spec.swap_short,
        "margin_initial": spec.margin_initial,
        "margin_maintenance": spec.margin_maintenance,
        "order_gtc_mode": _f(raw, "order_gtc_mode"),
        "trade_contract_size": spec.trade_contract_size,
    }
