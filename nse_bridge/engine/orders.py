"""Category 7 — pending orders & history (55-63)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..errors import BridgeError, ErrorCode
from ..mt5.types import (
    build_deal_model,
    build_history_order_model,
    build_order_model,
)
from ..mt5.types import (
    f as _f,
)
from ..retcodes import TRADE_RETCODE_DONE, translate_retcode
from ..timeutil import broker_dt
from .common import BridgeContext
from .registry import command
from .trading import reject


def _fetch_orders(ctx: BridgeContext, symbol: str | None = None) -> list[Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()

    raw = ctx.driver.run(job, op="orders_get", context={"symbol": symbol} if symbol else None)
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "orders_get() failed — broker state unverified (not 'no orders').",
            details={"symbol": symbol},
        )
    return list(raw)


def _fetch_order(ctx: BridgeContext, ticket: int) -> Any | None:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.orders_get(ticket=ticket)

    raw = ctx.driver.run(job, op="orders_get", context={"ticket": ticket})
    return raw[0] if raw else None


def _history_orders(ctx: BridgeContext, from_dt: datetime, to_dt: datetime, group: str | None) -> list[Any]:
    def job(mt5: Any):
        if group:
            return mt5.history_orders_get(from_dt, to_dt, group=group)
        return mt5.history_orders_get(from_dt, to_dt)

    raw = ctx.driver.run(
        job, op="history_orders_get", context={"group": group, "from": str(from_dt), "to": str(to_dt)}
    )
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED, "history_orders_get() failed.",
            details={"group": group},
        )
    return list(raw)


def _history_deals(ctx: BridgeContext, from_dt: datetime, to_dt: datetime, group: str | None) -> list[Any]:
    def job(mt5: Any):
        if group:
            return mt5.history_deals_get(from_dt, to_dt, group=group)
        return mt5.history_deals_get(from_dt, to_dt)

    raw = ctx.driver.run(
        job, op="history_deals_get", context={"group": group, "from": str(from_dt), "to": str(to_dt)}
    )
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED, "history_deals_get() failed.",
            details={"group": group},
        )
    return list(raw)


class OrdersListRequest(BaseModel):
    symbol: str | None = None


class OrdersListResponse(BaseModel):
    count: int
    orders: list[dict[str, Any]]


class TicketRequest(BaseModel):
    ticket: int = Field(..., gt=0)


class OrderInfoResponse(BaseModel):
    order: dict[str, Any]
    source: str  # "active" or "history"


class HistoryRequest(BaseModel):
    from_utc: str | None = Field(None, description="Default: last 24h")
    to_utc: str | None = None
    symbol: str | None = Field(None, description="MT5 group filter (a broker symbol name, or a prefix wildcard such as EUR*)")


class HistoryOrdersResponse(BaseModel):
    count: int
    from_utc: str | None = None
    to_utc: str | None = None
    orders: list[dict[str, Any]]


class HistoryPositionSummary(BaseModel):
    position_id: int
    symbol: str | None = None
    volume_in: float = 0.0
    volume_out: float = 0.0
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    fee: float = 0.0
    open_time_utc: str | None = None
    close_time_utc: str | None = None
    direction: str | None = None


class HistoryPositionsResponse(BaseModel):
    count: int
    positions: list[HistoryPositionSummary]


class HistoryDealsResponse(BaseModel):
    count: int
    deals: list[dict[str, Any]]


class CancelResponse(BaseModel):
    status: str
    ticket: int
    retcode: int
    retcode_label: str | None = None


class ModifyPendingRequest(BaseModel):
    ticket: int = Field(..., gt=0)
    price: float | None = Field(None, gt=0.0)
    sl: float | None = Field(None, ge=0.0)
    tp: float | None = Field(None, ge=0.0)
    volume: float | None = Field(None, gt=0.0)
    expiration_utc: str | None = Field(None, description="Switches order to ORDER_TIME_SPECIFIED")


class OrderExecutionDetailsResponse(BaseModel):
    ticket: int
    order: dict[str, Any] | None = None
    deals: list[dict[str, Any]]
    slippage_price: float | None = None
    note: str | None = None


@command(
    "get_trading_pending_orders",
    number=55,
    category="orders",
    title="Get Pending Orders",
    description="All active pending orders (LIMIT/STOP), optionally filtered by symbol.",
    request_model=OrdersListRequest,
    response_model=OrdersListResponse,
)
def get_trading_pending_orders(ctx: BridgeContext, params: OrdersListRequest | None) -> dict[str, Any]:
    p = params or OrdersListRequest()
    raw = _fetch_orders(ctx, p.symbol.strip() if p.symbol else None)
    orders = [build_order_model(o, ctx.settings.broker_utc_offset_minutes).model_dump() for o in raw]
    return {"count": len(orders), "orders": orders}


def _find_order_everywhere(ctx: BridgeContext, ticket: int) -> tuple[Any | None, str]:
    """Active book first, then history (filled/cancelled orders live in history)."""
    active = _fetch_order(ctx, ticket)
    if active is not None:
        return active, "active"
    now = datetime.now(UTC) + timedelta(minutes=ctx.settings.broker_utc_offset_minutes)
    for days in (1, 30, 365):
        raw = _history_orders(ctx, now - timedelta(days=days), now, None)
        for o in raw:
            if int(_f(o, "ticket", 0)) == ticket:
                return o, "history"
    return None, "none"


@command(
    "get_order_info",
    number=56,
    category="orders",
    title="Get Order Info",
    description="Order details by ticket — active book first, then trade history (filled/cancelled).",
    request_model=TicketRequest,
    response_model=OrderInfoResponse,
)
def get_order_info(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw, source = _find_order_everywhere(ctx, params.ticket)
    if raw is None:
        raise BridgeError(
            ErrorCode.TICKET_NOT_FOUND,
            f"Order #{params.ticket} not found in active book or history.",
            details={"ticket": params.ticket},
        )
    model = (
        build_order_model(raw, ctx.settings.broker_utc_offset_minutes)
        if source == "active"
        else build_history_order_model(raw, ctx.settings.broker_utc_offset_minutes)
    )
    return {"order": model.model_dump(), "source": source}


@command(
    "get_order_status",
    number=57,
    category="orders",
    title="Get Order Status",
    description="Lifecycle status of an order: active (PLACED) or terminal state from history.",
    request_model=TicketRequest,
    response_model=OrderInfoResponse,
)
def get_order_status(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    return get_order_info(ctx, params)


@command(
    "get_trading_history_orders",
    number=58,
    category="orders",
    title="Get History Orders",
    description="Historical orders in a UTC window (default last 24h), optional symbol group filter.",
    request_model=HistoryRequest,
    response_model=HistoryOrdersResponse,
)
def get_trading_history_orders(ctx: BridgeContext, params: HistoryRequest | None) -> dict[str, Any]:
    p = params or HistoryRequest()
    offset = ctx.settings.broker_utc_offset_minutes
    now = datetime.now(UTC) + timedelta(minutes=offset)
    from_dt = broker_dt(p.from_utc, ctx.settings) if p.from_utc else now - timedelta(days=1)
    to_dt = broker_dt(p.to_utc, ctx.settings) if p.to_utc else now
    if from_dt is None or to_dt < from_dt:
        raise BridgeError(ErrorCode.VALIDATION, "Invalid history window", http_status=422)
    raw = _history_orders(ctx, from_dt, to_dt, p.symbol.strip() if p.symbol else None)
    orders = [build_history_order_model(o, ctx.settings.broker_utc_offset_minutes).model_dump() for o in raw]
    return {
        "count": len(orders),
        "from_utc": from_dt.isoformat(),
        "to_utc": to_dt.isoformat(),
        "orders": orders,
    }


@command(
    "get_trading_history_positions",
    number=59,
    category="orders",
    title="Get History Positions",
    description="Closed positions reconstructed from deal history (grouped by position_id): volume in/out, "
    "net profit, commission, swap, open/close times.",
    request_model=HistoryRequest,
    response_model=HistoryPositionsResponse,
)
def get_trading_history_positions(ctx: BridgeContext, params: HistoryRequest | None) -> dict[str, Any]:
    p = params or HistoryRequest()
    offset = ctx.settings.broker_utc_offset_minutes
    now = datetime.now(UTC) + timedelta(minutes=offset)
    from_dt = broker_dt(p.from_utc, ctx.settings) if p.from_utc else now - timedelta(days=1)
    to_dt = broker_dt(p.to_utc, ctx.settings) if p.to_utc else now
    raw = _history_deals(ctx, from_dt, to_dt, p.symbol.strip() if p.symbol else None)

    grouped: dict[int, list[Any]] = defaultdict(list)
    for d in raw:
        pos_id = int(_f(d, "position_id", 0) or 0)
        if pos_id and str(_f(d, "symbol", "") or ""):
            grouped[pos_id].append(d)

    summaries: list[HistoryPositionSummary] = []
    for pos_id, deals in grouped.items():
        vol_in = vol_out = profit = commission = swap = fee = 0.0
        open_t = close_t = None
        direction = None
        for d in deals:
            entry = int(_f(d, "entry", -1))
            vol = float(_f(d, "volume", 0.0) or 0.0)
            profit += float(_f(d, "profit", 0.0) or 0.0)
            commission += float(_f(d, "commission", 0.0) or 0.0)
            swap += float(_f(d, "swap", 0.0) or 0.0)
            fee += float(_f(d, "fee", 0.0) or 0.0)
            t = int(_f(d, "time", 0) or 0)
            if entry in (0, 2):  # IN / INOUT
                vol_in += vol
                open_t = min(open_t, t) if open_t else t
                if direction is None:
                    direction = "buy" if int(_f(d, "type", 0)) == 0 else "sell"
            if entry in (1, 2, 3):  # OUT / INOUT / OUT_BY
                vol_out += vol
                close_t = max(close_t, t) if close_t else t
        summaries.append(
            HistoryPositionSummary(
                position_id=pos_id,
                symbol=str(_f(deals[0], "symbol", "") or ""),
                volume_in=round(vol_in, 8),
                volume_out=round(vol_out, 8),
                profit=round(profit, 8),
                commission=round(commission, 8),
                swap=round(swap, 8),
                fee=round(fee, 8),
                open_time_utc=_epoch_to_iso(open_t, offset),
                close_time_utc=_epoch_to_iso(close_t, offset),
                direction=direction,
            )
        )
    summaries.sort(key=lambda s: (s.close_time_utc or "", s.open_time_utc or ""), reverse=True)
    return {"count": len(summaries), "positions": summaries}


def _epoch_to_iso(epoch: int | None, offset_minutes: int) -> str | None:
    if not epoch:
        return None
    from ..timeutil import broker_epoch_to_utc, iso

    return iso(broker_epoch_to_utc(epoch, offset_minutes))


@command(
    "get_trading_deals_history",
    number=60,
    category="orders",
    title="Get Deals History",
    description="Executed deals in a UTC window with profit, commission, swap, entry type and reason.",
    request_model=HistoryRequest,
    response_model=HistoryDealsResponse,
)
def get_trading_deals_history(ctx: BridgeContext, params: HistoryRequest | None) -> dict[str, Any]:
    p = params or HistoryRequest()
    offset = ctx.settings.broker_utc_offset_minutes
    now = datetime.now(UTC) + timedelta(minutes=offset)
    from_dt = broker_dt(p.from_utc, ctx.settings) if p.from_utc else now - timedelta(days=1)
    to_dt = broker_dt(p.to_utc, ctx.settings) if p.to_utc else now
    raw = _history_deals(ctx, from_dt, to_dt, p.symbol.strip() if p.symbol else None)
    deals = [build_deal_model(d, offset).model_dump() for d in raw]
    return {"count": len(deals), "deals": deals}


@command(
    "trade_cancel_pending_order",
    number=61,
    category="orders",
    title="Cancel Pending Order",
    description="Remove an active pending order (TRADE_ACTION_REMOVE).",
    request_model=TicketRequest,
    response_model=CancelResponse,
    mutates=True,
)
def trade_cancel_pending_order(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw = _fetch_order(ctx, params.ticket)
    if raw is None:
        raise BridgeError(
            ErrorCode.TICKET_NOT_FOUND,
            f"Pending order #{params.ticket} not found in the active book.",
            details={"ticket": params.ticket},
        )
    symbol = str(_f(raw, "symbol", ""))

    def job(mt5: Any):
        return mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(params.ticket)})

    result = ctx.driver.run(job, op="order_send_cancel", context={"ticket": params.ticket, "symbol": symbol})
    if result is None:
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — broker state unverified.",
            details={"ticket": params.ticket},
        )
    retcode = int(_f(result, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        raise reject(result, "cancel_pending_order", symbol)
    return {
        "status": "done",
        "ticket": params.ticket,
        "retcode": retcode,
        "retcode_label": translate_retcode(retcode),
    }


@command(
    "trade_modify_pending_order",
    number=62,
    category="orders",
    title="Modify Pending Order",
    description="Modify price/SL/TP/volume/expiration of an active pending order (TRADE_ACTION_MODIFY).",
    request_model=ModifyPendingRequest,
    response_model=CancelResponse,
    mutates=True,
)
def trade_modify_pending_order(ctx: BridgeContext, params: ModifyPendingRequest) -> dict[str, Any]:
    raw = _fetch_order(ctx, params.ticket)
    if raw is None:
        raise BridgeError(
            ErrorCode.TICKET_NOT_FOUND,
            f"Pending order #{params.ticket} not found in the active book.",
            details={"ticket": params.ticket},
        )
    symbol = str(_f(raw, "symbol", ""))
    price = params.price if params.price is not None else float(_f(raw, "price_open", 0.0))
    sl = params.sl if params.sl is not None else float(_f(raw, "sl", 0.0) or 0.0)
    tp = params.tp if params.tp is not None else float(_f(raw, "tp", 0.0) or 0.0)
    volume = params.volume if params.volume is not None else float(_f(raw, "volume_current", 0.0) or 0.0)

    def job(mt5: Any):
        request = {
            "action": mt5.TRADE_ACTION_MODIFY,
            "order": int(params.ticket),
            "price": float(price),
            "sl": float(sl),
            "tp": float(tp),
            "volume": float(volume),
        }
        if params.expiration_utc:
            exp = broker_dt(params.expiration_utc, ctx.settings)
            if exp is not None:
                request["type_time"] = mt5.ORDER_TIME_SPECIFIED
                request["expiration"] = exp
        return mt5.order_send(request)

    result = ctx.driver.run(job, op="order_send_modify", context={"ticket": params.ticket})
    if result is None:
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — broker state unverified.",
            details={"ticket": params.ticket},
        )
    retcode = int(_f(result, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        raise reject(result, "modify_pending_order", symbol)
    return {
        "status": "done",
        "ticket": params.ticket,
        "retcode": retcode,
        "retcode_label": translate_retcode(retcode),
    }


@command(
    "get_order_execution_details",
    number=63,
    category="orders",
    title="Get Order Execution Details",
    description="Execution forensics for a historical order: fill price vs requested price (slippage), "
    "fill time, deals, commission.",
    request_model=TicketRequest,
    response_model=OrderExecutionDetailsResponse,
)
def get_order_execution_details(ctx: BridgeContext, params: TicketRequest) -> dict[str, Any]:
    raw, source = _find_order_everywhere(ctx, params.ticket)
    if raw is None:
        raise BridgeError(
            ErrorCode.TICKET_NOT_FOUND,
            f"Order #{params.ticket} not found in active book or history.",
            details={"ticket": params.ticket},
        )
    order_model = (
        build_order_model(raw, ctx.settings.broker_utc_offset_minutes)
        if source == "active"
        else build_history_order_model(raw, ctx.settings.broker_utc_offset_minutes)
    )
    offset = ctx.settings.broker_utc_offset_minutes
    now = datetime.now(UTC) + timedelta(minutes=offset)
    deals_raw = _history_deals(ctx, now - timedelta(days=365), now, None)
    deals = [
        build_deal_model(d, offset).model_dump()
        for d in deals_raw
        if int(_f(d, "order", 0) or 0) == params.ticket
    ]
    slippage = None
    note = None
    if deals and order_model.price_open:
        fill = deals[0].get("price")
        if fill:
            slippage = round(float(fill) - float(order_model.price_open), 8)
    if source == "active":
        note = "Order is still active — execution details available after it fills."
    return {
        "ticket": params.ticket,
        "order": order_model.model_dump(),
        "deals": deals,
        "slippage_price": slippage,
        "note": note,
    }
