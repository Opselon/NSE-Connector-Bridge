"""Category 8 — trade execution (64-72). MUTATES the account."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import f as _f
from .common import BridgeContext, require_symbol_info, resolve_order_symbol
from .registry import command
from .symbols import _fresh_tick
from .trading import (
    TradeExecutionResponse,
    close_position,
    fetch_position,
    market_order,
    modify_position_sltp,
    reject,
)

#: MT5 ORDER_TYPE values for pending orders. Stop-limit types (6/7)
#: were missing until the C2 execution work (spec §6): a stop-limit
#: carries BOTH a trigger ``price`` and a ``stoplimit`` (the limit
#: price placed once the trigger fires).
PENDING_TYPES = {
    "BUY_LIMIT": 2,
    "SELL_LIMIT": 3,
    "BUY_STOP": 4,
    "SELL_STOP": 5,
    "BUY_STOP_LIMIT": 6,
    "SELL_STOP_LIMIT": 7,
}


class MarketOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    volume: float = Field(..., gt=0.0)
    sl: float = Field(0.0, ge=0.0)
    tp: float = Field(0.0, ge=0.0)
    deviation: int = Field(20, ge=0, le=1000)
    comment: str = Field("NSE_MARKET", max_length=31)
    magic: int | None = Field(
        None,
        description="Override magic; default = explicitly configured bridge magic, or no magic stamped when none is configured",
    )


@command(
    "trade_send_market_order",
    number=64,
    category="trading",
    title="Send Market Order",
    description="Market buy/sell with SL/TP, deviation and magic. Ambiguous fills are reconciled "
    "against broker truth before any failure is reported.",
    request_model=MarketOrderRequest,
    response_model=TradeExecutionResponse,
    mutates=True,
)
def trade_send_market_order(ctx: BridgeContext, params: MarketOrderRequest) -> dict[str, Any]:
    return market_order(
        ctx,
        symbol=params.symbol,
        side=params.side,
        volume=params.volume,
        sl=params.sl,
        tp=params.tp,
        deviation=params.deviation,
        comment=params.comment,
        magic=params.magic,
    )


class SimpleOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    volume: float = Field(..., gt=0.0)
    sl: float = Field(0.0, ge=0.0)
    tp: float = Field(0.0, ge=0.0)
    deviation: int = Field(20, ge=0, le=1000)
    comment: str = Field("NSE_MARKET", max_length=31)
    magic: int | None = None


@command(
    "trade_send_buy_order",
    number=65,
    category="trading",
    title="Send Buy Order",
    description="Market buy (long entry) with SL/TP.",
    request_model=SimpleOrderRequest,
    response_model=TradeExecutionResponse,
    mutates=True,
)
def trade_send_buy_order(ctx: BridgeContext, params: SimpleOrderRequest) -> dict[str, Any]:
    return market_order(
        ctx,
        symbol=params.symbol,
        side="buy",
        volume=params.volume,
        sl=params.sl,
        tp=params.tp,
        deviation=params.deviation,
        comment=params.comment,
        magic=params.magic,
    )


@command(
    "trade_send_sell_order",
    number=66,
    category="trading",
    title="Send Sell Order",
    description="Market sell (short entry) with SL/TP.",
    request_model=SimpleOrderRequest,
    response_model=TradeExecutionResponse,
    mutates=True,
)
def trade_send_sell_order(ctx: BridgeContext, params: SimpleOrderRequest) -> dict[str, Any]:
    return market_order(
        ctx,
        symbol=params.symbol,
        side="sell",
        volume=params.volume,
        sl=params.sl,
        tp=params.tp,
        deviation=params.deviation,
        comment=params.comment,
        magic=params.magic,
    )


class PendingOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    order_type: Literal["BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY_STOP_LIMIT", "SELL_STOP_LIMIT"]
    volume: float = Field(..., gt=0.0)
    price: float = Field(..., gt=0.0)
    #: Limit price for the stop-limit types (required for
    #: BUY_STOP_LIMIT / SELL_STOP_LIMIT): once the trigger ``price``
    #: fires, a limit order is placed at this price.
    stoplimit: float | None = Field(None, gt=0.0)
    sl: float = Field(0.0, ge=0.0)
    tp: float = Field(0.0, ge=0.0)
    expiration_utc: str | None = None
    comment: str = Field("NSE_PENDING", max_length=31)
    magic: int | None = None


def validate_pending(
    ctx: BridgeContext,
    symbol: str,
    order_type: str,
    price: float,
    sl: float,
    tp: float,
    volume: float,
    stoplimit: float | None = None,
) -> list[str]:
    """Pre-dispatch structural validation against the CURRENT market (ported BUG-226).

    Entry-side geometry is per-type: LIMIT orders enter BEYOND the market
    (buy below ask / sell above bid), STOP orders enter THROUGH the market
    (buy above ask / sell below bid) — each with the stops-level gap.
    STOP-LIMIT orders share the STOP trigger geometry, and their
    ``stoplimit`` (the limit price placed once the trigger fires) must
    sit on the favourable side of the trigger (below it for buys, above
    it for sells); SL/TP geometry is then checked against the stoplimit
    — the price the limit order will actually fill at.
    """
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    reasons: list[str] = []
    point = float(spec.point or 0.0)
    stops_level = float(spec.trade_stops_level or 0)
    spread = float((tick.ask or 0) - (tick.bid or 0))
    safety_floor = 10 * point  # 10 broker points floor (ported)
    min_gap = max(stops_level * point, spread, safety_floor)
    min_stop_dist = max(stops_level * point, safety_floor)

    vol_min = float(spec.volume_min or 0.0)
    vol_max = float(spec.volume_max or 0.0)
    vol_step = float(spec.volume_step or 0.01)
    if volume < vol_min - 1e-9:
        reasons.append("volume_below_min")
    if volume > vol_max + 1e-9:
        reasons.append("volume_above_max")
    if vol_step > 0:
        steps = round(volume / vol_step)
        if abs(volume - steps * vol_step) > 1e-9:
            reasons.append("volume_not_multiple_of_step")

    is_buy = order_type.startswith("BUY")
    ask, bid = float(tick.ask), float(tick.bid)
    is_stop_limit = order_type in ("BUY_STOP_LIMIT", "SELL_STOP_LIMIT")
    if is_stop_limit and (stoplimit is None or stoplimit <= 0):
        reasons.append("stoplimit_required")
    if order_type == "BUY_LIMIT" and price >= ask - min_gap + 1e-9:
        reasons.append("buy_limit_price_not_below_ask")
    if order_type == "SELL_LIMIT" and price <= bid + min_gap - 1e-9:
        reasons.append("sell_limit_price_not_above_bid")
    if order_type in ("BUY_STOP", "BUY_STOP_LIMIT") and price <= ask + min_gap - 1e-9:
        reasons.append("buy_stop_price_not_above_ask")
    if order_type in ("SELL_STOP", "SELL_STOP_LIMIT") and price >= bid - min_gap + 1e-9:
        reasons.append("sell_stop_price_not_below_bid")
    if order_type == "BUY_STOP_LIMIT" and stoplimit is not None and stoplimit >= price - 1e-9:
        reasons.append("stoplimit_not_below_trigger")
    if order_type == "SELL_STOP_LIMIT" and stoplimit is not None and stoplimit <= price + 1e-9:
        reasons.append("stoplimit_not_above_trigger")
    market_ref = ask if is_buy else bid
    if abs(price - market_ref) < min_gap - 1e-9:
        reasons.append("entry_inside_stops_level")

    # SL/TP geometry is checked against the ENTRY price — the stoplimit
    # for stop-limit orders (the price the limit order fills at).
    entry = stoplimit if (is_stop_limit and stoplimit is not None) else price
    if sl > 0:
        if is_buy and sl >= entry - min_stop_dist + 1e-9:
            reasons.append("sl_not_below_entry")
        if not is_buy and sl <= entry + min_stop_dist - 1e-9:
            reasons.append("sl_not_above_entry")
    if tp > 0:
        if is_buy and tp <= entry + min_stop_dist - 1e-9:
            reasons.append("tp_not_above_entry")
        if not is_buy and tp >= entry - min_stop_dist + 1e-9:
            reasons.append("tp_not_below_entry")
    return reasons


@command(
    "trade_send_pending_order",
    number=67,
    category="trading",
    title="Send Pending Order",
    description="Place LIMIT/STOP/STOP-LIMIT order with pre-dispatch structural validation (volume bounds, entry side, "
    "stops/freeze distances, SL/TP geometry, stop-limit trigger/limit geometry) — structurally-invalid requests are rejected BEFORE dispatch.",
    request_model=PendingOrderRequest,
    response_model=TradeExecutionResponse,
    mutates=True,
)
def trade_send_pending_order(ctx: BridgeContext, params: PendingOrderRequest) -> dict[str, Any]:
    ctx.driver.ensure_connected()
    # Canonical ``BASE/QUOTE`` spellings resolve to the exact broker
    # symbol here (spec §4); exact broker names pass through untouched.
    symbol = resolve_order_symbol(ctx, params.symbol.strip())
    reasons = validate_pending(
        ctx,
        symbol,
        params.order_type,
        params.price,
        params.sl,
        params.tp,
        params.volume,
        stoplimit=params.stoplimit,
    )
    if reasons:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Structurally invalid pending request: {', '.join(reasons)}",
            http_status=422,
            details={"reasons": reasons, "symbol": symbol, "order_type": params.order_type, "price": params.price},
        )

    # resolve filling BEFORE entering the worker job — never call driver.run()
    # from inside a job (single-thread executor: nested run() deadlocks)
    from .common import resolve_filling_mode

    filling = resolve_filling_mode(ctx, symbol)

    def job(mt5: Any):
        # No hardcoded magic: per-command magic wins, else the
        # explicitly configured device magic, else the "magic" key is
        # omitted entirely (MT5 records magic 0 = no magic stamped).
        mgc = params.magic if params.magic is not None else ctx.settings.magic
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(params.volume),
            "type": PENDING_TYPES[params.order_type],
            "price": float(params.price),
            "sl": float(params.sl),
            "tp": float(params.tp),
            "comment": params.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        if params.stoplimit is not None:
            # Stop-limit only: the limit price placed once the trigger fires.
            request["stoplimit"] = float(params.stoplimit)
        if mgc is not None:
            request["magic"] = int(mgc)
        if params.expiration_utc:
            from ..timeutil import broker_dt

            exp = broker_dt(params.expiration_utc, ctx.settings)
            if exp is not None:
                request["type_time"] = mt5.ORDER_TIME_SPECIFIED
                request["expiration"] = exp
        return mt5.order_send(request)

    from ..retcodes import TRADE_RETCODE_DONE

    result = ctx.driver.run(job, op="order_send_pending", context={"symbol": symbol, "type": params.order_type})
    if result is None:
        ctx.trade_metrics.record("order_send_pending", "unknown", 0.0, None, {"symbol": symbol})
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — broker state unverified.",
            details={"symbol": symbol, "order_type": params.order_type},
        )
    retcode = int(_f(result, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        ctx.trade_metrics.record("order_send_pending", "rejected", 0.0, retcode, {"symbol": symbol})
        raise reject(result, "pending_order", symbol)
    ctx.trade_metrics.record("order_send_pending", "done", 0.0, retcode, {"symbol": symbol})
    ticket = int(_f(result, "order", 0) or 0)
    return _pending_payload(ctx, symbol, ticket)


def _resolve_filling(ctx: BridgeContext, mt5: Any, symbol: str) -> int:
    from .common import resolve_filling_mode

    return resolve_filling_mode(ctx, symbol)


def _pending_payload(ctx: BridgeContext, symbol: str, ticket: int) -> dict[str, Any]:
    """Fetch the just-placed pending order for a rich execution payload.

    The active book may lag the dispatch by a moment — retry briefly.
    """
    import time as _time

    from ..mt5.types import build_order_model
    from .orders import _fetch_order

    raw = None
    if ticket:
        for _ in range(6):
            raw = _fetch_order(ctx, ticket)
            if raw is not None:
                break
            _time.sleep(0.15)
    payload: dict[str, Any] = {
        "status": "done",
        "symbol": symbol,
        "order_ticket": ticket or None,
        "retcode": 10009,
        "retcode_label": "DONE - Request completed successfully",
    }
    if raw is not None:
        payload.update(
            {
                "price": _f(raw, "price_open"),
                "volume": _f(raw, "volume_current"),
                "order": build_order_model(raw, ctx.settings.broker_utc_offset_minutes).model_dump(),
            }
        )
    return payload


class PartialCloseRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    volume: float = Field(..., gt=0.0)


@command(
    "trade_close_partial_position",
    number=68,
    category="trading",
    title="Close Partial Position",
    description="Close part of a position (volume < position volume).",
    request_model=PartialCloseRequest,
    response_model=TradeExecutionResponse,
    mutates=True,
)
def trade_close_partial_position(ctx: BridgeContext, params: PartialCloseRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    pos_volume = float(_f(raw, "volume", 0.0) or 0.0)
    if params.volume >= pos_volume - 1e-9:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Partial close volume {params.volume} must be smaller than position volume {pos_volume}. "
            "Use trade_close_single_position for a full close.",
            http_status=422,
            details={"ticket": params.ticket, "position_volume": pos_volume},
        )
    return close_position(ctx, params.ticket, params.volume)


class TicketOnlyRequest(BaseModel):
    ticket: int = Field(..., gt=0)


@command(
    "trade_reverse_position",
    number=69,
    category="trading",
    title="Reverse Position",
    description="Close a position and immediately open the opposite side with the same volume.",
    request_model=TicketOnlyRequest,
    response_model=object,
    mutates=True,
)
def trade_reverse_position(ctx: BridgeContext, params: TicketOnlyRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    pos_type = int(_f(raw, "type", -1))
    symbol = str(_f(raw, "symbol", ""))
    volume = float(_f(raw, "volume", 0.0) or 0.0)
    sl = float(_f(raw, "sl", 0.0) or 0.0)
    tp = float(_f(raw, "tp", 0.0) or 0.0)
    magic = int(_f(raw, "magic", 0) or 0)
    side = "sell" if pos_type == 0 else "buy"
    closed = close_position(ctx, params.ticket)
    opened = market_order(
        ctx,
        symbol=symbol,
        side=side,
        volume=volume,
        sl=0.0,
        tp=0.0,
        comment="NSE_REVERSE",
        magic=magic or None,
    )
    return {
        "status": "done",
        "ticket": params.ticket,
        "symbol": symbol,
        "original_side": "buy" if pos_type == 0 else "sell",
        "reversed_to": side,
        "volume": volume,
        "original_sl_tp_preserved": False,
        "sl_request": sl,
        "tp_request": tp,
        "close": closed,
        "open": opened,
    }


class SetSlRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    sl: float = Field(..., ge=0.0)


class SetTpRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    tp: float = Field(..., ge=0.0)


@command(
    "trade_set_position_sl",
    number=70,
    category="trading",
    title="Set Position Stop Loss",
    description="Set (or replace) the stop-loss of an open position, preserving its current TP.",
    request_model=SetSlRequest,
    response_model=object,
    mutates=True,
)
def trade_set_position_sl(ctx: BridgeContext, params: SetSlRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    current_tp = float(_f(raw, "tp", 0.0) or 0.0)
    return modify_position_sltp(ctx, params.ticket, params.sl, current_tp)


@command(
    "trade_set_position_tp",
    number=71,
    category="trading",
    title="Set Position Take Profit",
    description="Set (or replace) the take-profit of an open position, preserving its current SL.",
    request_model=SetTpRequest,
    response_model=object,
    mutates=True,
)
def trade_set_position_tp(ctx: BridgeContext, params: SetTpRequest) -> dict[str, Any]:
    raw = fetch_position(ctx, params.ticket)
    current_sl = float(_f(raw, "sl", 0.0) or 0.0)
    return modify_position_sltp(ctx, params.ticket, current_sl, params.tp)


class CalcResultRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    volume: float = Field(..., gt=0.0)
    price_open: float = Field(..., gt=0.0)
    price_close: float = Field(..., gt=0.0)


@command(
    "trade_calculate_order_result",
    number=72,
    category="trading",
    title="Calculate Order Result",
    description="Broker-native PnL calculation for a hypothetical exit (order_calc_profit).",
    request_model=CalcResultRequest,
    response_model=object,
)
def trade_calculate_order_result(ctx: BridgeContext, params: CalcResultRequest) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        op_type = mt5.ORDER_TYPE_BUY if params.side == "buy" else mt5.ORDER_TYPE_SELL
        return mt5.order_calc_profit(
            op_type, params.symbol, float(params.volume), float(params.price_open), float(params.price_close)
        )

    raw = ctx.driver.run(job, op="order_calc_profit", context={"symbol": params.symbol, "volume": params.volume})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "order_calc_profit unavailable for this symbol.",
            details={"symbol": params.symbol},
        )
    spec = require_symbol_info(ctx, params.symbol)
    return {
        "symbol": params.symbol,
        "side": params.side,
        "volume": params.volume,
        "price_open": params.price_open,
        "price_close": params.price_close,
        "profit": float(raw),
        "currency": spec.currency_profit or "USD",
        "source": "BROKER_NATIVE",
    }
