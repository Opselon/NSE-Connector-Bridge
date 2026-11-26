"""Category 7 — pending orders & history (55-63)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
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
from ..retcodes import (
    TRADE_RETCODE_CONNECTION,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_NO_CHANGES,
    translate_retcode,
)
from ..timeutil import broker_dt
from .common import BridgeContext
from .registry import command
from .retry import ExecutionRetryPolicy, retcode_class_label, retryable_retcode
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

    raw = ctx.driver.run(job, op="history_orders_get", context={"group": group, "from": str(from_dt), "to": str(to_dt)})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "history_orders_get() failed.",
            details={"group": group},
        )
    return list(raw)


def _history_deals(ctx: BridgeContext, from_dt: datetime, to_dt: datetime, group: str | None) -> list[Any]:
    def job(mt5: Any):
        if group:
            return mt5.history_deals_get(from_dt, to_dt, group=group)
        return mt5.history_deals_get(from_dt, to_dt)

    raw = ctx.driver.run(job, op="history_deals_get", context={"group": group, "from": str(from_dt), "to": str(to_dt)})
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "history_deals_get() failed.",
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
    symbol: str | None = Field(
        None, description="MT5 group filter (a broker symbol name, or a prefix wildcard such as EUR*)"
    )


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
    # No ``volume`` field: MQL5 TRADE_ACTION_MODIFY cannot change a
    # pending order's volume (delete + replace instead). An earlier
    # build sent the live ``volume_current`` in the request — harmless
    # (brokers ignore it) but wrong; removed in the C2 execution work
    # (gap §6 Q10).
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
    description="All active pending orders (LIMIT/STOP/STOP-LIMIT), optionally filtered by symbol.",
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
    if from_dt is None or to_dt is None or to_dt < from_dt:
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
    if from_dt is None or to_dt is None or to_dt < from_dt:
        raise BridgeError(ErrorCode.VALIDATION, "Invalid history window", http_status=422)
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
    if from_dt is None or to_dt is None or to_dt < from_dt:
        raise BridgeError(ErrorCode.VALIDATION, "Invalid history window", http_status=422)
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

    # P3 v2: placement wrapper — a timed-out cancel is TRADE_UNVERIFIED
    # (the order may already be gone), never a bare timeout.
    result = ctx.driver.run_order_send(job, op="order_send_cancel", context={"ticket": params.ticket, "symbol": symbol})
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


def cancel_order_by_ticket(ctx: BridgeContext, ticket: int) -> dict[str, Any]:
    """v2 ``cancel_order`` entry point (P3 §14.2).

    A typed alias over :func:`trade_cancel_pending_order`: the
    Platform's ``cancel_order`` op carries the pending-order ticket,
    exactly what the local cancel takes. Kept separate so the v2
    command path names the operation it executes, and so the
    reconcile path can distinguish a v2 cancel from a local cancel.
    """
    return trade_cancel_pending_order(ctx, TicketRequest(ticket=ticket))


def _modify_pending_landed(ctx: BridgeContext, ticket: int, price: float, sl: float, tp: float) -> bool:
    """Reconcile-before-retry: is the requested pending-order state
    already live in the broker book?

    A MODIFY sets absolute state (price/SL/TP), so the live order is
    conclusive evidence. Brokers normalize prices to tick size, so the
    comparison allows a half-tick tolerance (same rule as the command
    layer's SL/TP reconciliation); a failed lookup is "not landed",
    never an error here — the caller retries instead.
    """
    try:
        raw = _fetch_order(ctx, ticket)
        if raw is None:
            return False
        tol = 1e-9
        try:
            from .common import require_symbol_info

            spec = require_symbol_info(ctx, str(_f(raw, "symbol", "")))
            step = spec.trade_tick_size or spec.point
            if step is not None and float(step) > 0:
                tol = float(step) / 2.0 + 1e-9
        except Exception as exc:  # noqa: BLE001 — tolerance lookup is best-effort
            logger.debug("[MODIFY_PENDING] tick-size lookup failed, using 1e-9 tolerance: {}", type(exc).__name__)
        return (
            abs(float(_f(raw, "price_open", 0.0) or 0.0) - price) <= tol
            and abs(float(_f(raw, "sl", 0.0) or 0.0) - sl) <= tol
            and abs(float(_f(raw, "tp", 0.0) or 0.0) - tp) <= tol
        )
    except Exception:  # noqa: BLE001 — reconcile failure means "unknown", the caller retries
        return False


@command(
    "trade_modify_pending_order",
    number=62,
    category="orders",
    title="Modify Pending Order",
    description="Modify price/SL/TP/expiration of an active pending order (TRADE_ACTION_MODIFY). "
    "Volume is NOT changeable via modify (MQL5) — delete + replace instead.",
    request_model=ModifyPendingRequest,
    response_model=CancelResponse,
    mutates=True,
)
def trade_modify_pending_order(
    ctx: BridgeContext,
    params: ModifyPendingRequest,
    *,
    retry_policy: ExecutionRetryPolicy | None = None,
) -> dict[str, Any]:
    """Modify a pending order (price/SL/TP/expiration, TRADE_ACTION_MODIFY).

    W2: the exact request runs through the official ``order_check``
    first (``no_changes_ok`` — MODIFY checks report NO_CHANGES when the
    target state already holds, verified live E-OC-1). W4: a ``None``
    response or retcode 10031 raises TRADE_UNVERIFIED — the command
    layer reconciles against the live order; the request is never
    re-sent while the outcome is unknown.

    Retries (S4): only RETRYABLE retcodes are re-sent, with bounded
    exponential backoff. Before a retry, the live order is re-read:
    if the requested state already holds, the call returns done
    (``reconciled: True``) instead of re-sending — a timeout retcode
    may mean the broker applied the modify and the response was lost.
    Hard rejects fail fast on the first attempt.
    """
    policy = retry_policy or ExecutionRetryPolicy()
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
    from .trading import mt5_trade_constants

    consts = mt5_trade_constants(ctx)
    request: dict[str, Any] = {
        "action": consts["action_modify"],
        "order": int(params.ticket),
        "price": float(price),
        "sl": float(sl),
        "tp": float(tp),
    }
    if params.expiration_utc:
        exp = broker_dt(params.expiration_utc, ctx.settings)
        if exp is not None:
            request["type_time"] = consts["order_time_specified"]
            request["expiration"] = exp
    last_check: dict[str, Any] | None = None
    last_retcode: int | None = None
    for attempt in range(1, policy.max_attempts + 1):
        # W2: official pre-send check with the exact request.
        from .trading import order_check_send

        last_check = order_check_send(ctx, request, "modify_pending", no_changes_ok=True)

        def job(mt5: Any, request=request):
            return mt5.order_send(request)

        result = ctx.driver.run(job, op="order_send_modify", context={"ticket": params.ticket})
        retcode = int(_f(result, "retcode", 0)) if result is not None else None
        last_retcode = retcode
        if result is None or retcode == TRADE_RETCODE_CONNECTION:
            raise BridgeError(
                ErrorCode.TRADE_UNVERIFIED,
                f"Pending-order modify for #{params.ticket} returned "
                + ("no result" if result is None else "retcode 10031 (no connection)")
                + " — outcome unverified; reconcile the live order before retrying (never blind-retry).",
                details={
                    "ticket": params.ticket,
                    "retcode": retcode,
                    "attempts": attempt,
                    "order_check": last_check,
                },
            )
        if retcode == TRADE_RETCODE_NO_CHANGES:
            # Idempotent success (real-MT5 defect 1, same as modify_position_sltp):
            # the requested price/SL/TP already hold — return success, never reject.
            return {
                "status": "done",
                "ticket": params.ticket,
                "retcode": retcode,
                "retcode_label": translate_retcode(retcode),
                "no_changes": True,
                "attempts": attempt,
                "order_check": last_check,
            }
        if retcode == TRADE_RETCODE_DONE:
            return {
                "status": "done",
                "ticket": params.ticket,
                "retcode": retcode,
                "retcode_label": translate_retcode(retcode),
                "attempts": attempt,
                "order_check": last_check,
            }
        if retryable_retcode(retcode) and attempt < policy.max_attempts:
            # Reconcile-before-retry: the modify may have landed
            # despite the retcode (e.g. a timeout after apply).
            if _modify_pending_landed(ctx, params.ticket, price, sl, tp):
                logger.info(
                    "[MODIFY_PENDING] ticket={} target state already live after retcode={} — reconciled, not re-sent",
                    params.ticket,
                    retcode,
                )
                return {
                    "status": "done",
                    "ticket": params.ticket,
                    "retcode": retcode,
                    "retcode_label": translate_retcode(retcode),
                    "reconciled": True,
                    "attempts": attempt,
                    "order_check": last_check,
                }
            logger.warning(
                "[MODIFY_PENDING] ticket={} retcode={} ({}) — retryable, backing off before attempt {}/{}",
                params.ticket,
                retcode,
                retcode_class_label(retcode),
                attempt + 1,
                policy.max_attempts,
            )
            policy.backoff(attempt)
            continue
        # Fail fast: a hard reject / repairable request can never
        # succeed by re-sending, and the retry budget is spent.
        exc = reject(result, "modify_pending_order", symbol)
        exc.details["order_check"] = last_check
        exc.details["attempts"] = attempt
        raise exc
    # Unreachable: every loop iteration returns, raises, or continues
    # into the fail-fast raise — but mypy needs a terminus.
    raise BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"Pending-order modify failed for #{params.ticket}: retry budget exhausted "
        f"({translate_retcode(last_retcode or 0)})",
        retcode=last_retcode,
        details={"ticket": params.ticket, "attempts": policy.max_attempts, "order_check": last_check},
    )


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
        build_deal_model(d, offset).model_dump() for d in deals_raw if int(_f(d, "order", 0) or 0) == params.ticket
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
