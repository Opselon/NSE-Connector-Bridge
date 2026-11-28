"""Trading core: order dispatch, ownership guards, retries, metrics.

Ported production semantics:
- retcode 0 / None result = IPC failure -> TRADE_UNVERIFIED (never blind-retry)
- non-DONE retcode -> TRADE_REJECTED with the official label
- close: ambiguous retcode re-checked against live positions (no duplicate close)
- filling mode resolved from the broker bitmask per symbol

Phase 2b execution-safety additions (W2/W4):

- Official pre-send ``order_check`` (CONTRACTS.md §7, DESIGN.md §3.8):
  every financial send in the gateway execution path first runs the
  official ``mt5.order_check`` with the EXACT request on the serialized
  worker, preserves the raw check result, and refuses the send on a
  failed check. A passing check is never a guarantee of execution.
- Uncertain outcomes (a ``None`` response or retcode 10031 CONNECTION)
  never prove non-execution: they reconcile against live broker state
  and are never re-sent. A volume-bearing re-send could double-fill a
  partial close or duplicate an open — so the send loop stops instead.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pydantic import BaseModel

from ..errors import BridgeError, ErrorCode
from ..mt5.types import f as _f
from ..retcodes import (
    ORDER_CHECK_RETCODE_OK,
    TRADE_RETCODE_CONNECTION,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_DONE_PARTIAL,
    TRADE_RETCODE_NO_CHANGES,
    TRADE_RETCODE_POSITION_CLOSED,
    translate_retcode,
)
from .common import BridgeContext, account_info_model, require_symbol_info, resolve_filling_mode, resolve_order_symbol
from .retry import ExecutionRetryPolicy, retcode_class_label, retryable_retcode
from .symbols import _fresh_tick

# ENUM_ACCOUNT_MARGIN_MODE (verified against the package constants in
# E-OC-1, scripts/phase2b_probe_order_check.py): 0=RETAIL_NETTING,
# 1=EXCHANGE, 2=RETAIL_HEDGING. Close-by is a hedging-account
# operation (real-MT5 defect D5: the old check compared against 1,
# wrongly refusing real hedging accounts — including the MetaQuotes
# Demo account used for Phase 2b verification).
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


def mt5_trade_constants(ctx: BridgeContext) -> dict[str, int]:
    """Resolve the MT5 action/type constants on the worker thread.

    One cheap IPC hop per financial send keeps the request dict
    canonical against the ACTUAL loaded package (never a hardcoded
    guess) while leaving it buildable outside the worker so the exact
    request can go through :func:`order_check_send` first (W2 step 4).

    P1-4: the constants are per-package, not per-call — the first
    fetch is cached on the context and reused until the driver is
    replaced or the terminal reconnects (``connection_epoch`` bump);
    a copy is returned so callers can never mutate the cached dict.
    """
    ctx.driver.ensure_connected()
    cached = ctx.ipc_cache.get_constants(ctx.driver)
    if cached is not None:
        return cached

    def job(mt5: Any):
        return {
            "action_deal": int(mt5.TRADE_ACTION_DEAL),
            "action_pending": int(mt5.TRADE_ACTION_PENDING),
            "action_sltp": int(mt5.TRADE_ACTION_SLTP),
            "action_modify": int(mt5.TRADE_ACTION_MODIFY),
            "order_type_buy": int(mt5.ORDER_TYPE_BUY),
            "order_type_sell": int(mt5.ORDER_TYPE_SELL),
            "order_time_gtc": int(mt5.ORDER_TIME_GTC),
            "order_time_specified": int(mt5.ORDER_TIME_SPECIFIED),
        }

    consts = ctx.driver.run(job, op="mt5_constants")
    ctx.ipc_cache.set_constants(ctx.driver, consts)
    return dict(consts)


def _check_result_dict(raw: Any) -> Any:
    """JSON-safe conversion of an order_check result (nested namedtuples,
    SimpleNamespace-style objects, dicts, lists, scalars)."""
    if hasattr(raw, "_asdict"):
        return {key: _check_result_dict(value) for key, value in raw._asdict().items()}
    if isinstance(raw, dict):
        return {key: _check_result_dict(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_check_result_dict(value) for value in raw]
    d = getattr(raw, "__dict__", None)
    if isinstance(d, dict):
        return {key: _check_result_dict(value) for key, value in d.items()}
    return raw


def order_check_send(
    ctx: BridgeContext,
    request: dict[str, Any],
    op: str,
    *,
    no_changes_ok: bool = False,
) -> dict[str, Any]:
    """Official MT5 ``order_check`` pre-send validation (W2, CONTRACTS §7).

    Runs the EXACT request that is about to be sent through the
    serialized worker — the official broker check, never a locally
    invented approximation. Semantics (all verified live, E-OC-1):

    - retcode 0 → check passed (validity + post-trade margin preview).
    - retcode 10025 (NO_CHANGES) with ``no_changes_ok`` → pass: the
      request is valid and the target state already holds (idempotent
      SLTP/MODIFY sends report this instead of a plain pass).
    - anything else → REFUSAL: the caller must not call ``order_send``;
      a BridgeError with code ``ORDER_CHECK_FAILED`` carries the raw
      check result.
    - ``None`` → the check itself is unavailable: fail closed, never
      send unverified.

    A passing check is a preview at check time only — requotes,
    disconnects and market moves can still fail the send (CONTRACTS §7).
    """
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.order_check(request)

    raw = ctx.driver.run(
        job,
        op="order_check",
        context={"op": op, "symbol": request.get("symbol"), "action": request.get("action")},
    )
    symbol = str(request.get("symbol") or "")
    if raw is None:
        raise BridgeError(
            ErrorCode.ORDER_CHECK_FAILED,
            f"order_check returned no result — refusing to send an unverified {op} request (fail closed).",
            details={"op": op, "symbol": symbol},
        )
    retcode = int(_f(raw, "retcode", 0))
    if retcode != ORDER_CHECK_RETCODE_OK and not (no_changes_ok and retcode == TRADE_RETCODE_NO_CHANGES):
        raise BridgeError(
            ErrorCode.ORDER_CHECK_FAILED,
            f"order_check refused the {op} request: {translate_retcode(retcode)} — order_send not reached.",
            retcode=retcode,
            details={"op": op, "symbol": symbol, "order_check": _check_result_dict(raw)},
        )
    check = _check_result_dict(raw)
    ctx.trade_metrics.record("order_check", "done", 0.0, retcode, {"op": op, "symbol": symbol})
    return check


def _unverified(op: str, message: str, details: dict[str, Any], retcode: int | None = None) -> BridgeError:
    """TRADE_UNVERIFIED with the broker retcode attached when known
    (audit P2-5) so ``_finish_error`` can surface it in the
    ``action.result`` error object instead of only in ``details``."""
    return BridgeError(ErrorCode.TRADE_UNVERIFIED, message, retcode=retcode, details=details)


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
        raise _unverified(
            op,
            "order_send returned no result — broker state unverified. Reconcile (positions/orders) before retrying.",
            {"op": op, "symbol": request.get("symbol")},
        )
    retcode = int(_f(raw, "retcode", 0))
    outcome = "done" if retcode == TRADE_RETCODE_DONE else "rejected"
    ctx.trade_metrics.record(
        op,
        outcome,
        ms,
        retcode,
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
    """Market order with ambiguous-fill recovery (ported execute_market_order).

    W2: the exact request goes through the official ``order_check``
    first; a failed check raises ``ORDER_CHECK_FAILED`` and
    ``order_send`` is never reached. W4: a ``None`` response or retcode
    10031 reconciles against live positions (fingerprint match) and is
    never re-sent — an open must not duplicate.
    """
    symbol = symbol.strip()
    ctx.driver.ensure_connected()
    # Canonical ``BASE/QUOTE`` spellings resolve to the exact broker
    # symbol here (spec §4); exact broker names pass through untouched.
    symbol = resolve_order_symbol(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    # A3: a tick with a missing/zero price must never reach the broker
    # as a zero-price order — fail closed here, before dispatch.
    price = float(tick.ask) if side == "buy" else float(tick.bid)
    if not price > 0:
        raise BridgeError(
            ErrorCode.MT5_CALL_FAILED,
            f"No usable {side} price for '{symbol}' (tick {side} is {price}) — refusing to send a zero-price order.",
            details={"symbol": symbol, "side": side, "price": price},
        )
    filling = resolve_filling_mode(ctx, symbol)
    # No hardcoded magic: per-command magic wins, else the explicitly
    # configured device magic, else None = no magic stamped (the
    # "magic" key is omitted, so MT5 records magic 0). The ambiguous-
    # fill recovery fingerprint compares against 0 in that case,
    # because that is the magic MT5 assigns to an unstamped order.
    mgc = magic if magic is not None else ctx.settings.magic
    mgc_for_match = mgc if mgc is not None else 0
    consts = mt5_trade_constants(ctx)
    request: dict[str, Any] = {
        "action": consts["action_deal"],
        "symbol": symbol,
        "volume": float(volume),
        "type": consts["order_type_buy"] if side == "buy" else consts["order_type_sell"],
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": int(deviation),
        "comment": comment,
        "type_time": consts["order_time_gtc"],
        "type_filling": filling,
    }
    if mgc is not None:
        request["magic"] = int(mgc)
    # W2 step 5: the official pre-send check with the exact request.
    check = order_check_send(ctx, request, "market_order")

    def job(mt5: Any, request=request):  # bound at definition time (B023)
        return mt5.order_send(request)

    raw = ctx.driver.run(job, op="order_send", context={"symbol": symbol, "side": side, "volume": volume})
    ms_probe = 0.0  # metrics handled inside order_send paths below

    if raw is None or int(_f(raw, "retcode", 0)) == TRADE_RETCODE_CONNECTION:
        # W4: a lost response or retcode 10031 does NOT prove the order
        # was not executed — recover from live broker truth, never re-send.
        outcome: str = "unknown" if raw is None else "rejected"
        ctx.trade_metrics.record(
            "order_send",
            outcome,
            ms_probe,
            None if raw is None else TRADE_RETCODE_CONNECTION,
            {"symbol": symbol, "side": side},
        )
        recovered = _recover_ambiguous(ctx, symbol, side, mgc_for_match)
        if recovered is not None:
            logger.warning(
                "[ORDER_WRITE] uncertain order_send outcome ({}), but live position found (ticket={}) — "
                "reconciled as executed without re-sending",
                "None" if raw is None else "retcode 10031",
                int(_f(recovered, "ticket", 0)),
            )
            ctx.trade_metrics.record(
                "order_send", "done", ms_probe, TRADE_RETCODE_DONE, {"symbol": symbol, "recovered": True}
            )
            payload = _execution_payload(recovered, symbol, recovered=True)
            payload["order_check"] = check
            return payload
        raise _unverified(
            "market_order",
            "order_send returned "
            + ("no result" if raw is None else "retcode 10031 (no connection)")
            + " — outcome unverified. Reconcile (positions) before retrying; the open was never re-sent.",
            {
                "op": "market_order",
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "retcode": None if raw is None else TRADE_RETCODE_CONNECTION,
                "order_check": check,
            },
            retcode=None if raw is None else TRADE_RETCODE_CONNECTION,
        )
    retcode = int(_f(raw, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        ctx.trade_metrics.record("order_send", "rejected", ms_probe, retcode, {"symbol": symbol, "side": side})
        recovered = _recover_ambiguous(ctx, symbol, side, mgc_for_match)
        if recovered is not None:
            logger.warning(
                "[ORDER_WRITE] retcode={} but live position found (ticket={}) — ambiguous fill treated as success",
                retcode,
                int(_f(recovered, "ticket", 0)),
            )
            ctx.trade_metrics.record("order_send", "done", ms_probe, retcode, {"symbol": symbol, "recovered": True})
            payload = _execution_payload(recovered, symbol, recovered=True)
            payload["order_check"] = check
            return payload
        exc = reject(raw, "market_order", symbol)
        exc.details["order_check"] = check
        raise exc
    ctx.trade_metrics.record("order_send", "done", ms_probe, retcode, {"symbol": symbol, "side": side})
    payload = _execution_payload(raw, symbol)
    payload["order_check"] = check
    return payload


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


def validate_modify_sltp_geometry(
    ctx: BridgeContext,
    *,
    symbol: str,
    is_long: bool,
    sl: float,
    tp: float,
) -> list[str]:
    """Pre-send SL/TP geometry for a position modify, against the CURRENT market (audit P2-4).

    The requested stops must sit on the correct side of the live
    price for the position's direction — SL below / TP above the
    closing-side tick on a long (bid), the mirror on a short (ask) —
    each at least the stops-level distance away. A leg of ``0.0``
    means "no stop / no take-profit" and is not checked; a negative
    leg is never valid (it passes ``parse_envelope`` unchecked).

    Mirrors :func:`nse_bridge.engine.execution.validate_market_open_params`'s
    min-distance math (broker stops level, 10-point safety floor),
    but the reference price is the CLOSE price (bid closes a long,
    ask closes a short), not the entry price — the old
    entry-only view is exactly what let a wrong-side modify through
    to a broker 10030 refusal. Returns a list of reason codes;
    empty means the request is structurally sound. The official
    ``order_check`` inside :func:`modify_position_sltp` remains the
    broker acceptance check — this pre-flight only fails fast on
    requests the broker cannot accept.

    Fail-closed: a missing symbol or an unavailable tick raises
    (``SYMBOL_NOT_FOUND`` / ``MT5_CALL_FAILED``) instead of
    returning an empty list — no unchecked modify is ever sent.
    """
    spec = require_symbol_info(ctx, symbol)
    tick = _fresh_tick(ctx, symbol)
    reasons: list[str] = []
    point = float(spec.point or 0.0)
    stops_level = float(spec.trade_stops_level or 0)
    safety_floor = 10 * point  # 10 broker points floor (ported)
    min_stop_dist = max(stops_level * point, safety_floor)

    price = float(tick.bid or 0) if is_long else float(tick.ask or 0)
    if not price > 0:
        reasons.append("no_live_price")
        return reasons
    if sl < 0:
        reasons.append("sl_negative")
    if tp < 0:
        reasons.append("tp_negative")
    if sl > 0:
        if is_long and sl >= price - min_stop_dist + 1e-9:
            reasons.append("sl_not_below_price")
        if not is_long and sl <= price + min_stop_dist - 1e-9:
            reasons.append("sl_not_above_price")
    if tp > 0:
        if is_long and tp <= price + min_stop_dist - 1e-9:
            reasons.append("tp_not_above_price")
        if not is_long and tp >= price - min_stop_dist + 1e-9:
            reasons.append("tp_not_below_price")
    if sl > 0 and tp > 0:
        if is_long and sl >= tp:
            reasons.append("sl_not_below_tp")
        if not is_long and sl <= tp:
            reasons.append("sl_not_above_tp")
    return reasons


def modify_position_sltp(
    ctx: BridgeContext,
    ticket: int,
    sl: float,
    tp: float,
    *,
    retry_policy: ExecutionRetryPolicy | None = None,
) -> dict[str, Any]:
    """TRADE_ACTION_SLTP with pre-send order_check + ownership guard (ported modify_position).

    W2: the exact request runs through the official ``order_check``
    first (``no_changes_ok``: SLTP checks report NO_CHANGES when the
    target state already holds — verified live, E-OC-1). W4: a ``None``
    response or retcode 10031 raises TRADE_UNVERIFIED — the command
    layer reconciles against the live SL/TP; the request is NEVER
    re-sent while the outcome is unknown (TRADE_ACTION_SLTP sets an
    absolute state, so the reconciliation read is conclusive).

    Retries (S4): only RETRYABLE retcodes (requote / timeout /
    price-changed / price-off / too-many-requests) are re-sent, with
    bounded exponential backoff from ``retry_policy``. Hard rejects
    and repairable requests fail fast on the first attempt — re-sending
    them could never succeed. Every attempt re-fetches the live
    position and re-runs ``order_check`` (fresh state per attempt),
    and the payload reports ``attempts``.

    P1-4: the filling mode + trade constants are cached per symbol on
    the context via the shared helpers (TTL + reconnect invalidation)
    — no code change is needed here, and the per-attempt position
    fetch + ``order_check`` above are unchanged.

    P2-4: before ``order_check``, the requested SL/TP run through
    :func:`validate_modify_sltp_geometry` against the live tick for
    the position's direction (SL below / TP above the bid on a long,
    the mirror on a short). A wrong-side modify is a hard
    ``VALIDATION`` reject on the first attempt — never retried, never
    sent — instead of a broker 10030 refusal after fencing. A
    missing tick/symbol fails closed the same way.
    """
    ctx.driver.ensure_connected()
    policy = retry_policy or ExecutionRetryPolicy()
    last: Any = None
    last_check: dict[str, Any] | None = None
    last_retcode: int | None = None
    for attempt in range(1, policy.max_attempts + 1):
        pos = fetch_position(ctx, ticket)
        if not _position_owns(ctx, pos):
            raise BridgeError(
                ErrorCode.TRADE_REJECTED,
                f"Refusing to modify #{ticket}: owned by another strategy "
                f"(magic={_f(pos, 'magic')} ours={ctx.settings.magic}) and strict_magic_guard is on.",
                details={"ticket": ticket},
            )
        symbol = str(_f(pos, "symbol", ""))
        reasons = validate_modify_sltp_geometry(
            ctx, symbol=symbol, is_long=int(_f(pos, "type", -1)) == 0, sl=float(sl), tp=float(tp)
        )
        if reasons:
            raise BridgeError(
                ErrorCode.VALIDATION,
                f"Refusing to modify #{ticket}: SL/TP geometry invalid against the current market "
                f"({', '.join(reasons)}).",
                http_status=422,
                details={"ticket": ticket, "symbol": symbol, "sl": sl, "tp": tp, "reasons": reasons},
            )
        # Validates the symbol/connection; TRADE_ACTION_SLTP carries no
        # filling mode, so the resolved value is intentionally unused.
        resolve_filling_mode(ctx, symbol)
        consts = mt5_trade_constants(ctx)
        request: dict[str, Any] = {
            "action": consts["action_sltp"],
            "position": int(ticket),
            "symbol": symbol,
            "sl": float(sl),
            "tp": float(tp),
        }
        last_check = order_check_send(ctx, request, "modify_sl_tp", no_changes_ok=True)

        def job(mt5: Any, request=request):
            return mt5.order_send(request)

        last = ctx.driver.run(job, op="order_send_sltp", context={"ticket": ticket, "sl": sl, "tp": tp})
        retcode = int(_f(last, "retcode", 0)) if last is not None else None
        last_retcode = retcode
        if last is None or retcode == TRADE_RETCODE_CONNECTION:
            # W4: outcome unknown — stop sending; the command layer
            # reconciles against the live SL/TP before anything else.
            ctx.trade_metrics.record(
                "modify_sltp", "unknown", 0.0, retcode, {"ticket": ticket, "uncertain": True, "attempts": attempt}
            )
            raise _unverified(
                "modify_sl_tp",
                f"SL/TP modification for #{ticket} returned "
                + ("no result" if last is None else "retcode 10031 (no connection)")
                + " — outcome unverified; reconcile the live SL/TP before retrying (never blind-retry).",
                {
                    "ticket": ticket,
                    "sl": sl,
                    "tp": tp,
                    "retcode": retcode,
                    "attempts": attempt,
                    "order_check": last_check,
                },
                retcode=retcode,
            )
        if retcode == TRADE_RETCODE_NO_CHANGES:
            # Idempotent success (real-MT5 defect 1): the requested SL/TP
            # already hold — the desired state is in effect, so there is
            # nothing to retry. The old code retried the identical
            # request 3 times (NO_CHANGES every time) and then failed
            # the call even though the broker state was correct.
            ctx.trade_metrics.record(
                "modify_sltp", "done", 0.0, retcode, {"ticket": ticket, "no_changes": True, "attempts": attempt}
            )
            return {
                "status": "done",
                "ticket": ticket,
                "symbol": symbol,
                "sl": sl,
                "tp": tp,
                "retcode": retcode,
                "retcode_label": translate_retcode(retcode),
                "no_changes": True,
                "attempts": attempt,
                "order_check": last_check,
            }
        if retcode == TRADE_RETCODE_DONE:
            ctx.trade_metrics.record("modify_sltp", "done", 0.0, retcode, {"ticket": ticket, "attempts": attempt})
            return {
                "status": "done",
                "ticket": ticket,
                "symbol": symbol,
                "sl": sl,
                "tp": tp,
                "retcode": retcode,
                "retcode_label": translate_retcode(retcode),
                "attempts": attempt,
                "order_check": last_check,
            }
        if retryable_retcode(retcode) and attempt < policy.max_attempts:
            logger.warning(
                "[MODIFY_SLTP] ticket={} retcode={} ({}) — retryable, backing off before attempt {}/{}",
                ticket,
                retcode,
                retcode_class_label(retcode),
                attempt + 1,
                policy.max_attempts,
            )
            policy.backoff(attempt)
            continue
        # Fail fast: a hard reject / repairable request can never
        # succeed by re-sending, and the retry budget is spent.
        ctx.trade_metrics.record("modify_sltp", "rejected", 0.0, retcode, {"ticket": ticket, "attempts": attempt})
        raise BridgeError(
            ErrorCode.TRADE_REJECTED,
            f"SL/TP modification failed for #{ticket} after {attempt} attempt(s): {translate_retcode(retcode or 0)}",
            retcode=retcode,
            details={"ticket": ticket, "sl": sl, "tp": tp, "attempts": attempt, "order_check": last_check},
        )
    # Unreachable: every loop iteration returns, raises, or continues
    # into the fail-fast raise — but mypy needs a terminus.
    raise BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"SL/TP modification failed for #{ticket}: retry budget exhausted ({translate_retcode(last_retcode or 0)})",
        retcode=last_retcode,
        details={"ticket": ticket, "sl": sl, "tp": tp, "attempts": policy.max_attempts, "order_check": last_check},
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

    return trade_modify_pending_order(ctx, ModifyPendingRequest(ticket=ticket, sl=sl, tp=tp))


def _reconcile_close_volume(ctx: BridgeContext, ticket: int) -> tuple[float | None, bool]:
    """Re-read a position after a (partial) close.

    Returns ``(remaining_volume, reconciled)``: ``remaining_volume`` is
    None when the position is gone (fully closed) or the re-read failed;
    ``reconciled`` is False only in the latter case. The caller computes
    the actually-closed volume from the pre-close volume.
    """
    try:
        current = fetch_position(ctx, ticket)
    except BridgeError as exc:
        if exc.code is ErrorCode.TICKET_NOT_FOUND:
            return None, True  # position gone: fully closed
        return None, False
    return float(_f(current, "volume", 0.0) or 0.0), True


def _reconcile_uncertain_close(
    ctx: BridgeContext, ticket: int, pre_volume: float, symbol: str
) -> dict[str, Any] | None:
    """W4: reconcile a lost/uncertain close against live broker truth.

    Returns the truthful payload when the outcome is established from
    broker state (position gone → fully closed; volume reduced → the
    actual partial fill), ``None`` when it remains unknown. Never
    re-sends the close — a re-send could double-fill a partial close.
    """
    try:
        current = fetch_position(ctx, ticket)
    except BridgeError as exc:
        if exc.code is ErrorCode.TICKET_NOT_FOUND:
            return {
                "status": "done",
                "symbol": symbol,
                "ticket": ticket,
                "closed_volume": pre_volume,
                "remaining_volume": 0.0,
                "partial": False,
                "reconciled": True,
                "ambiguous_fill_recovered": True,
                "reconciliation": "position_gone",
            }
        return None  # re-read failed — outcome stays unknown
    remaining = float(_f(current, "volume", 0.0) or 0.0)
    closed = max(0.0, pre_volume - remaining)
    if closed <= 1e-9:
        return None  # nothing landed — the outcome is genuinely unknown
    return {
        "status": "done",
        "symbol": symbol,
        "ticket": ticket,
        "closed_volume": closed,
        "remaining_volume": remaining,
        "partial": remaining > 1e-9,
        "reconciled": True,
        "ambiguous_fill_recovered": True,
        "reconciliation": "volume_reduced",
    }


def close_position(
    ctx: BridgeContext,
    ticket: int,
    volume: float | None = None,
    *,
    retry_policy: ExecutionRetryPolicy | None = None,
) -> dict[str, Any]:
    """Close (fully or partially) with idempotent ambiguous-close handling (ported).

    Partial closes are reconciled against live broker state (spec §7):
    after a DONE / DONE_PARTIAL result the position is re-read and the
    payload carries ``partial``, ``closed_volume`` (actually closed, not
    merely requested) and ``remaining_volume``. A DONE_PARTIAL retcode
    (10010) is NEVER retried — retrying would close more volume.

    W2: the exact request runs through the official ``order_check``
    first; a refusal raises ORDER_CHECK_FAILED and nothing is sent.
    W4: a ``None`` response or retcode 10031 (no connection) does not
    prove the close failed — the send loop stops and the close is
    reconciled against live broker state (:func:`_reconcile_uncertain_close`);
    it is never re-sent while the outcome is unknown.

    Retries (S4): only RETRYABLE retcodes are re-sent, with bounded
    exponential backoff from ``retry_policy``; hard rejects fail fast.
    Before a retry, the live position book is re-read — if the ticket
    is already gone, the close landed despite the retcode and the call
    returns done (recovered) instead of closing twice.

    P1-4: ``mt5_trade_constants`` + the filling mode are cached per
    symbol on the context (TTL + reconnect invalidation), and the
    retryable-retcode guard's position fetch is carried into the next
    attempt instead of re-fetched. ``order_check`` + reconciliation
    still run fresh on every attempt — the caching is latency only,
    never a behaviour change.

    Idempotency (S4): retcode 10036 POSITION_CLOSED means the position
    is already closed. The ticket is verified gone in the live book
    and the call returns done with ``already_closed: True`` (this call
    closed nothing) instead of a failure; if the position is STILL
    open, broker state contradicts the retcode and the outcome is
    TRADE_UNVERIFIED. The payload reports ``attempts`` throughout.
    """
    ctx.driver.ensure_connected()
    policy = retry_policy or ExecutionRetryPolicy()
    last: Any = None
    last_check: dict[str, Any] | None = None
    last_retcode: int | None = None
    # P1-4: the retryable-retcode guard below already fetched the live
    # position to prove the close did not land — carry that row into
    # the next attempt instead of fetching it twice. The carried row
    # is one backoff old (0.1–0.4 s); type/symbol/magic are immutable
    # for a ticket and only volume can shrink (a partial close by
    # another actor) — a stale-high volume can only make the broker
    # reject (INVALID_VOLUME), never over-close, and order_check still
    # runs fresh on the exact request every attempt.
    carried_pos: Any = None
    for attempt in range(1, policy.max_attempts + 1):
        if carried_pos is not None:
            pos_raw = carried_pos
            carried_pos = None
        else:
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
        # A3: a tick with a missing/zero price must never reach the broker
        # as a zero-price close — fail closed here, before dispatch.
        price = float(tick.bid) if pos_type == 0 else float(tick.ask)
        if not price > 0:
            raise BridgeError(
                ErrorCode.MT5_CALL_FAILED,
                f"No usable close price for position #{ticket} (tick price is {price}) — refusing to send a zero-price close.",
                details={"ticket": ticket, "symbol": symbol, "price": price},
            )
        filling = resolve_filling_mode(ctx, symbol)
        consts = mt5_trade_constants(ctx)
        request: dict[str, Any] = {
            "action": consts["action_deal"],
            "symbol": symbol,
            "volume": close_volume,
            "type": consts["order_type_sell"] if pos_type == 0 else consts["order_type_buy"],
            "position": int(ticket),
            "price": price,
            "magic": int(_f(pos_raw, "magic", 0) or 0),
            "comment": "NSE_CLOSE",
            "type_time": consts["order_time_gtc"],
            "type_filling": filling,
        }
        # W2 step 5: official pre-send check with the exact request —
        # a refusal raises and order_send is never reached.
        last_check = order_check_send(ctx, request, "close_position")

        def job(mt5: Any, request=request):  # loop variables bound as defaults (B023)
            return mt5.order_send(request)

        last = ctx.driver.run(job, op="order_send_close", context={"ticket": ticket, "volume": close_volume})
        retcode = int(_f(last, "retcode", 0)) if last is not None else None
        last_retcode = retcode
        if last is None or retcode == TRADE_RETCODE_CONNECTION:
            # W4: uncertain — reconcile against broker truth, never re-send.
            outcome_uncertain = _reconcile_uncertain_close(ctx, ticket, pos_volume, symbol)
            if outcome_uncertain is not None:
                outcome_uncertain["retcode"] = retcode
                outcome_uncertain["retcode_label"] = (
                    translate_retcode(retcode)
                    if retcode is not None
                    else "NO_RESPONSE - order_send returned no result object"
                )
                outcome_uncertain["order_check"] = last_check
                outcome_uncertain["attempts"] = attempt
                ctx.trade_metrics.record(
                    "close_position", "done", 0.0, retcode, {"ticket": ticket, "reconciled": True, "attempts": attempt}
                )
                return outcome_uncertain
            ctx.trade_metrics.record(
                "close_position", "unknown", 0.0, retcode, {"ticket": ticket, "uncertain": True, "attempts": attempt}
            )
            raise _unverified(
                "close_position",
                f"Close for #{ticket} returned "
                + ("no result" if last is None else "retcode 10031 (no connection)")
                + " and reconciliation could not establish the outcome — reconcile broker state "
                "before any retry; the close was never re-sent.",
                {
                    "ticket": ticket,
                    "volume": close_volume,
                    "retcode": retcode,
                    "attempts": attempt,
                    "order_check": last_check,
                },
                retcode=retcode,
            )
        if retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL):
            outcome = "done" if retcode == TRADE_RETCODE_DONE else "partial"
            payload = _execution_payload(last, symbol) | {
                "status": "done",
                "ticket": ticket,
                "closed_volume": close_volume,
                "attempts": attempt,
                "order_check": last_check,
            }
            requested_partial = close_volume < pos_volume - 1e-9
            if requested_partial or retcode == TRADE_RETCODE_DONE_PARTIAL:
                # Partial close: prove the resulting state against live
                # broker truth before reporting it (spec §7).
                remaining, reconciled = _reconcile_close_volume(ctx, ticket)
                if remaining is None and reconciled:
                    # Position gone: the broker closed everything.
                    payload["closed_volume"] = pos_volume
                    payload["partial"] = False
                    payload["remaining_volume"] = 0.0
                    payload["reconciled"] = True
                elif remaining is not None:
                    closed = max(0.0, pos_volume - remaining)
                    if closed <= 1e-9 and retcode == TRADE_RETCODE_DONE_PARTIAL:
                        # Broker reported partial completion but nothing
                        # moved — outcome uncertain, never report success.
                        raise BridgeError(
                            ErrorCode.TRADE_UNVERIFIED,
                            f"Close for #{ticket} reported DONE_PARTIAL but the position volume is unchanged "
                            f"({remaining}) — reconcile broker state before retrying.",
                            retcode=retcode,
                            details={
                                "ticket": ticket,
                                "remaining_volume": remaining,
                                "attempts": attempt,
                                "order_check": last_check,
                            },
                        )
                    payload["closed_volume"] = closed
                    payload["partial"] = remaining > 1e-9
                    payload["remaining_volume"] = remaining
                    payload["reconciled"] = True
                else:
                    # Re-read failed: the request was partial, but the
                    # resulting state is unverified — say so explicitly.
                    payload["partial"] = True
                    payload["remaining_volume"] = None
                    payload["reconciled"] = False
            ctx.trade_metrics.record("close_position", outcome, 0.0, retcode, {"ticket": ticket, "attempts": attempt})
            return payload
        if retcode == TRADE_RETCODE_POSITION_CLOSED:
            # Idempotent success — but only after the live book proves
            # the ticket is actually gone. A 10036 with the position
            # still open contradicts broker state: unverified, never
            # reported as success.
            try:
                fetch_position(ctx, ticket)
            except BridgeError as exc:
                if exc.code is ErrorCode.TICKET_NOT_FOUND:
                    logger.info("[CLOSE] ticket={} already closed (retcode 10036) — idempotent success", ticket)
                    ctx.trade_metrics.record(
                        "close_position",
                        "done",
                        0.0,
                        retcode,
                        {"ticket": ticket, "already_closed": True, "attempts": attempt},
                    )
                    return {
                        "status": "done",
                        "ticket": ticket,
                        "symbol": symbol,
                        "closed_volume": 0.0,
                        "remaining_volume": 0.0,
                        "partial": False,
                        "reconciled": True,
                        "already_closed": True,
                        "retcode": retcode,
                        "retcode_label": translate_retcode(retcode),
                        "attempts": attempt,
                        "order_check": last_check,
                    }
                raise
            raise BridgeError(
                ErrorCode.TRADE_UNVERIFIED,
                f"Close for #{ticket} reported POSITION_CLOSED (10036) but the position is still open — "
                "reconcile broker state before retrying.",
                details={"ticket": ticket, "attempts": attempt, "order_check": last_check},
            )
        if retryable_retcode(retcode) and attempt < policy.max_attempts:
            # Idempotency guard: accepted server-side despite the
            # ambiguous retcode? If the ticket is gone, the close
            # landed — report it recovered, never send again.
            try:
                guard_pos = fetch_position(ctx, ticket)
            except BridgeError as exc:
                if exc.code is ErrorCode.TICKET_NOT_FOUND:
                    logger.info(
                        "[CLOSE] ticket={} no longer open after ambiguous retcode={} — treated as closed",
                        ticket,
                        retcode,
                    )
                    ctx.trade_metrics.record(
                        "close_position",
                        "done",
                        0.0,
                        retcode,
                        {"ticket": ticket, "recovered": True, "attempts": attempt},
                    )
                    return {
                        "status": "done",
                        "ticket": ticket,
                        "symbol": symbol,
                        "closed_volume": close_volume,
                        "retcode": retcode,
                        "retcode_label": translate_retcode(retcode),
                        "ambiguous_fill_recovered": True,
                        "attempts": attempt,
                        "order_check": last_check,
                    }
                raise
            # P1-4: the position is still open and this guard fetch is
            # the live row the next attempt needs — carry it forward
            # instead of re-fetching it at the top of the loop.
            carried_pos = guard_pos
            logger.warning(
                "[CLOSE] ticket={} retcode={} ({}) — retryable, backing off before attempt {}/{}",
                ticket,
                retcode,
                retcode_class_label(retcode),
                attempt + 1,
                policy.max_attempts,
            )
            policy.backoff(attempt)
            continue
        # Fail fast: a hard reject / repairable request can never
        # succeed by re-sending, and the retry budget is spent.
        ctx.trade_metrics.record("close_position", "rejected", 0.0, retcode, {"ticket": ticket, "attempts": attempt})
        raise BridgeError(
            ErrorCode.TRADE_REJECTED,
            f"Close failed for #{ticket} after {attempt} attempt(s): {translate_retcode(retcode or 0)}",
            retcode=retcode,
            details={"ticket": ticket, "attempts": attempt, "order_check": last_check},
        )
    # Unreachable: every loop iteration returns, raises, or continues
    # into the fail-fast raise — but mypy needs a terminus.
    raise BridgeError(
        ErrorCode.TRADE_REJECTED,
        f"Close failed for #{ticket}: retry budget exhausted ({translate_retcode(last_retcode or 0)})",
        retcode=last_retcode,
        details={"ticket": ticket, "attempts": policy.max_attempts, "order_check": last_check},
    )


def close_position_by(ctx: BridgeContext, ticket: int, ticket_by: int) -> dict[str, Any]:
    """Close two opposite positions against each other (spec §6, C2).

    Sends ``TRADE_ACTION_CLOSE_BY`` with ``position`` + ``position_by``.
    Hedging accounts ONLY (ENUM_ACCOUNT_MARGIN_MODE:
    ``ACCOUNT_MARGIN_MODE_RETAIL_HEDGING`` = 2 — verified against the
    package constants live, E-OC-1): on a netting account (0) close-by
    is impossible by construction — the caller must close + open an
    opposing deal instead. (Real-MT5 defect D5, fixed in Phase 2b: the
    old check compared against 1, which is EXCHANGE, and therefore
    wrongly refused every genuine hedging account, including the
    MetaQuotes Demo account used for Phase 2b verification.) When the
    margin mode is unreported the Bridge refuses rather than guessing.

    Both positions must exist, share one symbol, and be opposite types.
    Per MQL5, the smaller-volume position closes entirely and the larger
    one remains open with the volume difference (both close only when
    the volumes are equal) — the resulting state is verified against
    live broker truth before the result is reported.
    """
    ctx.driver.ensure_connected()
    pos = fetch_position(ctx, ticket)
    pos_by = fetch_position(ctx, ticket_by)
    if not _position_owns(ctx, pos) or not _position_owns(ctx, pos_by):
        raise BridgeError(
            ErrorCode.TRADE_REJECTED,
            f"Refusing close-by #{ticket} vs #{ticket_by}: foreign magic and strict_magic_guard is on.",
            details={"ticket": ticket, "ticket_by": ticket_by},
        )
    symbol = str(_f(pos, "symbol", ""))
    symbol_by = str(_f(pos_by, "symbol", ""))
    if not symbol or symbol != symbol_by:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Close-by requires two positions on the same symbol (#{ticket}={symbol!r}, #{ticket_by}={symbol_by!r}).",
            http_status=422,
            details={"ticket": ticket, "ticket_by": ticket_by},
        )
    type_a = int(_f(pos, "type", -1))
    type_b = int(_f(pos_by, "type", -1))
    if type_a == type_b:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Close-by requires opposite position types (#{ticket} type={type_a}, #{ticket_by} type={type_b}).",
            http_status=422,
            details={"ticket": ticket, "ticket_by": ticket_by},
        )
    margin_mode = account_info_model(ctx).margin_mode
    if margin_mode is None:
        raise BridgeError(
            ErrorCode.VALIDATION,
            "Close-by requires a confirmed hedging account, but the broker did not report "
            "margin_mode — refusing rather than guessing.",
            http_status=422,
            details={"ticket": ticket, "ticket_by": ticket_by},
        )
    if margin_mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        raise BridgeError(
            ErrorCode.VALIDATION,
            f"Close-by requires a hedging account (margin_mode={margin_mode}; "
            "0=netting, 1=exchange, 2=hedging). On a netting account close the position "
            "and open an opposing deal instead.",
            http_status=422,
            details={"ticket": ticket, "ticket_by": ticket_by, "margin_mode": margin_mode},
        )
    vol_a = float(_f(pos, "volume", 0.0) or 0.0)
    vol_b = float(_f(pos_by, "volume", 0.0) or 0.0)
    closed_volume = min(vol_a, vol_b)
    remaining_volume = abs(vol_a - vol_b)
    remaining_ticket = ticket if vol_a > vol_b else (ticket_by if vol_b > vol_a else None)

    def job(mt5: Any):
        return mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_CLOSE_BY,
                "position": int(ticket),
                "position_by": int(ticket_by),
            }
        )

    raw = ctx.driver.run(job, op="order_send_close_by", context={"ticket": ticket, "ticket_by": ticket_by})
    if raw is None:
        raise BridgeError(
            ErrorCode.TRADE_UNVERIFIED,
            "order_send returned no result — broker state unverified. Reconcile (positions) before retrying.",
            details={"ticket": ticket, "ticket_by": ticket_by},
        )
    retcode = int(_f(raw, "retcode", 0))
    if retcode != TRADE_RETCODE_DONE:
        ctx.trade_metrics.record("close_position_by", "rejected", 0.0, retcode, {"ticket": ticket})
        raise reject(raw, "close_position_by", symbol)
    # Verify the resulting state: the smaller position must be gone; the
    # larger remains only with the volume difference (gone when equal).
    for t, expect_gone in ((ticket, vol_a <= vol_b), (ticket_by, vol_b <= vol_a)):
        try:
            current = fetch_position(ctx, t)
        except BridgeError as exc:
            if exc.code is ErrorCode.TICKET_NOT_FOUND:
                if expect_gone:
                    continue
                raise BridgeError(
                    ErrorCode.TRADE_UNVERIFIED,
                    f"Close-by reported DONE but position #{t} is gone while it should remain open "
                    f"with {remaining_volume} — reconcile broker state before retrying.",
                    details={"ticket": ticket, "ticket_by": ticket_by},
                ) from exc
            raise
        if expect_gone:
            raise BridgeError(
                ErrorCode.TRADE_UNVERIFIED,
                f"Close-by reported DONE but position #{t} is still open — reconcile broker state before retrying.",
                details={"ticket": ticket, "ticket_by": ticket_by},
            )
        remaining_now = float(_f(current, "volume", 0.0) or 0.0)
        if abs(remaining_now - remaining_volume) > 1e-9:
            raise BridgeError(
                ErrorCode.TRADE_UNVERIFIED,
                f"Close-by reported DONE but position #{t} remains with {remaining_now} "
                f"(expected {remaining_volume}) — reconcile broker state before retrying.",
                details={"ticket": ticket, "ticket_by": ticket_by, "remaining_volume": remaining_now},
            )
    ctx.trade_metrics.record("close_position_by", "done", 0.0, retcode, {"ticket": ticket})
    return {
        "status": "done",
        "ticket": ticket,
        "ticket_by": ticket_by,
        "symbol": symbol,
        "closed_volume": closed_volume,
        "remaining_volume": remaining_volume,
        "remaining_ticket": remaining_ticket,
        "retcode": retcode,
        "retcode_label": translate_retcode(retcode),
    }
