"""Async event bus feeding the local dashboard WS channel (/ws/ui).

Streaming tasks:
- tick poller: fresh tick per subscribed symbol at its interval
  (dashboard only — the server gets ticks via §17.1 chart.tick under
  a subscription, never as ``tick`` frames)
- account poller: account snapshot at the configured interval
  (dashboard bus + §3.3 ``account`` sync batch via the sync engine)
- positions poller: open positions + pending orders (dashboard bus +
  §3.3 ``positions`` sync batch)
- orders poller: pending-order snapshots (dashboard only)
- status poller: periodic bridge status (dashboard only)

CONTRACT v1 (§5.3): the old server frames (``tick``,
``account_update``, ``positions_update``, ``orders_update``,
``status_update``, ``trade_event``) no longer exist on the server
channel. The server channel reads nothing from this bus; snapshots
reach the server as REST sync batches, ticks as chart frames.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from starlette.concurrency import run_in_threadpool

from ..engine.common import BridgeContext


class EventBus:
    """Fan-out of typed events to WS consumers. Slow consumers drop (no backpressure)."""

    def __init__(self, max_queue: int = 500) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue
        # Slow-consumer drops are counted, not silent: the diagnostics
        # summary can report a saturated dashboard feed instead of the
        # bug hiding as "everything looks fine".
        self.dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "data": data, "ts": time.time()}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self.dropped += 1  # slow consumer: drop rather than block the bus
                if self.dropped % 100 == 1:
                    logger.debug("[BUS] queue full — dropped event {} (total drops {})", event_type, self.dropped)


async def tick_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push fresh ticks for every subscription (per-symbol min interval).

    Sleeps until the EARLIEST due subscription instead of a fixed
    50 ms wake-up: with no subscribers the loop costs no MT5 IPC and
    no CPU beyond one event wait per idle second (the old 20 wakes/s
    hammered the serialized MT5 worker for nothing).
    """
    last_sent: dict[str, float] = {}
    from ..mt5.types import build_tick_model

    idle_wake_s = 1.0  # re-check subscription changes at most 1×/s when idle
    while not stop.is_set():
        polled = 0.0
        try:
            subs = ctx.subscriptions.snapshot()
            now = time.monotonic()
            due = [s for s in subs if now - last_sent.get(s.symbol, 0.0) >= s.interval_ms / 1000.0]
            if due:
                ctx.driver.ensure_connected()

                def job(mt5: Any, due=due):
                    out = {}
                    for s in due:
                        try:
                            raw = mt5.symbol_info_tick(s.symbol)
                            if raw is not None:
                                out[s.symbol] = build_tick_model(
                                    raw, s.symbol, ctx.settings.broker_utc_offset_minutes
                                ).model_dump()
                        except Exception as exc:  # noqa: BLE001 — one bad tick must not kill the poller
                            logger.debug("[STREAM] tick for {} skipped: {}", s.symbol, type(exc).__name__)
                            continue
                    return out

                ticks = await run_in_threadpool(ctx.driver.run, job, "tick_poll")
                for symbol, payload in ticks.items():
                    last_sent[symbol] = time.monotonic()
                    ctx.subscriptions.record_tick(symbol, payload)
                    bus.publish("tick", payload)
                polled = 0.05  # yield briefly after work before recomputing dues
        except Exception as exc:  # noqa: BLE001 — a poller must never die on a stray MT5 error
            logger.debug("[STREAM] tick poll skipped: {}", exc)
        # Sleep until the next symbol comes due (bounded), or idle_wake
        # when nothing is subscribed — never a fixed hot 50 ms spin.
        subs_now = ctx.subscriptions.snapshot()
        if subs_now:
            now = time.monotonic()
            next_due = min(last_sent.get(s.symbol, 0.0) + s.interval_ms / 1000.0 for s in subs_now)
            timeout = max(polled, min(next_due - now, idle_wake_s))
        else:
            timeout = idle_wake_s
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.01, timeout))
        except TimeoutError:
            pass


