"""Outbound secure WebSocket channel to the NSE Platform server
(CONTRACT v1 §5/§6/§17.1) — REPLACED in Phase 2, do not patch the old
protocol back in.

The bridge dials OUT to the server (no inbound ports, firewall
friendly) and authenticates in the upgrade headers. There is NO
``hello`` frame (§5.1, D-WS-2). Sync travels over REST (§3.3);
chart/symbol data travels over this WS (§17.1).

URL (production): ``wss://bridge.nexusverse.space/api/v1/connector/ws``
(``ws://`` only for the local mock in tests). TLS verification is
mandatory; ``ws://`` is accepted only for loopback test endpoints
(see :func:`nse_bridge.config.validate_server_url`).

Frames, server → client (§5.2 ONLY):

- ``command`` — CommandEnvelope (§6.2)
- ``sync_request`` — re-send ``account`` batch then ``positions``
  (also the account-switch trigger, §7.3)
- ``ack`` — informational (the REST 200 ack is authoritative)
- ``heartbeat`` — server heartbeat reply
- ``chart.request`` / ``chart.subscribe`` / ``chart.unsubscribe`` (§17.1)

Unknown frames are silently ignored, never fatal.

Frames, client → server (§5.3 ONLY):

- ``heartbeat`` ``{type, device_id}`` every 15 s (server replies
  ``heartbeat``; session dead after 45 s without one)
- ``action.result`` (§6.5)
- ``chart.data``, ``chart.tick`` (§17.1)

No ``ping``, no ``tick``, no ``account_update``/``positions_update``/
``orders_update``/``status_update``/``trade_event``,
``request_full_state`` or ``subscribe``/``unsubscribe`` frames exist
in v1 — snapshots/events go over REST batches, and command
acknowledgement IS the ``action.result`` envelope.

Outbound (§5.3/§5.4): bound 128 messages; results first, chart frames
second; the oldest unsent chart frame is dropped with a warning when
full (chart data is re-derivable by re-request, command results are
not). A full outbound channel MUST NOT block the heartbeat loop —
heartbeats bypass the queue entirely. Results persist in the bounded
pending queue across reconnects and are re-sent on reconnect — never
dropped silently.

Reconnect (§5.4): fresh session, no resume payload; backoff 500 ms
doubling → 30 s + jitter (0–100 ms), reset on a successful session;
clean ``close()`` on shutdown. The server re-sends only ``pending``
commands on reconnect, never ``executed``/``rejected``/``expired``;
the client ALSO dedups (replay cache, §6.2).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

from loguru import logger
from starlette.concurrency import run_in_threadpool

from ..config import validate_server_url
from ..engine.common import BridgeContext
from ..reliability import DurableResultQueue, DurableResultStore
from ..reliability.self_healing import HealingContext, Scenario
from ..server.chart import (
    CHART_REQUEST_TIMEOUT_S,
    ChartError,
    ChartService,
    build_chart_data_frame,
    build_chart_error_frame,
    build_chart_tick_frame,
)
from ..server.commands import (
    CommandExecutor,
    CommandValidator,
)
from ..server.manifest import build_manifest
from ..server.queue import BoundedPendingQueue
from ..server.rest import RestClient
from ..server.sync import SyncEngine

__all__ = ["ServerChannel", "backoff_delay", "backoff_with_jitter"]

HEARTBEAT_DEAD_S = 45.0  # §5.3: session dead after 45 s without a frame
OUTBOUND_MAX = 128  # §5.3/§5.4: bounded channels, 128 messages each way
BACKOFF_INITIAL_S = 0.5  # §5.4: initial 500 ms
BACKOFF_CAP_S = 30.0  # §5.4: capped at 30 s
BACKOFF_FACTOR = 2.0  # §5.4: doubling
JITTER_MAX_S = 0.1  # §5.4: jitter of subsec_nanos % 100 ms


def backoff_delay(
    attempt: int, *, initial_s: float = BACKOFF_INITIAL_S, cap_s: float = BACKOFF_CAP_S, factor: float = BACKOFF_FACTOR
) -> float:
    """§5.4 reconnect backoff: initial, doubling, capped at 30 s.

    ``attempt`` counts consecutive failed sessions (0 = first retry).
    """
    return min(initial_s * (factor**attempt), cap_s)


def backoff_with_jitter(delay_s: float) -> float:
    """Add the §5.4 jitter: a uniform 0–100 ms on top of the delay."""
    return delay_s + random.uniform(0, JITTER_MAX_S)


class _OutboundQueue:
    """Priority outbound queue (§5.3): results first, chart frames second.

    Two FIFO queues sharing one 128-message bound. When full, the
    oldest unsent CHART frame is dropped with a warning (chart data is
    re-derivable); a result is dropped from the wire only when the
    queue holds no chart frame at all — and it stays in the pending
    results queue for redelivery on reconnect, so it is never lost
    silently. Heartbeats bypass this queue entirely.
    """

    def __init__(self, maxsize: int = OUTBOUND_MAX) -> None:
        self.maxsize = maxsize
        self._results: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._charts: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)

    def _total(self) -> int:
        return self._results.qsize() + self._charts.qsize()

    def put_result(self, frame: dict[str, Any]) -> None:
        if self._total() >= self.maxsize:
            if not self._charts.empty():
                dropped = self._charts.get_nowait()
                logger.warning(
                    "[CHANNEL] outbound full — dropped oldest chart frame "
                    "(symbol={}) to make room for a command result",
                    dropped.get("symbol"),
                )
            else:
                dropped = self._results.get_nowait()
                logger.warning(
                    "[CHANNEL] outbound full of results — dropped oldest result "
                    "frame (correlation_id={}); it stays in the pending queue "
                    "for redelivery on reconnect",
                    dropped.get("correlation_id"),
                )
        self._results.put_nowait(frame)

    def put_chart(self, frame: dict[str, Any]) -> bool:
        """Enqueue a chart frame. Returns False when the frame was
        dropped (queue full of results — chart data is re-derivable)."""
        if self._total() >= self.maxsize:
            if not self._charts.empty():
                dropped = self._charts.get_nowait()
                logger.warning(
                    "[CHANNEL] outbound full — dropped oldest chart frame (symbol={}) for a newer one",
                    dropped.get("symbol"),
                )
                self._charts.put_nowait(frame)
                return True
            logger.warning(
                "[CHANNEL] outbound full of results — dropping chart frame "
                "(symbol={}); chart data is re-derivable by re-request",
                frame.get("symbol"),
            )
            return False
        self._charts.put_nowait(frame)
        return True

    def clear(self) -> None:
        """Drop everything queued. Chart frames are re-derivable; any
        result dropped here MUST still live in the pending results
        queue (callers re-enqueue from there)."""
        while not self._results.empty():
            self._results.get_nowait()
        while not self._charts.empty():
            self._charts.get_nowait()

    async def get(self) -> dict[str, Any]:
        if not self._results.empty():
            return self._results.get_nowait()
        if not self._charts.empty():
            return self._charts.get_nowait()
        get_result = asyncio.create_task(self._results.get())
        get_chart = asyncio.create_task(self._charts.get())
        try:
            done, _ = await asyncio.wait({get_result, get_chart}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (get_result, get_chart):
                if not task.done():
                    task.cancel()
        if get_result in done:
            return get_result.result()
        return get_chart.result()


class ServerChannel:
    """CONTRACT v1 WebSocket channel (command/ack + chart-data)."""

    def __init__(
        self,
        ctx: BridgeContext,
        bus: Any,
        *,
        rest_client: RestClient | None = None,
        sync_engine: SyncEngine | None = None,
        chart_service: ChartService | None = None,
        executor: CommandExecutor | None = None,
        enrolment: Any | None = None,
        dead_after_s: float = HEARTBEAT_DEAD_S,
    ) -> None:
        self.ctx = ctx
        self.bus = bus  # kept for the local dashboard bus; the server channel no longer consumes it
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ws: Any = None
        self.dead_after_s = dead_after_s
        self._outbound = _OutboundQueue()
        # Pending results are DURABLE (§6.5 + reliability phase): an
        # in-memory-only queue loses acknowledged results on a Bridge
        # restart mid-operation. The store file is created lazily on
        # the first persisted result; construction only reads it.
        self._durable_store = DurableResultStore(ctx.settings.data_dir / "pending_results.jsonl")
        self._pending_results: BoundedPendingQueue[dict[str, Any]] = DurableResultQueue(
            32, "channel/results", self._durable_store
        )
        self._record_restart_recovery()
        self._last_server_frame_at: float = 0.0
        self._last_login: str | None = None
        self._rest = rest_client
        self._sync = sync_engine
        self._chart = chart_service
        self._executor = executor
        # Enrolment state machine (§5.1: device enrolled first via
        # REST — the channel must not open the WS until REGISTERED).
        # Falls back to ctx.enrolment when not injected (app startup
        # wires it on the context); None = ungated (legacy callers).
        self._enrolment = enrolment if enrolment is not None else getattr(ctx, "enrolment", None)
        self._rest_heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def rest_client(self) -> RestClient | None:
        return self._rest

    @property
    def sync_engine(self) -> SyncEngine | None:
        return self._sync

    def _device_id(self) -> str:
        return self.ctx.get_or_create_device_id()

    def _telemetry(self):  # TelemetryEmitter | None (ARCHITECTURE-CONTRACT §8)
        return getattr(self.ctx, "telemetry", None)

    def _healing(self):  # SelfHealingRegistry | None (§8)
        return getattr(self.ctx, "self_healing", None)

    def _record_restart_recovery(self) -> None:
        """Bridge restart mid-operation (§8): persisted
        unacknowledged results were re-queued from the durable store
        at construction — record it in the self-healing matrix so the
        diagnostics summary shows the recovery."""
        healing = self._healing()
        if healing is None or len(self._pending_results) == 0:
            return
        healing.handle(
            Scenario.BRIDGE_RESTART_MID_OPERATION,
            HealingContext(persisted_results=len(self._pending_results)),
        )

    def _bound_account_id(self) -> str | None:
        login = self.ctx.driver.connection_state_dict().get("login")
        return str(login) if login is not None else None

    def _ensure_components(self) -> None:
        """Build the REST/sync/chart/command components (unless injected)."""
        device_id = self._device_id()
        if self._rest is None:
            self._rest = RestClient(self.ctx.settings, device_id, enrolment=self._enrolment)
            self.ctx.rest_client = self._rest
        elif self._enrolment is not None and getattr(self._rest, "enrolment", None) is None:
            # Injected REST client (app lifespan) — attach the state
            # machine so 401 re-enrolments are reflected in it (§3.2).
            self._rest.enrolment = self._enrolment
        if self._sync is None:
            self._sync = SyncEngine(self._rest, self.ctx)
            self.ctx.sync_engine = self._sync
        if self._chart is None:
            from ..server.chart import bind_mt5_fetchers

            fetch_candles, fetch_ticks, ensure_market_watch = bind_mt5_fetchers(self.ctx)
            self._chart = ChartService(
                fetch_candles=fetch_candles,
                fetch_ticks=fetch_ticks,
                ensure_market_watch=ensure_market_watch,
            )
        if self._executor is None:
            rest = self._rest
            validator = CommandValidator(
                device_id,
                get_account_id=self._bound_account_id,
                entitlements=lambda: rest.entitlements,
            )
            self._executor = CommandExecutor(
                self.ctx,
                validator,
                results_queue=self._pending_results,
                user_id=rest.user_id,
                telemetry=getattr(self.ctx, "telemetry", None),
            )

    def start(self) -> None:
        url = self.ctx.settings.resolved_server_ws_url
        try:
            validate_server_url(url)
        except ValueError as exc:
            logger.error("[CHANNEL] refusing to connect: {}", exc)
            state = self.ctx.server_channel_state
            state["enabled"] = False
            state["last_error"] = str(exc)
            return
        if not self.ctx.settings.server_token:
            logger.warning("[CHANNEL] server_token missing — outbound channel disabled")
            return
        self._ensure_components()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="nse-server-channel")
        self._start_rest_heartbeat()
        state = self.ctx.server_channel_state
        state["enabled"] = True
        state["url"] = url

    def _start_rest_heartbeat(self) -> None:
        """Start the REST heartbeat loop (§3.4), if not already running."""
        if self._rest_heartbeat_task is not None and not self._rest_heartbeat_task.done():
            return
        self._rest_heartbeat_task = asyncio.create_task(self._rest_heartbeat_loop(), name="nse-rest-heartbeat")

    async def _rest_heartbeat_loop(self) -> None:
        """REST heartbeat (§3.4) as the keep-alive when the WS is down.

        NOT a WS heartbeat substitute: it only proves REST reachability
        and reports ``pending_commands``. It never runs unconfigured or
        after a terminal auth failure (FAILED_AUTH — the key is dead,
        heartbeat would just 401). While enrolment is retrying it keeps
        running, so the server still sees liveness. If
        ``pending_commands > 0`` and no WS is live, the ``_run`` loop
        is already (re)connecting once enrolled — nothing extra to do
        here. A 401 here re-enrols through the REST client (§3.2).
        """
        interval = max(1.0, self.ctx.settings.heartbeat_interval_s)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            if self._enrolment is not None and self._enrolment.to_dict()["state"] in ("not_configured", "failed_auth"):
                continue
            rest = self._rest
            if rest is None or not self.ctx.settings.server_token:
                continue
            try:
                result = await rest.heartbeat()
            except Exception as exc:  # noqa: BLE001 — a failed heartbeat never kills the loop
                logger.debug("[CHANNEL] REST heartbeat failed: {}", type(exc).__name__)
                continue
            logger.debug("[CHANNEL] REST heartbeat ok (pending_commands={})", result.pending_commands)

    async def stop(self) -> None:
        """Clean shutdown (§5.4: close() frame; the supervisor exits)."""
        self._stop.set()
        heartbeat_task, self._rest_heartbeat_task = self._rest_heartbeat_task, None
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — shutdown must never raise
                logger.debug("[CHANNEL] REST heartbeat task await during stop failed: {}", type(exc).__name__)
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close(code=1000, reason="shutdown")
            except Exception as exc:  # noqa: BLE001 — shutdown must never raise
                logger.debug("[CHANNEL] ws.close during stop failed: {}", type(exc).__name__)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — shutdown must never raise
                    logger.debug("[CHANNEL] channel task await during stop failed: {}", type(exc).__name__)
            self._task = None
        if self._rest is not None:
            await self._rest.close()
        state = self.ctx.server_channel_state
        state["enabled"] = False
        state["connected"] = False

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        url = self.ctx.settings.resolved_server_ws_url
        attempt = 0
        while not self._stop.is_set():
            # §5.1 gate: identity is established by the prior
            # register-device call — never open the WS before the
            # device is REGISTERED. While enrolment is still retrying
            # this waits; on FAILED_AUTH the channel stays down with
            # the reason exposed (no retry with the same key, §3.1).
            if self._enrolment is not None:
                enrolled = await self._enrolment.wait_until_registered(self._stop)
                if not enrolled:
                    state = self.ctx.server_channel_state
                    state["connected"] = False
                    status = self._enrolment.to_dict()
                    state["last_error"] = f"enrolment {status['state']}" + (
                        f": {status['last_error']}" if status.get("last_error") else ""
                    )
                    logger.error("[CHANNEL] not connecting — {}", state["last_error"])
                    return
            try:
                logger.info("[CHANNEL] connecting to {}", url)
                import websockets  # lazy import keeps startup fast

                headers = {
                    "Authorization": f"Bearer {self.ctx.settings.server_token}",
                    "X-NSE-Device-Id": self._device_id(),
                }
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ping_interval=None,  # §5: no ping frames — the 15 s app heartbeat is liveness
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._last_server_frame_at = time.monotonic()
                    state = self.ctx.server_channel_state
                    state["connected"] = True
                    state["last_connected_at"] = time.time()
                    state["last_error"] = None
                    logger.info("[CHANNEL] connected to {}", url)
                    telemetry = self._telemetry()
                    if telemetry is not None:
                        telemetry.info(
                            "channel.connected",
                            component="realtime.channel",
                            details={"reconnect": attempt > 0, "attempt": attempt},
                        )
                    got_frame = await self._session(ws)
                    # Backoff resets on a SUCCESSFUL session (§5.4): a
                    # session that exchanged at least one frame counts.
                    attempt = 0 if got_frame else attempt + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — any disconnect error restarts the loop
                state = self.ctx.server_channel_state
                state["connected"] = False
                state["reconnects"] = int(state.get("reconnects", 0)) + 1
                state["last_error"] = type(exc).__name__
                logger.warning("[CHANNEL] disconnected: {} — reconnecting", type(exc).__name__)
                telemetry = self._telemetry()
                if telemetry is not None:
                    telemetry.warn(
                        "channel.disconnected",
                        component="realtime.channel",
                        error_code=type(exc).__name__,
                        details={"reconnects": state["reconnects"]},
                    )
                healing = self._healing()
                if healing is not None:
                    healing.handle(
                        Scenario.WS_DROP,
                        HealingContext(ws_connected=False, ws_was_connected=True),
                    )
                attempt += 1
            finally:
                self._ws = None
                state = self.ctx.server_channel_state
                state["connected"] = False
            if self._stop.is_set():
                break
            delay = backoff_with_jitter(
                backoff_delay(
                    attempt,
                    initial_s=self.ctx.settings.reconnect_initial_ms / 1000.0,
                    cap_s=self.ctx.settings.reconnect_max_ms / 1000.0,
                )
            )
            logger.info("[CHANNEL] reconnect in {:.2f}s (attempt {})", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    # ------------------------------------------------------------------
    async def _session(self, ws: Any) -> bool:
        """One WS session. Returns True when at least one frame was
        received from the server (a successful session, §5.4)."""
        self._last_server_frame_at = time.monotonic()
        # Re-send any results that never made it out (§6.5: results
        # persist in the bounded pending queue across reconnects).
        # Clear the wire queue first: anything still queued from the
        # previous session is either a chart frame (re-derivable) or a
        # result that is ALSO in the pending queue — re-enqueueing
        # from the pending queue alone avoids duplicates.
        self._outbound.clear()
        for result in self._pending_results.snapshot():
            self._outbound.put_result(result)
        receiver = asyncio.create_task(self._receive_loop(ws), name="nse-channel-recv")
        heartbeat = asyncio.create_task(self._heartbeat_loop(ws), name="nse-channel-hb")
        sender = asyncio.create_task(self._send_loop(ws), name="nse-channel-send")
        chart_poll = asyncio.create_task(self._chart_poll_loop(), name="nse-channel-chart")
        got_frame = False
        try:
            done, _pending = await asyncio.wait(
                {receiver, heartbeat, sender, chart_poll},
                return_when=asyncio.FIRST_COMPLETED,
            )
            got_frame = getattr(receiver, "_got_frame", False)
            for task in done:
                if task is receiver:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        finally:
            for task in (receiver, heartbeat, sender, chart_poll):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — teardown must never raise
                    logger.debug("[CHANNEL] session task teardown failed: {}", type(exc).__name__)
            if self._chart is not None:
                # Subscriptions die with the WS session (§17.1.3); the
                # server re-subscribes on reconnect with fresh ids.
                self._chart.drop_all_subscriptions()
        return got_frame

    async def _receive_loop(self, ws: Any) -> None:
        got_frame = False
        try:
            async for raw in ws:
                got_frame = True
                self._last_server_frame_at = time.monotonic()
                await self._handle_server_message(raw)
        finally:
            # Stash the flag on the task for _session to read.
            current = asyncio.current_task()
            if current is not None:
                current._got_frame = got_frame  # type: ignore[attr-defined]

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send the 15 s app heartbeat (§5.3); detect a dead session
        after 45 s without ANY frame from the server."""
        interval = max(1.0, self.ctx.settings.heartbeat_interval_s)
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() - self._last_server_frame_at > self.dead_after_s:
                logger.warning(
                    "[CHANNEL] no server frame for {:.0f}s — session dead (§5.3), reconnecting",
                    self.dead_after_s,
                )
                telemetry = self._telemetry()
                if telemetry is not None:
                    telemetry.warn(
                        "channel.session_dead",
                        component="realtime.channel",
                        error_code="heartbeat_timeout",
                        details={"dead_after_s": self.dead_after_s},
                    )
                healing = self._healing()
                if healing is not None:
                    healing.handle(
                        Scenario.WS_DROP,
                        HealingContext(ws_connected=False, ws_was_connected=True),
                    )
                try:
                    await ws.close(code=1000, reason="heartbeat timeout")
                except Exception as exc:  # noqa: BLE001 — close failure still ends the session
                    logger.debug("[CHANNEL] ws.close after heartbeat timeout failed: {}", type(exc).__name__)
                return
            # Account-switch detection (§7.3): a changed bound login
            # invalidates pending old-account commands and triggers a
            # fresh sync (account → positions → history).
            login = self._bound_account_id()
            if login is not None and self._last_login is not None and login != self._last_login:
                logger.warning("[CHANNEL] MT5 account switched {} → {} — fresh sync (§7.3)", self._last_login, login)
                if self._sync is not None:
                    self._sync.reset_for_account_switch()
                    asyncio.create_task(self._sync.handle_sync_request())
            self._last_login = login
            try:
                await ws.send(json.dumps({"type": "heartbeat", "device_id": self._device_id()}))
                self.ctx.server_channel_state["last_heartbeat_at"] = time.time()
            except Exception as exc:  # noqa: BLE001 — a failed send ends the session
                logger.debug("[CHANNEL] heartbeat send failed: {}", type(exc).__name__)
                return

    async def _send_loop(self, ws: Any) -> None:
        while True:
            frame = await self._outbound.get()
            try:
                await ws.send(json.dumps(frame, default=str))
            except Exception as exc:  # noqa: BLE001 — a failed send ends the session
                logger.debug("[CHANNEL] frame send failed: {}", type(exc).__name__)
                # Re-queue results (they persist in the pending queue
                # anyway); chart frames are dropped — re-derivable.
                if frame.get("event_type") == "action.result":
                    self._outbound.put_result(frame)
                return
            if frame.get("event_type") == "action.result":
                correlation = frame.get("correlation_id")
                if correlation:
                    self._pending_results.remove(
                        lambda r, correlation=correlation: r.get("correlation_id") == correlation
                    )

    async def _chart_poll_loop(self) -> None:
        """Poll due chart subscriptions (§17.1) and enqueue deltas."""
        while True:
            await asyncio.sleep(1.0)
            if self._chart is None or self._ws is None:
                continue
            now = time.monotonic()
            for sub in self._chart.poll_due(now):
                asyncio.create_task(self._poll_subscription(sub))

    async def _poll_subscription(self, sub: Any) -> None:
        if self._chart is None:
            return
        try:
            candles = await run_in_threadpool(
                self._chart._fetch_with_prerequisite,
                sub.symbol,
                sub.timeframe,
                100,
                None,
                None,
            )
        except ChartError as exc:
            frame = build_chart_error_frame(
                request_id=None,
                subscription_id=sub.subscription_id,
                symbol=sub.symbol,
                timeframe=sub.timeframe,
                code=exc.code,
                message=exc.message,
            )
            self._enqueue_chart(frame)
            return
        delta = self._chart.build_delta(sub, candles)
        if not delta:
            return
        if not self._chart.rate_limiter.allow():
            self._enqueue_chart(
                build_chart_error_frame(
                    request_id=None,
                    subscription_id=sub.subscription_id,
                    symbol=sub.symbol,
                    timeframe=sub.timeframe,
                    code="rate_limited",
                    message="chart frame budget (60/min) exceeded (§17.1.3)",
                )
            )
            return
        data = build_chart_data_frame(
            request_id=None,
            subscription_id=sub.subscription_id,
            symbol=sub.symbol,
            timeframe=sub.timeframe,
            candles=delta,
            broker_time=delta[-1]["time"],
        )
        self._enqueue_chart(data)
        if sub.include_ticks and self._chart.fetch_ticks is not None:
            try:
                ticks = await run_in_threadpool(self._chart.fetch_ticks, sub.symbol, 100, None, None)
            except ChartError:
                return
            if ticks and self._chart.rate_limiter.allow():
                self._enqueue_chart(
                    build_chart_tick_frame(
                        subscription_id=sub.subscription_id,
                        request_id=None,
                        symbol=sub.symbol,
                        ticks=ticks,
                        broker_time=ticks[-1]["time_ms"],
                    )
                )

    # ------------------------------------------------------------------
    def _enqueue_result(self, result: dict[str, Any]) -> None:
        self._outbound.put_result(result)

    def _enqueue_chart(self, frame: dict[str, Any]) -> None:
        if self._chart is not None and not self._chart.rate_limiter.allow():
            logger.warning("[CHANNEL] chart frame budget exceeded — dropping frame (§17.1.3)")
            return
        self._outbound.put_chart(frame)

    async def _handle_server_message(self, raw: Any) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            logger.warning("[CHANNEL] non-JSON message from server: {}", str(raw)[:200])
            return
        if not isinstance(msg, dict):
            logger.warning("[CHANNEL] non-object message from server — ignored")
            return
        mtype = msg.get("type")
        if mtype == "command":
            asyncio.create_task(self._handle_command(msg))
        elif mtype == "sync_request":
            logger.info("[CHANNEL] sync_request — re-sending account, positions, history (§5.2)")
            if self._sync is not None:
                asyncio.create_task(self._sync.handle_sync_request())
        elif mtype == "ack":
            # Informational only (§5.2); the REST 200 ack is authoritative.
            logger.debug("[CHANNEL] server ack for batch {}", msg.get("batch_id"))
        elif mtype == "heartbeat":
            self.ctx.server_channel_state["last_server_heartbeat_at"] = time.time()
        elif mtype == "chart.request":
            await self._handle_chart_request(msg)
        elif mtype == "chart.subscribe":
            if self._chart is not None:
                error_frame = self._chart.handle_subscribe(msg)
                if error_frame is not None:
                    self._enqueue_chart(error_frame)
        elif mtype == "chart.unsubscribe":
            if self._chart is not None:
                self._chart.handle_unsubscribe(msg)
        else:
            # Unknown frames are silently ignored, never fatal (§5.2).
            logger.debug("[CHANNEL] ignored server message type={}", mtype)

    async def _handle_chart_request(self, msg: dict[str, Any]) -> None:
        if self._chart is None:
            return
        try:
            frames = await asyncio.wait_for(
                run_in_threadpool(self._chart.handle_request, msg),
                timeout=CHART_REQUEST_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "[CHANNEL] chart.request {} timed out after {}s (§17.1.3)",
                msg.get("request_id"),
                CHART_REQUEST_TIMEOUT_S,
            )
            frames = [
                build_chart_error_frame(
                    request_id=msg.get("request_id"),
                    subscription_id=None,
                    symbol=str(msg.get("symbol") or ""),
                    timeframe=str(msg.get("timeframe") or ""),
                    code="chart_timeout",
                    message="MT5 chart call exceeded the 30 s timeout",
                )
            ]
        for frame in frames:
            self._enqueue_chart(frame)

    async def _handle_command(self, msg: dict[str, Any]) -> None:
        if self._executor is None:
            return
        try:
            _, result = await run_in_threadpool(self._executor.execute, msg)
        except Exception as exc:  # noqa: BLE001 — a command must never kill the channel
            logger.exception("[CHANNEL] command handler crashed: {}", type(exc).__name__)
            return
        self._enqueue_result(result)

    # ------------------------------------------------------------------
    def build_manifest_payload(self) -> dict[str, Any]:
        """Current §4.1 manifest payload (sent over REST by the caller)."""
        return build_manifest(self._device_id(), driver=self.ctx.driver)
