"""Outbound secure WebSocket channel to the NSE Platform server.

The bridge dials OUT to the server (no inbound ports, firewall friendly),
authenticates with the platform token, announces capabilities, then:
- executes whitelisted commands sent by the server (registry-enforced)
- streams subscribed ticks + account snapshots + health
- keeps the connection alive with heartbeats and exponential-backoff reconnects

Message protocol (JSON, versioned via PROTOCOL_VERSION):
  client -> server: hello, command_result, tick, account_update, ping
  server -> client: command{id, command, params}, subscribe, unsubscribe, pong
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from typing import Any

from loguru import logger
from starlette.concurrency import run_in_threadpool

from .. import BRIDGE_VERSION, PROTOCOL_VERSION
from ..engine.common import BridgeContext
from ..engine.registry import REGISTRY, all_commands
from ..errors import BridgeError
from .bus import EventBus


class ServerChannel:
    def __init__(self, ctx: BridgeContext, bus: EventBus) -> None:
        self.ctx = ctx
        self.bus = bus
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ws: Any = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.ctx.settings.server_url:
            logger.info("[CHANNEL] server_url empty — outbound channel disabled")
            return
        if not self.ctx.settings.server_token:
            logger.warning("[CHANNEL] server_token missing — outbound channel disabled")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="nse-server-channel")
        state = self.ctx.server_channel_state
        state["enabled"] = True
        state["url"] = self.ctx.settings.server_url

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        state = self.ctx.server_channel_state
        state["enabled"] = False
        state["connected"] = False

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        s = self.ctx.settings
        backoff = s.reconnect_initial_ms / 1000.0
        while not self._stop.is_set():
            try:
                logger.info("[CHANNEL] connecting to {}", s.server_url)
                import websockets  # lazy import keeps startup fast

                headers = {"Authorization": f"Bearer {s.server_token}"}
                async with websockets.connect(
                    s.server_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=15,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    state = self.ctx.server_channel_state
                    state["connected"] = True
                    state["last_connected_at"] = time.time()
                    state["last_error"] = None
                    backoff = s.reconnect_initial_ms / 1000.0
                    logger.info("[CHANNEL] connected to {}", s.server_url)
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state = self.ctx.server_channel_state
                state["connected"] = False
                state["reconnects"] = int(state.get("reconnects", 0)) + 1
                state["last_error"] = str(exc)
                logger.warning("[CHANNEL] disconnected: {} — reconnect in {:.1f}s", exc, backoff)
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            jitter = random.uniform(0, 0.3) * backoff
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff + jitter)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 1.8, s.reconnect_max_ms / 1000.0)

    # ------------------------------------------------------------------
    async def _session(self, ws: Any) -> None:
        await ws.send(await run_in_threadpool(self._hello_payload))
        sender = asyncio.create_task(self._stream_sender(ws))
        try:
            async for raw in ws:
                await self._handle_server_message(ws, raw)
        finally:
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, Exception):
                pass

    async def _stream_sender(self, ws: Any) -> None:
        """Forward bus events (ticks/account) + heartbeats to the server."""
        queue = self.bus.subscribe()
        last_ping = time.monotonic()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    await ws.send(json.dumps(event))
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - last_ping >= self.ctx.settings.heartbeat_interval_s:
                    last_ping = time.monotonic()
                    await ws.send(json.dumps({"type": "ping", "ts": time.time(), "uptime_s": round(self.ctx.uptime_s, 1)}))
        finally:
            self.bus.unsubscribe(queue)

    # ------------------------------------------------------------------
    def _hello_payload(self) -> str:
        import json

        ctx = self.ctx
        state = ctx.driver.connection_state_dict()
        return json.dumps(
            {
                "type": "hello",
                "device_id": ctx.get_or_create_device_id(),
                "bridge_version": BRIDGE_VERSION,
                "protocol_version": PROTOCOL_VERSION,
                "mt5": {
                    "state": state["state"],
                    "connected": ctx.driver.connected,
                    "login": state.get("login"),
                    "terminal_version": state.get("terminal_version"),
                },
                "capabilities": {
                    "commands_count": len(REGISTRY),
                    "commands": [c.name for c in all_commands()],
                    "mutating_commands": [c.name for c in all_commands() if c.mutates],
                    "features": ["ticks", "account_stream", "commands", "history", "risk_calcs"],
                },
            }
        )

    async def _handle_server_message(self, ws: Any, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            logger.warning("[CHANNEL] non-JSON message from server: {}", str(raw)[:200])
            return
        mtype = msg.get("type")
        if mtype == "command":
            await self._execute_command(ws, msg)
        elif mtype == "subscribe":
            symbol = str(msg.get("symbol", "")).strip()
            if symbol:
                interval = int(msg.get("interval_ms", 250))
                await run_in_threadpool(self.ctx.subscriptions.add, symbol, interval)
                logger.info("[CHANNEL] server subscribed ticks: {} @ {}ms", symbol, interval)
        elif mtype == "unsubscribe":
            symbol = str(msg.get("symbol", "")).strip()
            self.ctx.subscriptions.remove(symbol)
            logger.info("[CHANNEL] server unsubscribed ticks: {}", symbol)
        elif mtype == "request_full_state":
            payload = await run_in_threadpool(self._full_state_payload)
            try:
                await ws.send(json.dumps({"type": "full_state", "data": payload}, default=str))
                logger.info("[CHANNEL] full state sent to server")
            except Exception as exc:
                logger.warning("[CHANNEL] failed to send full_state: {}", exc)
        elif mtype == "pong":
            pass
        else:
            logger.debug("[CHANNEL] ignored server message type={}", mtype)

    def _full_state_payload(self) -> dict[str, Any]:
        """One-shot complete snapshot (server restart sync / reconciliation)."""
        from ..engine.common import account_info_model
        from ..engine.trading import fetch_positions
        from ..mt5.types import build_order_model, build_position_model
        from ..errors import BridgeError

        ctx = self.ctx
        offset = ctx.settings.broker_utc_offset_minutes
        state: dict[str, Any] = {
            "device_id": ctx.get_or_create_device_id(),
            "generated_at": time.time(),
            "bridge": {
                "version": BRIDGE_VERSION,
                "uptime_seconds": round(ctx.uptime_s, 1),
                "server_channel": ctx.server_channel_state,
            },
            "mt5": ctx.driver.connection_state_dict(),
        }
        if ctx.driver.connected:
            try:
                acc = account_info_model(ctx)
                state["account"] = acc.model_dump()
            except BridgeError as exc:
                state["account"] = {"error": exc.message}
            try:
                rows = fetch_positions(ctx)
                state["positions"] = {
                    "count": len(rows),
                    "positions": [build_position_model(r, offset).model_dump() for r in rows],
                }
            except BridgeError as exc:
                state["positions"] = {"error": exc.message}
            try:
                def orders_job(mt5: Any):
                    raw = mt5.orders_get()
                    return list(raw) if raw else []

                rows = ctx.driver.run(orders_job, "orders_full_state")
                state["orders"] = {
                    "count": len(rows),
                    "orders": [build_order_model(r, offset).model_dump() for r in rows],
                }
            except BridgeError as exc:
                state["orders"] = {"error": exc.message}
            state["trade_metrics"] = ctx.trade_metrics.to_dict()
            state["subscriptions"] = [
                {"symbol": s.symbol, "interval_ms": s.interval_ms} for s in ctx.subscriptions.snapshot()
            ]
        return state

    async def _execute_command(self, ws: Any, msg: dict[str, Any]) -> None:
        cmd_id = msg.get("id") or str(uuid.uuid4())
        name = str(msg.get("command", ""))
        params = msg.get("params") or {}
        t0 = time.perf_counter()
        spec = next((c for c in all_commands() if c.name == name), None)
        if spec is None:
            await ws.send(
                json.dumps(
                    {
                        "type": "command_result",
                        "id": cmd_id,
                        "ok": False,
                        "error": {"code": "UNKNOWN_COMMAND", "message": f"Unknown command '{name}'"},
                    }
                )
            )
            return
        try:
            if spec.request_model is not None:
                params = spec.request_model(**params)
            result = await run_in_threadpool(spec.handler, self.ctx, params)
            payload = {"type": "command_result", "id": cmd_id, "ok": True, "data": result}
        except BridgeError as exc:
            payload = {"type": "command_result", "id": cmd_id, "ok": False, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[CHANNEL] command {} crashed", name)
            payload = {
                "type": "command_result",
                "id": cmd_id,
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(exc)},
            }
        payload["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        try:
            await ws.send(json.dumps(payload, default=str))
        except Exception as exc:
            logger.warning("[CHANNEL] failed to send command_result: {}", exc)
