"""Async event bus feeding both WebSocket channels (dashboard + NSE server).

Streaming tasks:
- tick poller: fresh tick per subscribed symbol at its interval
- account poller: account snapshot at the configured interval
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

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: Any) -> None:
        payload = {"type": event_type, "ts": time.time(), "data": data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block the bus


async def tick_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push fresh ticks for every subscription (per-symbol min interval)."""
    last_sent: dict[str, float] = {}
    from ..mt5.types import build_tick_model

    while not stop.is_set():
        try:
            subs = ctx.subscriptions.snapshot()
            now = time.monotonic()
            due = [
                s
                for s in subs
                if now - last_sent.get(s.symbol, 0.0) >= s.interval_ms / 1000.0
            ]
            if due:
                ctx.driver.ensure_connected()

                def job(mt5: Any):
                    out = {}
                    for s in due:
                        try:
                            raw = mt5.symbol_info_tick(s.symbol)
                            if raw is not None:
                                out[s.symbol] = build_tick_model(raw, s.symbol, ctx.settings.broker_utc_offset_minutes).model_dump()
                        except Exception:
                            pass
                    return out

                ticks = await run_in_threadpool(ctx.driver.run, job, "tick_poll")
                for symbol, payload in ticks.items():
                    last_sent[symbol] = time.monotonic()
                    ctx.subscriptions.record_tick(symbol, payload)
                    bus.publish("tick", payload)
        except Exception as exc:
            logger.debug("[STREAM] tick poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05)
        except asyncio.TimeoutError:
            pass


async def account_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push account snapshots at the configured interval."""
    from ..engine.common import account_info_model

    interval = max(0.2, ctx.settings.stream_account_interval_ms / 1000.0)
    while not stop.is_set():
        try:
            if ctx.driver.connected:
                acc = await run_in_threadpool(account_info_model, ctx)
                bus.publish("account_update", acc.model_dump())
        except Exception as exc:
            logger.debug("[STREAM] account poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def positions_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push open-positions snapshots to the server (2s interval)."""
    from ..mt5.types import build_position_model
    from ..errors import BridgeError

    interval = 2.0
    while not stop.is_set():
        try:
            if ctx.driver.connected:
                def job(mt5: Any):
                    raw = mt5.positions_get()
                    return list(raw) if raw else []

                rows = await run_in_threadpool(ctx.driver.run, job, "positions_poll")
                positions = [
                    build_position_model(r, ctx.settings.broker_utc_offset_minutes).model_dump()
                    for r in rows
                ]
                bus.publish(
                    "positions_update",
                    {"count": len(positions), "positions": positions},
                )
        except BridgeError:
            pass  # not connected — skip silently
        except Exception as exc:
            logger.debug("[STREAM] positions poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def orders_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push pending-order snapshots to the server (3s interval)."""
    from ..mt5.types import build_order_model
    from ..errors import BridgeError

    interval = 3.0
    while not stop.is_set():
        try:
            if ctx.driver.connected:
                def job(mt5: Any):
                    raw = mt5.orders_get()
                    return list(raw) if raw else []

                rows = await run_in_threadpool(ctx.driver.run, job, "orders_poll")
                orders = [
                    build_order_model(r, ctx.settings.broker_utc_offset_minutes).model_dump()
                    for r in rows
                ]
                bus.publish("orders_update", {"count": len(orders), "orders": orders})
        except BridgeError:
            pass
        except Exception as exc:
            logger.debug("[STREAM] orders poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def status_poller(ctx: BridgeContext, bus: EventBus, stop: asyncio.Event) -> None:
    """Push periodic bridge status to the server (10s interval)."""
    import os

    interval = 10.0
    while not stop.is_set():
        try:
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
        except Exception as exc:
            logger.debug("[STREAM] status poll skipped: {}", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
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