async def account_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push account snapshots at the configured interval.

    The snapshot goes to the local dashboard bus AND — as a §3.3
    ``account`` sync batch — to the server via the sync engine (the
    old ``account_update`` server frame no longer exists in v1).
    """
    from ..engine.common import account_info_model

    interval = max(0.2, ctx.settings.stream_account_interval_ms / 1000.0)
    while not stop.is_set():
        try:
            if ctx.driver.connected:
                acc = await run_in_threadpool(account_info_model, ctx)
                bus.publish("account_update", acc.model_dump())
                sync_engine = getattr(ctx, "sync_engine", None)
                if sync_engine is not None:
                    items = sync_engine.account_items(acc, ctx.driver.connection_state_dict())
                    sync_engine.enqueue("account", items)
        except Exception as exc:  # noqa: BLE001 — a poller must never die on a stray MT5 error
            logger.debug("[STREAM] account poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def positions_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push open-positions snapshots to the server (2s interval).

    The snapshot goes to the local dashboard bus AND — as a §3.3
    ``positions`` sync batch (open positions + pending orders) — to
    the server via the sync engine. The old ``positions_update`` server
    frame no longer exists in v1.
    """
    from ..errors import BridgeError
    from ..mt5.types import build_order_model, build_position_model

    interval = 2.0
    while not stop.is_set():
        try:
            if ctx.driver.connected:

                def job(mt5: Any):
                    raw_positions = mt5.positions_get()
                    raw_orders = mt5.orders_get()
                    return (list(raw_positions) if raw_positions else [], list(raw_orders) if raw_orders else [])

                raw_positions, raw_orders = await run_in_threadpool(ctx.driver.run, job, "positions_poll")
                offset = ctx.settings.broker_utc_offset_minutes
                positions = [build_position_model(r, offset).model_dump() for r in raw_positions]
                orders = [build_order_model(r, offset).model_dump() for r in raw_orders]
                bus.publish(
                    "positions_update",
                    {"count": len(positions), "positions": positions},
                )
                sync_engine = getattr(ctx, "sync_engine", None)
                if sync_engine is not None:
                    sync_engine.enqueue("positions", sync_engine.positions_items(positions, orders))
        except BridgeError:
            pass  # not connected — skip silently
        except Exception as exc:  # noqa: BLE001 — a poller must never die on a stray MT5 error
            logger.debug("[STREAM] positions poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def orders_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push pending-order snapshots to the dashboard (3s interval).

    Dashboard-only (pending orders reach the server inside the §3.3
    ``positions`` batch), so the MT5 IPC round-trip is skipped while
    no /ws/ui consumer is attached — a headless bridge pays nothing.
    """
    from ..errors import BridgeError
    from ..mt5.types import build_order_model

    interval = 3.0
    while not stop.is_set():
        try:
            if bus.subscriber_count == 0:
                pass  # nobody is listening — skip the MT5 call entirely
            elif ctx.driver.connected:

                def job(mt5: Any):
                    raw = mt5.orders_get()
                    return list(raw) if raw else []

                rows = await run_in_threadpool(ctx.driver.run, job, "orders_poll")
                orders = [build_order_model(r, ctx.settings.broker_utc_offset_minutes).model_dump() for r in rows]
                bus.publish("orders_update", {"count": len(orders), "orders": orders})
        except BridgeError:
            pass
        except Exception as exc:  # noqa: BLE001 — a poller must never die on a stray MT5 error
            logger.debug("[STREAM] orders poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


async def status_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push periodic bridge status to the dashboard (10s interval).

    Dashboard-only: the old ``status_update`` server frame does not
    exist in v1 (§5.3), so the payload is skipped while no /ws/ui
    consumer is attached.
    """
    import os

    interval = 10.0
    while not stop.is_set():
        try:
            if bus.subscriber_count == 0:
                pass  # nobody is listening
            else:
                ops = ctx.driver.metrics_dict()
            slowest = max(ops.items(), key=lambda kv: kv[1]["avg_ms"]) if ops else None
            bus.publish(
                "status_update",
                {
                    "bridge_version": __import__("nse_bridge", fromlist=["BRIDGE_VERSION"]).BRIDGE_VERSION,
                    "uptime_seconds": round(ctx.uptime_s, 1),
                    "mt5_state": ctx.driver.connection_state_dict()["state"],
                    "mt5_connected": ctx.driver.connected,
                    "subscriptions": len(ctx.subscriptions.snapshot()),
                    "server_channel": ctx.server_channel_state,
                    "pid": os.getpid(),
                    "avg_call_ms": round(slowest[1]["avg_ms"], 2) if slowest else None,
                },
            )
        except Exception as exc:  # noqa: BLE001 — a poller must never die on a stray MT5 error
            logger.debug("[STREAM] status poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


def start_streaming(ctx: BridgeContext, bus: EventBus) -> tuple[list[asyncio.Task], asyncio.Event]:
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(tick_poller(ctx, bus, stop), name="nse-tick-poller"),
        asyncio.create_task(account_poller(ctx, bus, stop), name="nse-account-poller"),
        asyncio.create_task(positions_poller(ctx, bus, stop), name="nse-positions-poller"),
        asyncio.create_task(orders_poller(ctx, bus, stop), name="nse-orders-poller"),
        asyncio.create_task(status_poller(ctx, bus, stop), name="nse-status-poller"),
    ]
    return tasks, stop


def stop_streaming(tasks: list[asyncio.Task], stop: asyncio.Event) -> None:
    stop.set()
    for t in tasks:
        t.cancel()
