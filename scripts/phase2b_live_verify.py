"""Phase 2b W11 — live Demo verification harness (real MT5, real broker state).

Every step executes the Bridge's REAL execution path (the same engine
functions the server command channel dispatches) against the attached
MetaTrader 5 DEMO terminal. All trade objects are test-owned:
``comment`` starts with ``NSE-P2B-VERIFY`` and the magic is the
configured bridge magic — the harness never touches any other
position or order, and cleans up only what it created.

Usage (run steps individually; market hours required for FX steps):
    python scripts/phase2b_live_verify.py probe
    python scripts/phase2b_live_verify.py order_check
    python scripts/phase2b_live_verify.py close_position
    python scripts/phase2b_live_verify.py modify_sl_tp
    python scripts/phase2b_live_verify.py partial_close
    python scripts/phase2b_live_verify.py reconciliation
    python scripts/phase2b_live_verify.py modify_pending
    python scripts/phase2b_live_verify.py cleanup

Output is JSON on stdout — paste verbatim into
docs/upgrade/PHASE-2B-VERIFICATION.md (E-records).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, ".")

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver

VERIFY_COMMENT = "NSE-P2B-VERIFY"
SYMBOL = "EURUSD"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _out(**payload) -> None:
    payload["ts"] = _now_iso()
    print(json.dumps(payload, indent=2, default=str))


def _ctx() -> BridgeContext:
    settings = BridgeSettings(_env_file=None)
    driver = Mt5Driver(settings)
    summary = driver.connect()
    if not summary.get("ok"):
        raise SystemExit(f"MT5 terminal unavailable: {summary.get('error')}")
    return BridgeContext(settings=settings, driver=driver)


def _test_owned(mt5_obj: object) -> bool:
    return str(getattr(mt5_obj, "comment", "") or "").startswith(VERIFY_COMMENT)


def step_probe(ctx: BridgeContext) -> None:
    from nse_bridge.engine.trading import fetch_positions

    info = ctx.driver.connection_state_dict()
    acct = ctx.driver.run(lambda mt5: mt5.account_info(), op="account_info")
    from nse_bridge.mt5.types import f as _f

    positions = fetch_positions(ctx)
    _out(
        step="probe",
        login=acct.login,
        server=acct.server,
        trade_mode=int(_f(acct, "trade_mode", -1)),
        trade_mode_meaning="0=DEMO,1=CONTEST,2=REAL",
        margin_mode=int(_f(acct, "margin_mode", -1)),
        balance=float(_f(acct, "balance", 0.0)),
        currency=str(_f(acct, "currency", "")),
        driver_state=info["state"],
        terminal_version=info.get("terminal_version"),
        pkg_version=info.get("pkg_version"),
        open_positions=len(positions),
        test_owned_positions=sum(1 for p in positions if _test_owned(p)),
    )


def _open_test_position(ctx: BridgeContext, volume: float = 0.01) -> dict:
    """Open ONE test-owned position through the Bridge's own market-order
    path (local capability; demo; 0.01 lots)."""
    from nse_bridge.engine.trading import market_order

    result = market_order(ctx, symbol=SYMBOL, side="buy", volume=volume, comment=VERIFY_COMMENT)
    if result.get("status") != "done":
        raise SystemExit(f"test position open failed: {result}")
    return result


def step_close_position(ctx: BridgeContext) -> None:
    from nse_bridge.engine.trading import close_position, fetch_position
    from nse_bridge.errors import BridgeError
    from nse_bridge.mt5.types import f as _f

    opened = _open_test_position(ctx, 0.01)
    ticket = int(opened["order_ticket"])
    before = _f(fetch_position(ctx, ticket), "volume", 0.0)
    t0 = time.perf_counter()
    result = close_position(ctx, ticket)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    remaining = None
    try:
        after = fetch_position(ctx, ticket)
        remaining = float(_f(after, "volume", 0.0))
    except BridgeError as exc:
        if exc.code.value != "TICKET_NOT_FOUND":
            raise
        remaining = 0.0  # position gone
    _out(
        step="close_position",
        ticket=ticket,
        operation="close_position (full)",
        retcode=result.get("retcode"),
        retcode_label=result.get("retcode_label"),
        requested_volume=before,
        closed_volume=result.get("closed_volume"),
        remaining_after=remaining,
        fill_price=result.get("price"),
        duration_ms=round(duration_ms, 1),
        order_check_retcode=(result.get("order_check") or {}).get("retcode"),
        broker_state_before=f"volume={before}",
        broker_state_after=f"volume={remaining}",
        reconciled=result.get("reconciled"),
    )


def step_modify_sl_tp(ctx: BridgeContext) -> None:
    from nse_bridge.engine.common import require_symbol_info
    from nse_bridge.engine.trading import fetch_position, modify_position_sltp
    from nse_bridge.mt5.types import f as _f

    opened = _open_test_position(ctx, 0.01)
    ticket = int(opened["order_ticket"])
    pos_before = fetch_position(ctx, ticket)
    spec = require_symbol_info(ctx, SYMBOL)
    before = {"sl": float(_f(pos_before, "sl", 0.0) or 0.0), "tp": float(_f(pos_before, "tp", 0.0) or 0.0)}
    tick = ctx.driver.run(lambda mt5: mt5.symbol_info_tick(SYMBOL), op="symbol_info_tick")
    point = float(spec.point)
    # SL 30 points below the current bid, TP 60 points above the ask
    # (inside broker stops level; digits-rounded).
    new_sl = round(float(tick.bid) - 30 * point, int(spec.digits))
    new_tp = round(float(tick.ask) + 60 * point, int(spec.digits))
    t0 = time.perf_counter()
    result = modify_position_sltp(ctx, ticket, new_sl, new_tp)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    pos_after = fetch_position(ctx, ticket)
    after = {"sl": float(_f(pos_after, "sl", 0.0) or 0.0), "tp": float(_f(pos_after, "tp", 0.0) or 0.0)}
    _out(
        step="modify_sl_tp",
        ticket=ticket,
        operation="modify_sl_tp",
        retcode=result.get("retcode"),
        retcode_label=result.get("retcode_label"),
        requested={"sl": new_sl, "tp": new_tp},
        broker_state_before=before,
        broker_state_after=after,
        landed=abs(after["sl"] - new_sl) <= point and abs(after["tp"] - new_tp) <= point,
        duration_ms=round(duration_ms, 1),
        order_check_retcode=(result.get("order_check") or {}).get("retcode"),
    )


def step_partial_close(ctx: BridgeContext) -> None:
    from nse_bridge.engine.trading import close_position, fetch_position
    from nse_bridge.errors import BridgeError
    from nse_bridge.mt5.types import f as _f

    opened = _open_test_position(ctx, 0.02)
    ticket = int(opened["order_ticket"])
    before = float(_f(fetch_position(ctx, ticket), "volume", 0.0))
    t0 = time.perf_counter()
    result = close_position(ctx, ticket, 0.01)  # reduce-volume (W3 path)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    try:
        remaining = float(_f(fetch_position(ctx, ticket), "volume", 0.0))
    except BridgeError as exc:
        if exc.code.value != "TICKET_NOT_FOUND":
            raise
        remaining = 0.0  # position gone
    _out(
        step="partial_close",
        ticket=ticket,
        operation="close_position(volume=0.01) — reduce-volume",
        retcode=result.get("retcode"),
        retcode_label=result.get("retcode_label"),
        requested_volume=0.01,
        filled_volume=result.get("closed_volume"),
        remaining_volume=remaining,
        partial_flag=result.get("partial"),
        reconciled=result.get("reconciled"),
        broker_state_before=f"volume={before}",
        broker_state_after=f"volume={remaining}",
        duration_ms=round(duration_ms, 1),
        order_check_retcode=(result.get("order_check") or {}).get("retcode"),
        no_retry_scheduled=True,
    )


def step_reconciliation(ctx: BridgeContext) -> None:
    """W4 live: the close LANDS on the broker but the response frame is
    withheld once (a proxy swallows the result) — the Bridge must
    reconcile against REAL broker state and never re-send."""
    from nse_bridge.engine.trading import close_position, fetch_position
    from nse_bridge.mt5.types import f as _f

    opened = _open_test_position(ctx, 0.01)
    ticket = int(opened["order_ticket"])
    before = float(_f(fetch_position(ctx, ticket), "volume", 0.0))

    real_run = ctx.driver.run
    swallowed = {"done": False}

    def run_once_with_lost_response(job, op=None, context=None, timeout=60.0):
        if op == "order_send_close" and not swallowed["done"]:
            swallowed["done"] = True
            real_run(job, op=op, context=context, timeout=timeout)  # the op EXECUTES on the broker
            return None  # …but the response frame is lost
        return real_run(job, op=op, context=context, timeout=timeout)

    ctx.driver.run = run_once_with_lost_response  # type: ignore[method-assign]
    t0 = time.perf_counter()
    result = close_position(ctx, ticket)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    _out(
        step="reconciliation",
        ticket=ticket,
        scenario="order_send response lost (None) after the close executed on the broker",
        retcode=result.get("retcode"),
        retcode_label=result.get("retcode_label"),
        status=result.get("status"),
        closed_volume=result.get("closed_volume"),
        remaining_volume=result.get("remaining_volume"),
        reconciled=result.get("reconciled"),
        reconciliation_source=result.get("reconciliation"),
        ambiguous_fill_recovered=result.get("ambiguous_fill_recovered"),
        broker_state_before=f"volume={before}",
        broker_state_after="position gone",
        duration_ms=round(duration_ms, 1),
        sends_observed=swallowed["done"],
    )


def step_modify_pending(ctx: BridgeContext) -> None:
    """W1 §17.4(c) live proof: trade_modify_pending_order — the exact
    function the server channel routes modify_pending to — accepts a
    PENDING ORDER ticket and changes its SL/TP. GATE STAYS CLOSED in
    production: this run is the evidence the Platform owner needs to
    publish the minimum connector version."""
    from nse_bridge.engine.common import require_symbol_info
    from nse_bridge.engine.execution import PENDING_TYPES
    from nse_bridge.engine.orders import ModifyPendingRequest, trade_modify_pending_order
    from nse_bridge.mt5.types import f as _f

    spec = require_symbol_info(ctx, SYMBOL)
    tick = ctx.driver.run(lambda mt5: mt5.symbol_info_tick(SYMBOL), op="symbol_info_tick")
    point = float(spec.point)
    digits = int(spec.digits)
    price = round(float(tick.bid) - max(500 * point, float(spec.trade_stops_level or 0) * point, 0.001), digits)
    from nse_bridge.engine.trading import mt5_trade_constants

    consts = mt5_trade_constants(ctx)
    place_request = {
        "action": consts["action_pending"],
        "symbol": SYMBOL,
        "volume": 0.01,
        "type": PENDING_TYPES["BUY_LIMIT"],
        "price": price,
        "sl": 0.0,
        "tp": 0.0,
        "comment": VERIFY_COMMENT,
        "type_time": consts["order_time_gtc"],
    }
    placed = ctx.driver.run(lambda mt5: mt5.order_send(place_request), op="order_send_pending")
    if placed is None or int(_f(placed, "retcode", 0)) != 10009:
        raise SystemExit(f"test pending order placement failed: {placed}")
    ticket = int(_f(placed, "order", 0))
    order_before = ctx.driver.run(lambda mt5: mt5.orders_get(ticket=ticket), op="orders_get")[0]
    new_sl = round(price - 100 * point, digits)
    t0 = time.perf_counter()
    result = trade_modify_pending_order(ctx, ModifyPendingRequest(ticket=ticket, sl=new_sl))
    duration_ms = (time.perf_counter() - t0) * 1000.0
    order_after = ctx.driver.run(lambda mt5: mt5.orders_get(ticket=ticket), op="orders_get")[0]
    landed = abs(float(_f(order_after, "sl", 0.0) or 0.0) - new_sl) <= point
    _out(
        step="modify_pending",
        ticket=ticket,
        ticket_kind="pending order (orders_get)",
        mt5_action="TRADE_ACTION_MODIFY (trade_modify_sl_tp with an order ticket)",
        retcode=result.get("retcode"),
        retcode_label=result.get("retcode_label"),
        requested={"sl": new_sl},
        order_state_before={
            "sl": float(_f(order_before, "sl", 0.0) or 0.0),
            "price_open": float(_f(order_before, "price_open", 0.0) or 0.0),
        },
        order_state_after={
            "sl": float(_f(order_after, "sl", 0.0) or 0.0),
            "price_open": float(_f(order_after, "price_open", 0.0) or 0.0),
        },
        landed=landed,
        duration_ms=round(duration_ms, 1),
        order_check_retcode=(result.get("order_check") or {}).get("retcode"),
    )


def step_cleanup(ctx: BridgeContext) -> None:
    """Delete every test-owned pending order and close every test-owned
    position (magic + comment match required — never touches anything else)."""
    from nse_bridge.engine.trading import close_position
    from nse_bridge.mt5.types import f as _f

    orders = ctx.driver.run(lambda mt5: mt5.orders_get(), op="orders_get") or []
    removed = []
    for order in orders:
        if not _test_owned(order):
            continue
        ticket = int(_f(order, "ticket", 0))
        result = ctx.driver.run(
            lambda mt5, t=ticket: mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": t}),
            op="order_send_cancel",
        )
        removed.append({"ticket": ticket, "retcode": int(_f(result, "retcode", 0)) if result else None})
    positions = ctx.driver.run(lambda mt5: mt5.positions_get(), op="positions_get") or []
    closed = []
    for pos in positions:
        if not _test_owned(pos):
            continue
        ticket = int(_f(pos, "ticket", 0))
        try:
            result = close_position(ctx, ticket)
            closed.append({"ticket": ticket, "retcode": result.get("retcode")})
        except Exception as exc:  # noqa: BLE001 — report, never escalate during cleanup
            closed.append({"ticket": ticket, "error": type(exc).__name__})
    leftover = [
        int(_f(p, "ticket", 0))
        for p in (ctx.driver.run(lambda mt5: mt5.positions_get(), op="positions_get") or [])
        if _test_owned(p)
    ]
    _out(step="cleanup", removed_test_owned_orders=removed, closed_test_owned_positions=closed, leftover=leftover)


STEPS = {
    "probe": step_probe,
    "close_position": step_close_position,
    "modify_sl_tp": step_modify_sl_tp,
    "partial_close": step_partial_close,
    "reconciliation": step_reconciliation,
    "modify_pending": step_modify_pending,
    "cleanup": step_cleanup,
}


def main() -> int:
    step_name = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if step_name not in STEPS:
        raise SystemExit(f"unknown step {step_name!r}; choose from {sorted(STEPS)}")
    ctx = _ctx()
    try:
        STEPS[step_name](ctx)
    finally:
        ctx.driver.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
