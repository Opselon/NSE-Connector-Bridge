"""Trading core: order dispatch, ownership guards, retries, metrics.

Ported production semantics:
- retcode 0 / None result = IPC failure -> TRADE_UNVERIFIED (never blind-retry)
- non-DONE retcode -> TRADE_REJECTED with the official label
- close: ambiguous retcode re-checked against live positions (no duplicate close)
- filling mode resolved from the broker bitmask per symbol
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pydantic import BaseModel

from ..errors import BridgeError, ErrorCode
from ..mt5.types import f as _f
from ..retcodes import TRADE_RETCODE_DONE, translate_retcode
from .common import BridgeContext, resolve_filling_mode
from .symbols import _fresh_tick


def order_send_checked(ctx: BridgeContext, request: dict[str, Any], op: str) -> Any:
    """order_send with metrics + unverified-vs-rejected semantics."""
    ctx.driver.ensure_connected()
    t0 = time.perf_counter()

    def job(mt5: Any):
        return mt5.order_send(request)

    raw = ctx.driver.run(job, op=op, context={"symbol": request.get("symbol"), "action": request.get("action")})
    ms = (time.perf_counter() - t0) * 1000.0
    if raw is None:
        # No result object at all: outcome unobserved — never treated as rejection.
        ctx.trade_metrics.record(op, "unknown", ms, None, {"symbol": request.get("symbol")})
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — broker state unverified. "
            "Reconcile (positions/orders) before retrying.",
            details={"op": op, "symbol": request.get("symbol")},
        )
    retcode = int(_f(raw, "retcode", 0))
    outcome = "done" if retcode == TRADE_RETCODE_DONE else "rejected"
    ctx.trade_metrics.record(
        op, outcome, ms, retcode,
        {"symbol": request.get("symbol"), "order": int(_f(raw, "order", 0) or 0)},
    )
    return raw


def fetch_positions(ctx: BridgeContext, symbol: str | None = None) -> list[Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()

    raw = ctx.driver.run(job, op="positions_get", context={"symbol": symbol} if symbol else None)
    if raw is None:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            "positions_get() failed — broker state unverified (not 'no positions').",
            details={"symbol": symbol},
        )
    return list(raw)


def fetch_position(ctx: BridgeContext, ticket: int) -> Any:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.positions_get(ticket=ticket)

    raw = ctx.driver.run(job, op="positions_get", context={"ticket": ticket})
    if not raw:
        raise BridgeError(
            ErrorCode.TICKET_NOT_FOUND,
            f"Position #{ticket} not found (closed or unknown ticket).",
            details={"ticket": ticket},
        )
    return raw[0]


def _position_owns(ctx: BridgeContext, raw: Any) -> bool:
    """strict_magic_guard mode: only touch positions carrying our magic.

    The guard applies ONLY when a magic IS configured. With no magic
    configured (``settings.magic is None`` — the default since the
    client-only refactor removed the hardcoded magic) there is no
    bridge identity to compare against, so the guard passes.
    """
    if not ctx.settings.strict_magic_guard or ctx.settings.magic is None:
        return True
    return int(_f(raw, "magic", -1)) == int(ctx.settings.magic)


class TradeExecutionResponse(BaseModel):
    status: str  # done
    symbol: str
    order_ticket: int | None = None
    deal_ticket: int | None = None
    retcode: int
    retcode_label: str | None = None
    price: float | None = None
    volume: float | None = None
    comment: str | None = None
    ambiguous_fill_recovered: bool = False
    order: dict[str, Any] | None = None  # pending orders: full placed-order snapshot
    ticket: int | None = None  # closes: the closed position ticket
    closed_volume: float | None = None


def _execution_payload(raw: Any, symbol: str, *, recovered: bool = False) -> dict[str, Any]:
    retcode = int(_f(raw, "retcode", 0))
    return {
        "status": "done" if retcode == TRADE_RETCODE_DONE else "unknown",
        "symbol": symbol,
        "order_ticket": int(_f(raw, "order", 0) or 0) or None,
        "deal_ticket": int(_f(raw, "deal", 0) or 0) or None,
        "retcode": retcode,
        "retcode_label": translate_retcode(retcode),
        "price": _f(raw, "price"),
        "volume": _f(raw, "volume"),
        "comment": _f(raw, "comment"),
        "ambiguous_fill_recovered": recovered,
    }


def reject(raw: Any, op: str, symbol: str) -> BridgeError:
    retcode = int(_f(raw, "retcode", 0))
    return BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"{op} rejected by broker: {translate_retcode(retcode)}",
        retcode=retcode,
        details={"op": op, "symbol": symbol, "comment": _f(raw, "comment")},
    )


def market_order(
    ctx: BridgeContext,
    *,
    symbol: str,
    side: str,
    volume: float,
    sl: float = 0.0,
    tp: float = 0.0,
    deviation: int = 20,
    comment: str = "NSE_MARKET",
    magic: int | None = None,
) -> dict[str, Any]:
    """Market order with ambiguous-fill recovery (ported execute_market_order)."""
    symbol = symbol.strip()
    ctx.driver.ensure_connected()
    tick = _fresh_tick(ctx, symbol)
    filling = resolve_filling_mode(ctx, symbol)
    # No hardcoded magic: per-command magic wins, else the explicitly
    # configured device magic, else None = no magic stamped (the
    # "magic" key is omitted, so MT5 records magic 0). The ambiguous-
    # fill recovery fingerprint compares against 0 in that case,
    # because that is the magic MT5 assigns to an unstamped order.
    mgc = magic if magic is not None else ctx.settings.magic
    mgc_for_match = mgc if mgc is not None else 0

    def job(mt5: Any):
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask) if side == "buy" else float(tick.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": int(deviation),
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        if mgc is not None:
            request["magic"] = int(mgc)
        return mt5.order_send(request)

    raw = ctx.driver.run(job, op="order_send", context={"symbol": symbol, "side": side, "volume": volume})
    ms_probe = 0.0  # metrics handled inside order_send paths below

    if raw is None:
        ctx.trade_metrics.record("order_send", "unknown", ms_probe, None, {"symbol": symbol, "side": side})
        # ambiguous fill recovery: verify against broker truth before failing
        recovered = _recover_ambiguous(ctx, symbol, side, mgc_for_match)
        if recovered is not None:
            ctx.trade_metrics.record("order_send", "done", ms_probe, TRADE_RETCODE_DONE, {"symbol": symbol, "recovered": True})
            return _execution_payload(recovered, symbol, recovered=True)
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — reconcile broker state before retrying.",
            details={"symbol": symbol, "side": side, "volume": volume},
        )
    retcode = int(_f(raw, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        ctx.trade_metrics.record("order_send", "rejected", ms_probe, retcode, {"symbol": symbol, "side": side})
        recovered = _recover_ambiguous(ctx, symbol, side, mgc_for_match)
        if recovered is not None:
            logger.warning(
                "[ORDER_WRITE] retcode={} but live position found (ticket={}) — ambiguous fill treated as success",
                retcode, int(_f(recovered, "ticket", 0)),
            )
            ctx.trade_metrics.record("order_send", "done", ms_probe, retcode, {"symbol": symbol, "recovered": True})
            return _execution_payload(recovered, symbol, recovered=True)
        raise reject(raw, "market_order", symbol)
    ctx.trade_metrics.record("order_send", "done", ms_probe, retcode, {"symbol": symbol, "side": side})
    return _execution_payload(raw, symbol)


def _recover_ambiguous(ctx: BridgeContext, symbol: str, side: str, magic: int) -> Any | None:
    """Look for a just-opened live position matching the fingerprint (no blind retry)."""
    want = 0 if side == "buy" else 1  # ORDER_TYPE_BUY / SELL
    cutoff = time.time() - 30.0
    try:
        for raw in fetch_positions(ctx, symbol):
            if int(_f(raw, "type", -1)) != want:
                continue
            if int(_f(raw, "magic", -1)) != magic:
                continue
            opened = float(_f(raw, "time", 0) or 0)
            if opened and opened >= cutoff:
                return raw
    except BridgeError:
        return None
    return None


def modify_position_sltp(ctx: BridgeContext, ticket: int, sl: float, tp: float) -> dict[str, Any]:
    """TRADE_ACTION_SLTP with retries + ownership guard (ported modify_position)."""
    ctx.driver.ensure_connected()
    last: Any = None
    for attempt in range(1, 4):
        pos = fetch_position(ctx, ticket)
        if not _position_owns(ctx, pos):
            raise BridgeError(
                ErrorCode.TRADE_REJECTED,
                f"Refusing to modify #{ticket}: owned by another strategy "
                f"(magic={_f(pos, 'magic')} ours={ctx.settings.magic}) and strict_magic_guard is on.",
                details={"ticket": ticket},
            )
        symbol = str(_f(pos, "symbol", ""))
        # Validates the symbol/connection; TRADE_ACTION_SLTP carries no
        # filling mode, so the resolved value is intentionally unused.
        resolve_filling_mode(ctx, symbol)

        def job(mt5: Any, symbol=symbol, ticket=ticket, sl=sl, tp=tp):
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": symbol,
                "sl": float(sl),
                "tp": float(tp),
            })

        last = ctx.driver.run(job, op="order_send_sltp", context={"ticket": ticket, "sl": sl, "tp": tp})
        if last is None:
            continue
        retcode = int(_f(last, "retcode", 0))
        if retcode == TRADE_RETCODE_DONE:
            ctx.trade_metrics.record("modify_sltp", "done", 0.0, retcode, {"ticket": ticket})
            return {
                "status": "done",
                "ticket": ticket,
                "symbol": symbol,
                "sl": sl,
                "tp": tp,
                "retcode": retcode,
                "retcode_label": translate_retcode(retcode),
            }
        time.sleep(0.025)
    retcode = int(_f(last, "retcode", 0)) if last is not None else 0
    ctx.trade_metrics.record("modify_sltp", "rejected", 0.0, retcode, {"ticket": ticket})
    raise BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"SL/TP modification failed for #{ticket} after 3 attempts: {translate_retcode(retcode)}",
        retcode=retcode if retcode else None,
        details={"ticket": ticket, "sl": sl, "tp": tp},
    )


def modify_pending_order_sltp(ctx: BridgeContext, ticket: int, sl: float, tp: float) -> dict[str, Any]:
    """Modify SL/TP of a pending order (contract §17.4 ``modify_pending``).

    SL/TP ONLY — price/volume/expiration changes have no verified
    action and are out of scope (§17.4). The pending order's price and
    volume are preserved from the live order; the dispatch reuses the
    proven local ``trade_modify_pending_order`` request shape
    (``TRADE_ACTION_MODIFY`` with the order ticket).

    CAPABILITY GATE (§17.4): this function is reachable ONLY through
    the server command path after the ``modify_pending`` gate passes
    (:class:`nse_bridge.server.commands.CommandValidator` with
    ``modify_pending_proven=True``). The gate is closed until the
    §17.4 close-out (live proof at market open + published minimum
    connector_version) is complete — calling this from anywhere else
    bypasses that gate.
    """
    # Deferred import: orders.py imports this module at top level.
    from .orders import ModifyPendingRequest, trade_modify_pending_order

    return trade_modify_pending_order(
        ctx, ModifyPendingRequest(ticket=ticket, sl=sl, tp=tp)
    )


def close_position(ctx: BridgeContext, ticket: int, volume: float | None = None) -> dict[str, Any]:
    """Close (fully or partially) with idempotent ambiguous-close handling (ported)."""
    ctx.driver.ensure_connected()
    last: Any = None
    for attempt in range(1, 4):
        pos_raw = fetch_position(ctx, ticket)
        if not _position_owns(ctx, pos_raw):
            raise BridgeError(
                ErrorCode.TRADE_REJECTED,
                f"Refusing to close #{ticket}: foreign magic and strict_magic_guard is on.",
                details={"ticket": ticket},
            )
        pos_type = int(_f(pos_raw, "type", -1))
        symbol = str(_f(pos_raw, "symbol", ""))
        pos_volume = float(_f(pos_raw, "volume", 0.0))
        close_volume = float(volume) if volume is not None else pos_volume
        if close_volume > pos_volume + 1e-9:
            raise BridgeError(
                ErrorCode.VALIDATION,
                f"Close volume {close_volume} exceeds position volume {pos_volume}.",
                http_status=422,
                details={"ticket": ticket, "position_volume": pos_volume},
            )
        tick = _fresh_tick(ctx, symbol)
        filling = resolve_filling_mode(ctx, symbol)

        def job(  # loop variables bound as defaults (B023): the job runs synchronously
            mt5: Any,
            pos_type=pos_type,
            tick=tick,
            close_volume=close_volume,
            symbol=symbol,
            pos_raw=pos_raw,
            filling=filling,
            ticket=ticket,
        ):
            close_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
            price = float(tick.bid) if pos_type == 0 else float(tick.ask)
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": close_volume,
                "type": close_type,
                "position": int(ticket),
                "price": price,
                "magic": int(_f(pos_raw, "magic", 0) or 0),
                "comment": "NSE_CLOSE",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            })

        last = ctx.driver.run(job, op="order_send_close", context={"ticket": ticket, "volume": close_volume})
        if last is None:
            continue
        retcode = int(_f(last, "retcode", 0))
        if retcode == TRADE_RETCODE_DONE:
            ctx.trade_metrics.record("close_position", "done", 0.0, retcode, {"ticket": ticket})
            return _execution_payload(last, symbol) | {
                "status": "done",
                "ticket": ticket,
                "closed_volume": close_volume,
            }
        if attempt < 3:
            # idempotency guard: accepted server-side despite ambiguous retcode?
            try:
                fetch_position(ctx, ticket)
            except BridgeError:
                logger.info(
                    "[CLOSE] ticket={} no longer open after ambiguous retcode={} — treated as closed",
                    ticket, retcode,
                )
                ctx.trade_metrics.record("close_position", "done", 0.0, retcode, {"ticket": ticket, "recovered": True})
                return {
                    "status": "done",
                    "ticket": ticket,
                    "symbol": symbol,
                    "closed_volume": close_volume,
                    "retcode": retcode,
                    "retcode_label": translate_retcode(retcode),
                    "ambiguous_fill_recovered": True,
                }
            time.sleep(0.025)
    retcode = int(_f(last, "retcode", 0)) if last is not None else 0
    ctx.trade_metrics.record("close_position", "rejected", 0.0, retcode, {"ticket": ticket})
    raise BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"Close failed for #{ticket} after 3 attempts: {translate_retcode(retcode)}",
        retcode=retcode if retcode else None,
        details={"ticket": ticket},
    )
