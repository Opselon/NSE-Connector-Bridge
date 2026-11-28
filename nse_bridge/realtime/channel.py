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

Frames, client → server (§5.3 ONLY, plus §4.1):

- ``heartbeat`` ``{type, device_id}`` every 15 s (server replies
  ``heartbeat``; session dead after 45 s without one)
- ``action.result`` (§6.5)
- ``chart.data``, ``chart.tick`` (§17.1)
- ``manifest.update`` envelope (``event_type: "manifest.update"``,
  §4.1) when the manifest changes mid-session; the server treats it
  the same as the REST manifest POST

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

WS state machine (B2 — the dashboard reads this, never a bare
"RETRY"): ``ctx.server_channel_state`` carries ``ws_state``
(``disconnected`` / ``connecting`` / ``connected`` / ``retrying`` /
``auth_failed`` / ``blocked_enrolment`` / ``stopped``),
``last_error_kind`` (``ws_auth`` / ``ws_transport`` / ``enrolment`` /
``config``), ``last_error_detail`` (the exact failure, verbatim where
the server supplied it — e.g. ``WS handshake failed: HTTP 401
key_revoked — key revoked``), and ``next_attempt_at`` (ISO-8601 UTC
of the next reconnect attempt). A WS-auth failure (HTTP 401/403 on
the handshake) is a DIFFERENT failure from an enrolment failure:
the key can validate over REST while the WS handshake 401s (stale
device session). After ``MAX_WS_AUTH_FAILURES`` consecutive WS-auth
failures the channel asks enrolment to revalidate once
(``request_revalidation``) instead of hammering the handshake with
the same key; if the key itself is bad that revalidation lands in
FAILED_AUTH and the channel stays down with the reason exposed.

Heartbeats (B2 — the dashboard must never show "—" for a heartbeat
that happened): every heartbeat SENT and every heartbeat RECEIVED is
recorded in ``ctx.server_channel_state`` as an epoch
(``last_heartbeat_at`` / ``last_server_heartbeat_at``, the fields the
dashboard already reads) AND as ISO-8601 UTC + local timestamps
(``last_heartbeat_sent_at`` / ``last_heartbeat_received_at`` and
their ``_local`` twins), with ``heartbeat_via`` (``"ws"`` or
``"rest"``). The REST heartbeat (§3.4) is a heartbeat too: while the
WS is down it is the keep-alive the server records, so it updates
the same fields — the 2026-11-26 incident showed "Last heartbeat —"
while the server recorded heartbeats in the same minute, because
only WS heartbeats were ever recorded.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from starlette.concurrency import run_in_threadpool

from .. import BRIDGE_VERSION
from ..config import validate_server_url
from ..engine.common import BridgeContext
from ..mt5.driver import MT5_PRIORITY_BACKGROUND, mt5_priority_scope
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
    modify_pending_gate_open,
)
from ..server.manifest import ManifestTracker, build_manifest, build_manifest_update_envelope
from ..server.policy import POLICY_FRAME_TYPE
from ..server.queue import BoundedPendingQueue
from ..server.rest import RestClient
from ..server.sync import SyncEngine
from ..server.sync_checkpoint import CHECKPOINT_FILENAME, SyncCheckpointStore

__all__ = ["ServerChannel", "backoff_delay", "backoff_with_jitter", "classify_ws_connect_error"]

HEARTBEAT_DEAD_S = 45.0  # §5.3: session dead after 45 s without a frame
OUTBOUND_MAX = 128  # §5.3/§5.4: bounded channels, 128 messages each way
BACKOFF_INITIAL_S = 0.5  # §5.4: initial 500 ms
BACKOFF_CAP_S = 30.0  # §5.4: capped at 30 s
BACKOFF_FACTOR = 2.0  # §5.4: doubling
JITTER_MAX_S = 0.1  # §5.4: jitter of subsec_nanos % 100 ms
#: Consecutive WS-handshake auth failures (HTTP 401/403) after which
#: the channel asks enrolment to revalidate once instead of hammering
#: the handshake with the same key. N = 5: enough to rule out a
#: transient handshake glitch, small enough that a stale session is
#: re-registered within ~a minute of backoff. The count resets on any
#: successful session or non-auth failure.
MAX_WS_AUTH_FAILURES = 5


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


def classify_ws_connect_error(exc: BaseException) -> dict[str, Any]:
    """Classify a WS connect failure for the state machine + UI.

    Returns ``{"kind", "http_status", "code", "message", "detail"}``:

    - ``kind == "ws_auth"`` — the handshake got HTTP 401/403. The
      server's reason is carried verbatim where the handshake
      response body had one (``error.code`` / ``error.message``,
      e.g. ``key_revoked``) — a WS-auth failure is NOT an enrolment
      failure and must not be displayed as one.
    - ``kind == "ws_transport"`` — handshake timeout, refused
      connection, or any non-HTTP failure.
    - ``kind == "config"`` is never produced here (start-time config
      errors are recorded by ``start()`` directly).

    Duck-typed on ``exc.response.status_code`` /
    ``exc.response.body`` (``websockets.exceptions.InvalidStatus``)
    so fakes can drive it in tests without a socket.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        code: str | None = None
        message: str | None = None
        body = getattr(response, "body", b"") or b""
        if isinstance(body, (bytes, bytearray)):
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    if isinstance(error.get("code"), str):
                        code = error["code"]
                    if isinstance(error.get("message"), str):
                        message = error["message"]
                elif isinstance(payload.get("code"), str):
                    code = payload["code"]
                elif isinstance(payload.get("message"), str):
                    message = payload["message"]
        detail = f"WS handshake failed: HTTP {status}"
        if code:
            detail += f" {code}"
        if message:
            detail += f" — {message}"
        return {
            "kind": "ws_auth" if status in (401, 403) else "ws_transport",
            "http_status": status,
            "code": code,
            "message": message,
            "detail": detail,
        }
    if isinstance(exc, TimeoutError):
        return {
            "kind": "ws_transport",
            "http_status": None,
            "code": "handshake_timeout",
            "message": None,
            "detail": "WS handshake timed out (no response from server)",
        }
    return {
        "kind": "ws_transport",
        "http_status": None,
        "code": None,
        "message": None,
        "detail": f"{type(exc).__name__}: {exc}"[:200],
    }


class _OutboundQueue:
    """Priority outbound queue (§5.3): results first, chart frames second.

    Two FIFO deques sharing one 128-message bound, drained through a
    single consumer wake-up event. When full, the oldest unsent CHART
    frame is dropped with a warning (chart data is re-derivable); a
    result is dropped from the wire only when the queue holds no chart
    frame at all — and it stays in the pending results queue for
    redelivery on reconnect, so it is never lost silently. Heartbeats
    bypass this queue entirely.

    The deque+event shape (instead of racing two asyncio.Queue gets
    with FIRST_COMPLETED) makes priority draining exact: an item is
    popped only by the consumer, so a completing waiter can never
    silently swallow a chart frame while a result is returned.
    """

    def __init__(self, maxsize: int = OUTBOUND_MAX) -> None:
        self.maxsize = maxsize
        self._results: deque[dict[str, Any]] = deque()
        self._charts: deque[dict[str, Any]] = deque()
        self._item_ready = asyncio.Event()

    def _total(self) -> int:
        return len(self._results) + len(self._charts)

    @property
    def total_queued(self) -> int:
        """Messages currently queued (results + charts) — surfaced in
        the channel state so health can show the bounded queue depth
        instead of letting it grow invisibly while sending is gated."""
        return self._total()

    def put_result(self, frame: dict[str, Any]) -> None:
        if self._total() >= self.maxsize:
            if self._charts:
                dropped = self._charts.popleft()
                logger.warning(
                    "[CHANNEL] outbound full — dropped oldest chart frame "
                    "(symbol={}) to make room for a command result",
                    dropped.get("symbol"),
                )
            else:
                dropped = self._results.popleft()
                logger.warning(
                    "[CHANNEL] outbound full of results — dropped oldest result "
                    "frame (correlation_id={}); it stays in the pending queue "
                    "for redelivery on reconnect",
                    dropped.get("correlation_id"),
                )
        self._results.append(frame)
        self._item_ready.set()

    def put_chart(self, frame: dict[str, Any]) -> bool:
        """Enqueue a chart frame. Returns False when the frame was
        dropped (queue full of results — chart data is re-derivable)."""
        if self._total() >= self.maxsize:
            if self._charts:
                self._charts.popleft()
                self._charts.append(frame)
                self._item_ready.set()
                return True
            logger.warning(
                "[CHANNEL] outbound full of results — dropping chart frame "
                "(symbol={}); chart data is re-derivable by re-request",
                frame.get("symbol"),
            )
            return False
        self._charts.append(frame)
        self._item_ready.set()
        return True

    def clear(self) -> None:
        """Drop everything queued. Chart frames are re-derivable; any
        result dropped here MUST still live in the pending results
        queue (callers re-enqueue from there)."""
        self._results.clear()
        self._charts.clear()
        self._item_ready.clear()

    async def get(self) -> dict[str, Any]:
        """Pop the highest-priority frame, waiting when empty.

        Wake-up discipline: the event is cleared BEFORE the emptiness
        check and every producer sets it after appending, so an item
        that arrives between the check and the wait can never be
        missed.
        """
        while True:
            self._item_ready.clear()
            if self._results:
                return self._results.popleft()
            if self._charts:
                return self._charts.popleft()
            await self._item_ready.wait()


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
        policy_service: Any | None = None,
        enrolment: Any | None = None,
        manifest_tracker: ManifestTracker | None = None,
        dead_after_s: float = HEARTBEAT_DEAD_S,
    ) -> None:
        self.ctx = ctx
        self.bus = bus  # kept for the local dashboard bus; the server channel no longer consumes it
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Set by _request_ws_open() to interrupt a reconnect backoff
        # immediately (§3.4: pending_commands > 0 with no WS live must
        # open/refresh the WS now, not after the backoff expires).
        self._wake = asyncio.Event()
        self._ws: Any = None
        self.dead_after_s = dead_after_s
        self._outbound = _OutboundQueue()
        # Lifecycle mutual exclusion: start()/stop() are reachable from
        # concurrent callers (app startup, PUT /settings channel
        # restart, POST /server_channel/restart, §3.4 wake) — without a
        # lock two supervisors could run at once and the first would be
        # orphaned when self._task is overwritten.
        self._lifecycle_lock = asyncio.Lock()
        # Set by the receive loop on the first frame of the CURRENT
        # session (a plain attribute, not a task-stashed flag: the
        # session can end via the sender/heartbeat while the receiver
        # is still running, and a flag read from a not-yet-finished
        # task reported healthy sessions as failed — backoff never
        # reset).
        self._session_got_frame = False
        # One account-switch sync at a time (§7.3): the heartbeat loop
        # detects a login change every tick — without a guard two
        # consecutive heartbeats during a long sync each spawn a
        # concurrent history sync (double fetch, duplicate batches).
        self._account_switch_sync_task: asyncio.Task[None] | None = None
        # Chart subscriptions with a poll already executing (their MT5
        # fetch can take seconds; without this guard a slow fetch
        # stacks a new task per 1 s scheduler tick for the same sub).
        self._chart_poll_inflight: set[str] = set()
        # §5.3 client-side liveness: the session is dead when the
        # server stops REPLYING to heartbeats — any other server frame
        # does not prove the heartbeat path is alive (contract §5.3:
        # the server MUST reply to each client heartbeat). Updated on
        # session start and on every server ``heartbeat`` frame.
        self._last_heartbeat_reply_at: float = 0.0
        # Pending results are DURABLE (§6.5 + reliability phase): an
        # in-memory-only queue loses acknowledged results on a Bridge
        # restart mid-operation. The store file is created lazily on
        # the first persisted result; construction only reads it.
        self._durable_store = DurableResultStore(ctx.settings.data_dir / "pending_results.jsonl")
        self._pending_results: BoundedPendingQueue[dict[str, Any]] = DurableResultQueue(
            32, "channel/results", self._durable_store
        )
        self._record_restart_recovery()
        # Consecutive WS-handshake auth failures (HTTP 401/403).
        # At MAX_WS_AUTH_FAILURES the channel asks enrolment to
        # revalidate once instead of hammering the handshake; the
        # count resets on any success or non-auth failure.
        self._ws_auth_failures = 0
        # In-flight chart.request tasks (latency hot spot 1): chart
        # fetches run off the receive loop so a slow MT5 fetch can
        # never delay the next incoming command. Tracked so session
        # teardown can cancel them (like the other session tasks) and
        # so a finished task is never GC'd mid-flight.
        self._chart_request_tasks: set[asyncio.Task[None]] = set()
        self._last_server_frame_at: float = 0.0
        self._last_login: str | None = None
        self._rest = rest_client
        self._sync = sync_engine
        self._chart = chart_service
        self._executor = executor
        # W6: policy-envelope gate + trailing/breakeven executor (built
        # in _ensure_components; injectable for tests via the kwarg).
        self._policy: Any = policy_service
        # Enrolment state machine (§5.1: device enrolled first via
        # REST — the channel must not open the WS until REGISTERED).
        # Falls back to ctx.enrolment when not injected (app startup
        # wires it on the context); None = ungated (legacy callers).
        self._enrolment = enrolment if enrolment is not None else getattr(ctx, "enrolment", None)
        # Manifest tracker (§4.1), shared with the enrolment manager
        # when it has one so REST + WS agree on what the server has.
        self._manifest_tracker = (
            manifest_tracker or getattr(self._enrolment, "manifest_tracker", None) or ManifestTracker()
        )
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

    def recent_command_executions(self, limit: int = 50) -> dict[str, Any]:
        """Recent server-channel command lifecycle for the C4 GUI.

        ``executions`` is the executor's bounded in-memory history
        (true local lifecycle states, newest first); ``pending_results``
        is the count of ``action.result`` frames still queued for
        delivery to the server (§6.5, durable across restarts). When
        the executor has never been created (channel never opened) the
        history is empty — never a fabricated entry.
        """
        executor = self._executor
        return {
            "executions": executor.recent_executions(limit) if executor is not None else [],
            "pending_results": len(self._pending_results),
            "executor_available": executor is not None,
        }

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

    # ------------------------------------------------------------------
    # B2 state machine: heartbeat timestamps + queue/send-gate state.
    # Every heartbeat — WS or REST — is recorded here, as an epoch
    # (the fields the dashboard already reads) AND as ISO-8601 UTC +
    # local timestamps, with ``heartbeat_via`` naming the transport.
    # A heartbeat the server recorded must never display as "—".
    # ------------------------------------------------------------------
    @staticmethod
    def _iso_now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _iso_local_now() -> str:
        return datetime.now().astimezone().isoformat()

    def _note_heartbeat_sent(self, via: str) -> None:
        state = self.ctx.server_channel_state
        state["last_heartbeat_at"] = time.time()
        state["last_heartbeat_sent_at"] = self._iso_now()
        state["last_heartbeat_sent_at_local"] = self._iso_local_now()
        state["heartbeat_via"] = via

    def _note_heartbeat_received(self, via: str, *, server_time: str | None = None) -> None:
        state = self.ctx.server_channel_state
        state["last_server_heartbeat_at"] = time.time()
        state["last_heartbeat_received_at"] = self._iso_now()
        state["last_heartbeat_received_at_local"] = self._iso_local_now()
        state["heartbeat_via"] = via
        if server_time is not None:
            # The server's own clock at the heartbeat (§3.4 response).
            state["last_heartbeat_server_time"] = server_time

    def _refresh_queue_state(self) -> None:
        """Publish bounded-queue depth + send-gate state.

        Data sending is gated on enrolment (§5.1: the channel must
        not open, and sync must not send, before REGISTERED). While
        gated the outbound queue is BOUNDED (128) and the pending
        results queue is BOUNDED (32, durable) — they cannot grow
        without bound — but health must say so honestly instead of
        letting a stuck queue look like a healthy idle: ``send_gated``
        names the gate and ``send_gated_reason`` the exact enrolment
        state + server reason behind it.
        """
        state = self.ctx.server_channel_state
        state["outbound_queued"] = self._outbound.total_queued
        state["pending_results"] = len(self._pending_results)
        if self._enrolment is not None:
            enrol = self._enrolment.to_dict()
            gated = enrol.get("state") != "registered"
            state["send_gated"] = gated
            if gated:
                reason = enrol.get("last_error_message") or enrol.get("last_error") or enrol.get("state")
                state["send_gated_reason"] = f"enrolment {enrol.get('state')}: {reason}"
            else:
                state["send_gated_reason"] = None
        else:
            state["send_gated"] = False
            state["send_gated_reason"] = None

    # ------------------------------------------------------------------
    # REST heartbeat → WS (§3.4) + manifest (§4.1)
    # ------------------------------------------------------------------
    def _is_ws_live(self) -> bool:
        """True when a WS session is currently up."""
        return self._ws is not None and bool(self.ctx.server_channel_state.get("connected"))

    def _request_ws_open(self) -> bool:
        """Open/refresh the WS now (§3.4: ``pending_commands > 0`` and
        no WS live). Returns True when an open was (re)triggered.

        - WS live → nothing to do.
        - The ``_run`` supervisor is alive (connecting, or backing
          off between attempts) → wake it so it reconnects
          immediately instead of sleeping out the backoff.
        - The supervisor is not running at all (and the channel is
          not stopping) → ``start()`` it, provided enrolment allows
          a connection (REGISTERED, or ungated legacy callers).
        """
        if self._is_ws_live():
            return False
        if self._task is not None and not self._task.done():
            self._wake.set()
            logger.info("[CHANNEL] pending commands with no WS live — waking reconnect (§3.4)")
            return True
        if self._stop.is_set():
            return False
        if self._enrolment is not None and self._enrolment.to_dict().get("state") != "registered":
            return False
        logger.info("[CHANNEL] pending commands with no WS live — (re)opening the channel (§3.4)")
        self.start()
        return True

    async def _wait_backoff(self, delay: float) -> None:
        """Sleep ``delay`` unless stopped or woken (``_request_ws_open``).

        A wake interrupts the backoff immediately; the wait consumes
        the wake event so a stale wake cannot skip a later backoff.
        """
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_task, wake_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            self._wake.clear()

    def _maybe_send_manifest_update(self) -> bool:
        """Send the §4.1 ``manifest.update`` WS envelope if the
        manifest changed since the server last saw it.

        Returns True when a frame was enqueued. The frame is a
        ``MessageEnvelope`` with ``event_type: "manifest.update"``
        (contract §4.1) carrying the same payload the REST POST
        carries; the server treats it the same as the REST post. The
        hash is marked sent once enqueued — the REST post at
        enrolment is the authoritative delivery, so a frame dropped
        later by a full outbound queue loses nothing the server does
        not already have.
        """
        manifest = build_manifest(self._device_id(), driver=self.ctx.driver)
        if not self._manifest_tracker.is_changed(manifest):
            return False
        envelope = build_manifest_update_envelope(
            manifest,
            user_id=self._rest.user_id if self._rest is not None else None,
        )
        self._outbound.put_result(envelope)
        self._manifest_tracker.mark_sent(manifest)
        logger.info("[CHANNEL] manifest changed — sent manifest.update (hash {})", manifest["manifest_hash"])
        return True

    async def _maybe_publish_manifest_rest(self) -> None:
        """POST the §4.1 manifest over REST when the WS is down.

        Best-effort: a failure is logged and retried on the next
        change / heartbeat — it must never kill the heartbeat loop.
        """
        if self._rest is None:
            return
        try:
            manifest = build_manifest(self._device_id(), driver=self.ctx.driver)
            await self._manifest_tracker.send_via_rest(self._rest, manifest)
        except Exception as exc:  # noqa: BLE001 — manifest must never break the loop
            logger.warning("[CHANNEL] manifest REST post failed ({}); will retry", type(exc).__name__)

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
            self._sync = SyncEngine(
                self._rest,
                self.ctx,
                checkpoint_store=SyncCheckpointStore(self.ctx.settings.data_dir / CHECKPOINT_FILENAME),
            )
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
            # B3 (P3 v2): the v2 flag is the flag half of the
            # open_order gate — the other half is the negotiated v2
            # envelope, checked per command in the validator. The
            # flag defaults ON (``BridgeSettings.enable_v2_open_order``;
            # opt-out via ``NSE_BRIDGE_ENABLE_V2_OPEN_ORDER=0``).
            # The flag alone never opens an open; without it, v2 opens
            # stay UnsupportedOperation.
            # §17.4 (P0-1): the modify_pending gate is the same shape
            # with the opposite default — explicit opt-in
            # (``BridgeSettings.enable_modify_pending``; opt-in via
            # ``NSE_BRIDGE_ENABLE_MODIFY_PENDING=1``) AND a qualifying
            # local version (>= MODIFY_PENDING_MIN_VERSION "1.4.1").
            # No wire channel carries the minimum version (the Platform
            # never sends one), so the version floor is checked
            # locally against this Bridge's own version; the Platform
            # enforces the same floor server-side before sending.
            validator = CommandValidator(
                device_id,
                get_account_id=self._bound_account_id,
                entitlements=lambda: rest.entitlements,
                modify_pending_proven=modify_pending_gate_open(
                    enabled=self.ctx.settings.enable_modify_pending,
                    bridge_version=BRIDGE_VERSION,
                ),
                open_order_authorized=self.ctx.settings.enable_v2_open_order,
            )
            self._executor = CommandExecutor(
                self.ctx,
                validator,
                results_queue=self._pending_results,
                user_id=rest.user_id,
                telemetry=getattr(self.ctx, "telemetry", None),
            )
        if self._policy is None:
            # W6 (D-5): the policy-envelope gate + trailing/breakeven
            # executor. Dormant until a valid, fresh ``policy.envelope``
            # frame arrives (CONTRACT v1 §5.2, defined by the Platform
            # slice 8); unknown frames are ignored, so the service
            # stays dormant in production until the Platform sends one.
            # Executed/failed moves persist in the durable policy-move
            # store (P1-8) when the app wired one onto the context.
            from ..server.policy import PolicyService

            self._policy = PolicyService(
                self.ctx,
                telemetry=getattr(self.ctx, "telemetry", None),
                move_store=getattr(self.ctx, "policy_move_store", None),
            )

    def start(self) -> None:
        """Start the supervised channel exactly once.

        A second start() while a supervisor is alive is a no-op —
        callers race (app startup, settings PUT, restart endpoint, §3.4
        wake), and an unconditional create_task would orphan the first
        supervisor while overwriting ``self._task`` (double connections
        to the Platform, duplicated results).
        """
        if self.running:
            logger.debug("[CHANNEL] start() while already running — no-op")
            return
        url = self.ctx.settings.resolved_server_ws_url
        try:
            validate_server_url(url)
        except ValueError as exc:
            logger.error("[CHANNEL] refusing to connect: {}", exc)
            state = self.ctx.server_channel_state
            state["enabled"] = False
            state["ws_state"] = "stopped"
            state["last_error_kind"] = "config"
            state["last_error_detail"] = str(exc)
            state["last_error"] = str(exc)
            return
        if not self.ctx.settings.server_token:
            logger.warning("[CHANNEL] server_token missing — outbound channel disabled")
            state = self.ctx.server_channel_state
            state["ws_state"] = "stopped"
            state["last_error_kind"] = "config"
            state["last_error_detail"] = "server_token missing — outbound channel disabled"
            state["last_error"] = state["last_error_detail"]
            return
        self._ensure_components()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="nse-server-channel")
        self._start_rest_heartbeat()
        state = self.ctx.server_channel_state
        state["enabled"] = True
        state["url"] = url
        state["ws_state"] = "connecting"

    def _start_rest_heartbeat(self) -> None:
        """Start the REST heartbeat loop (§3.4), if not already running."""
        if self._rest_heartbeat_task is not None and not self._rest_heartbeat_task.done():
            return
        self._rest_heartbeat_task = asyncio.create_task(self._rest_heartbeat_loop(), name="nse-rest-heartbeat")

    async def _rest_heartbeat_loop(self) -> None:
        """REST heartbeat (§3.4) as the keep-alive when the WS is down.

        NOT a WS heartbeat substitute: it only proves REST reachability
        and reports ``pending_commands``. While a WS session is LIVE
        the loop idles — the WS application heartbeat is the liveness
        mechanism and the server already stamps the session row from
        it (§5.3); the REST heartbeat would be a redundant round-trip.
        It never runs unconfigured or after a terminal auth failure
        (FAILED_AUTH — the key is dead, heartbeat would just 401).
        While enrolment is retrying it keeps running, so the server
        still sees liveness. If ``pending_commands > 0`` and no WS is
        live, it wakes the WS supervisor immediately via
        ``_request_ws_open`` (§3.4) — the ``_run`` loop on its own
        only retries on its backoff schedule, so without the wake a
        pending command could wait up to 30 s. A 401 here re-enrols
        through the REST client (§3.2).
        """
        interval = max(1.0, self.ctx.settings.heartbeat_interval_s)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            if self._is_ws_live():
                continue  # WS heartbeat owns liveness while a session is up
            if self._enrolment is not None and self._enrolment.to_dict()["state"] in ("not_configured", "failed_auth"):
                continue
            rest = self._rest
            if rest is None or not self.ctx.settings.server_token:
                continue
            try:
                # The REST heartbeat is a real heartbeat: the server
                # records it while the WS is down, so it updates the
                # same last-heartbeat fields the dashboard reads —
                # otherwise the dashboard shows "—" while the server
                # recorded heartbeats in the same minute (2026-11-26).
                self._note_heartbeat_sent("rest")
                result = await rest.heartbeat()
            except Exception as exc:  # noqa: BLE001 — a failed heartbeat never kills the loop
                logger.debug("[CHANNEL] REST heartbeat failed: {}", type(exc).__name__)
                continue
            self._note_heartbeat_received("rest", server_time=result.server_time)
            self._refresh_queue_state()
            logger.debug("[CHANNEL] REST heartbeat ok (pending_commands={})", result.pending_commands)
            # §3.4 (verified behaviour — see _request_ws_open): the
            # server says commands are waiting and no WS is live, so
            # open/refresh the WS now instead of waiting out the
            # reconnect backoff. The WS loop on its own only retries
            # on its backoff schedule; without this wake a pending
            # command could sit undelivered for up to 30 s.
            if result.pending_commands > 0 and not self._is_ws_live():
                self._request_ws_open()
            # §4.1: while the WS is down, a changed manifest still
            # reaches the server over REST (the WS frame is the
            # mid-session path; the REST post is the offline path).
            if not self._is_ws_live():
                await self._maybe_publish_manifest_rest()

    async def stop(self) -> None:
        """Clean shutdown (§5.4: close() frame; the supervisor exits).

        Serialized against start()/restarts via the lifecycle lock so
        a concurrent restart cannot interleave a stop with a fresh
        supervisor's task creation.
        """
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
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
        state["ws_state"] = "stopped"
        state["next_attempt_at"] = None

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
                    # WS-auth failure and enrolment failure are
                    # different failures with different fixes: name
                    # the enrolment state AND the server's verbatim
                    # reason (e.g. key_revoked — key revoked).
                    detail = f"enrolment {status['state']}"
                    if status.get("last_error"):
                        detail += f": {status['last_error']}"
                    if status.get("last_error_message"):
                        detail += f" — {status['last_error_message']}"
                    state["last_error"] = detail
                    state["last_error_detail"] = detail
                    state["last_error_kind"] = "enrolment"
                    state["ws_state"] = "blocked_enrolment"
                    state["next_attempt_at"] = status.get("next_retry_at")
                    logger.error("[CHANNEL] not connecting — {}", detail)
                    return
            try:
                logger.info("[CHANNEL] connecting to {}", url)
                self.ctx.server_channel_state["ws_state"] = "connecting"
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
                    # Explicit proxy route (settings.network_proxy):
                    # same sanctioned route the REST client uses. SOCKS
                    # proxies require the python-socks extra (bundled).
                    proxy=self.ctx.settings.network_proxy or None,
                ) as ws:
                    self._ws = ws
                    self._last_server_frame_at = time.monotonic()
                    state = self.ctx.server_channel_state
                    state["connected"] = True
                    state["ws_state"] = "connected"
                    state["last_connected_at"] = time.time()
                    state["last_error"] = None
                    state["last_error_kind"] = None
                    state["last_error_detail"] = None
                    state["last_error_http_status"] = None
                    state["next_attempt_at"] = None
                    self._ws_auth_failures = 0
                    state["ws_auth_failures"] = 0
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
                info = classify_ws_connect_error(exc)
                state = self.ctx.server_channel_state
                state["connected"] = False
                state["reconnects"] = int(state.get("reconnects", 0)) + 1
                # Never report a bare exception type name ("InvalidStatus")
                # as the error: the dashboard must show the exact failure
                # — HTTP status + the server's verbatim reason.
                state["last_error_kind"] = info["kind"]
                state["last_error_detail"] = info["detail"]
                state["last_error"] = info["detail"]
                state["last_error_http_status"] = info["http_status"]
                if info["kind"] == "ws_auth":
                    self._ws_auth_failures += 1
                    state["ws_state"] = "auth_failed"
                    state["ws_auth_failures"] = self._ws_auth_failures
                    if self._ws_auth_failures >= MAX_WS_AUTH_FAILURES:
                        # The key validates over REST but the WS
                        # handshake keeps 401ing — the device session
                        # is stale. Ask enrolment to re-register once
                        # per threshold crossing instead of hammering
                        # the handshake with the same key; if the key
                        # itself is bad the revalidation lands in
                        # FAILED_AUTH and the channel stays down.
                        self._ws_auth_failures = 0
                        state["ws_auth_failures"] = 0
                        enrolment = self._enrolment
                        if enrolment is not None and hasattr(enrolment, "request_revalidation"):
                            enrolment.request_revalidation()
                else:
                    self._ws_auth_failures = 0
                    state["ws_auth_failures"] = 0
                    state["ws_state"] = "retrying"
                logger.warning("[CHANNEL] disconnected: {} — reconnecting", info["detail"])
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
                if state.get("ws_state") == "connected":
                    # The session ended without an exception (e.g. the
                    # heartbeat loop declared it dead) — a reconnect
                    # follows unless stopping, so say retrying, not
                    # connected.
                    state["ws_state"] = "retrying"
            if self._stop.is_set():
                break
            delay = backoff_with_jitter(
                backoff_delay(
                    attempt,
                    initial_s=self.ctx.settings.reconnect_initial_ms / 1000.0,
                    cap_s=self.ctx.settings.reconnect_max_ms / 1000.0,
                )
            )
            self.ctx.server_channel_state["next_attempt_at"] = (
                datetime.now(UTC) + timedelta(seconds=delay)
            ).isoformat()
            logger.info("[CHANNEL] reconnect in {:.2f}s (attempt {})", delay, attempt)
            await self._wait_backoff(delay)

    # ------------------------------------------------------------------
    async def _session(self, ws: Any) -> bool:
        """One WS session. Returns True when at least one frame was
        received from the server (a successful session, §5.4)."""
        self._last_server_frame_at = time.monotonic()
        self._last_heartbeat_reply_at = time.monotonic()
        self._session_got_frame = False
        # Fresh session: publish the bounded-queue depth + send-gate
        # state so health never shows a stale queue picture.
        self._refresh_queue_state()
        # Re-send any results that never made it out (§6.5: results
        # persist in the bounded pending queue across reconnects).
        # Clear the wire queue first: anything still queued from the
        # previous session is either a chart frame (re-derivable) or a
        # result that is ALSO in the pending queue — re-enqueueing
        # from the pending queue alone avoids duplicates.
        self._outbound.clear()
        for result in self._pending_results.snapshot():
            self._outbound.put_result(result)
        # §4.1: a manifest the server has not seen yet goes out as a
        # manifest.update frame on (re)connect — e.g. after enrolment
        # posted it over REST the hash is already known and this is a
        # no-op; after a registry change mid-session this is the
        # delivery.
        self._maybe_send_manifest_update()
        receiver = asyncio.create_task(self._receive_loop(ws), name="nse-channel-recv")
        heartbeat = asyncio.create_task(self._heartbeat_loop(ws), name="nse-channel-hb")
        sender = asyncio.create_task(self._send_loop(ws), name="nse-channel-send")
        chart_poll = asyncio.create_task(self._chart_poll_loop(), name="nse-channel-chart")
        policy_poll = asyncio.create_task(self._policy_poll_loop(), name="nse-channel-policy")
        got_frame = False
        try:
            done, _pending = await asyncio.wait(
                {receiver, heartbeat, sender, chart_poll, policy_poll},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Read the session flag from the shared attribute, not from
            # a task-stashed value: when the session ends via the
            # sender or heartbeat loop the receiver is still running
            # and its finally-block has not executed — a stashed flag
            # read at this point misreports a frame-exchanging session
            # as failed, so the reconnect backoff never reset.
            got_frame = self._session_got_frame
            for task in done:
                if task is receiver:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        finally:
            for task in (receiver, heartbeat, sender, chart_poll, policy_poll):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — teardown must never raise
                    logger.debug("[CHANNEL] session task teardown failed: {}", type(exc).__name__)
            # In-flight chart.request tasks end with the session too:
            # the fetch was abandoned with the old receive loop, and a
            # late frame would be cleared by the next session's
            # outbound reset anyway — the server re-requests.
            for task in list(self._chart_request_tasks):
                task.cancel()
            if self._chart_request_tasks:
                await asyncio.gather(*self._chart_request_tasks, return_exceptions=True)
            if self._chart is not None:
                # Subscriptions die with the WS session (§17.1.3); the
                # server re-subscribes on reconnect with fresh ids.
                self._chart.drop_all_subscriptions()
        return got_frame

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            self._session_got_frame = True
            self._last_server_frame_at = time.monotonic()
            await self._handle_server_message(raw)

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send the 15 s app heartbeat (§5.3); declare the session dead
        after 45 s without a server HEARTBEAT REPLY.

        Reply-based, not any-frame-based (contract §5.3: the server
        MUST reply to each client heartbeat): command/chart/policy
        frames are server-originated pushes and do not prove the
        heartbeat path is alive. Server-side, the mirror image holds
        since Platform PR #144 — the server's liveness timer counts
        only client-originated frames.
        """
        interval = max(1.0, self.ctx.settings.heartbeat_interval_s)
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() - self._last_heartbeat_reply_at > self.dead_after_s:
                logger.warning(
                    "[CHANNEL] no server heartbeat reply for {:.0f}s — session dead (§5.3), reconnecting",
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
            # fresh sync (account → positions → history). One sync at
            # a time: the check runs on EVERY heartbeat tick, and two
            # ticks during a long sync must not spawn concurrent
            # history fetches.
            login = self._bound_account_id()
            if login is not None and self._last_login is not None and login != self._last_login:
                logger.warning("[CHANNEL] MT5 account switched {} → {} — fresh sync (§7.3)", self._last_login, login)
                if self._sync is not None:
                    self._sync.reset_for_account_switch()
                    if self._account_switch_sync_task is None or self._account_switch_sync_task.done():
                        self._account_switch_sync_task = asyncio.create_task(self._sync.handle_sync_request())
            self._last_login = login
            # §4.1: the registry can only change mid-session if the
            # command set changes under the running bridge — check
            # each heartbeat tick and send a manifest.update frame
            # when it did. Unchanged hash = no-op (tracker).
            self._maybe_send_manifest_update()
            self._refresh_queue_state()
            try:
                await ws.send(json.dumps({"type": "heartbeat", "device_id": self._device_id()}))
                self._note_heartbeat_sent("ws")
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
            self._refresh_queue_state()
            if frame.get("event_type") == "action.result":
                correlation = frame.get("correlation_id")
                if correlation:
                    self._pending_results.remove(
                        lambda r, correlation=correlation: r.get("correlation_id") == correlation
                    )

    async def _chart_poll_loop(self) -> None:
        """Poll due chart subscriptions (§17.1) and enqueue deltas.

        One poll per subscription at a time: the scheduler ticks every
        second, but an MT5 fetch can take seconds (or hit the 30 s
        chart timeout) — without the in-flight guard a slow fetch
        stacked a new task per tick for the same subscription.
        """
        while True:
            await asyncio.sleep(1.0)
            if self._chart is None or self._ws is None:
                continue
            now = time.monotonic()
            for sub in self._chart.poll_due(now):
                if sub.subscription_id in self._chart_poll_inflight:
                    continue
                self._chart_poll_inflight.add(sub.subscription_id)
                task = asyncio.create_task(self._poll_subscription(sub))
                task.add_done_callback(self._chart_poll_done(sub.subscription_id))

    def _chart_poll_done(self, subscription_id: str) -> Callable[[asyncio.Task[Any]], None]:
        """Done-callback factory: release the subscription's in-flight slot."""

        def _release(_task: asyncio.Task[Any]) -> None:
            self._chart_poll_inflight.discard(subscription_id)

        return _release

    async def _policy_poll_loop(self) -> None:
        """W6: evaluate the policy envelope's trailing/breakeven rules
        against the open positions every 2 s.

        The loop is the ONLY caller of the executor; every move is
        authorized at move time (fresh envelope + healthy WS), runs on
        the serialized MT5 worker via threadpool, and is recorded with
        rule_id/decision_id/envelope version. No envelope, no moves.
        """
        while True:
            await asyncio.sleep(2.0)
            if self._policy is None or self._ws is None:
                continue
            if not self._policy.executor.ws_connected():
                continue  # fail closed: no WS, no autonomous moves
            from ..engine.trading import fetch_positions

            try:
                # P1-3: the policy poll's position read runs in the
                # BACKGROUND lane (the move itself runs in the
                # COMMAND lane — see PolicyMoveExecutor._attempt_move).
                with mt5_priority_scope(MT5_PRIORITY_BACKGROUND):
                    positions = await run_in_threadpool(fetch_positions, self.ctx)
            except Exception as exc:  # noqa: BLE001 — a poll failure must not kill the session
                logger.debug("[POLICY] position read for policy evaluation failed: {}", type(exc).__name__)
                continue
            try:
                await self._policy.poll_once(positions)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[POLICY] policy evaluation failed: {}", type(exc).__name__)

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
        if not self._chart.rate_limiter.would_allow():
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
            if ticks and self._chart.rate_limiter.would_allow():
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
            self._last_heartbeat_reply_at = time.monotonic()
            self._note_heartbeat_received("ws")
        elif mtype == "chart.request":
            # Latency hot spot 1 (PERFORMANCE-v14-baseline): the
            # receive loop used to AWAIT the chart fetch inline, so a
            # 300 ms MT5 fetch delayed the NEXT incoming command by
            # ~330 ms (ws_in p50 331/367 ms) before the command's own
            # execution even started. Chart requests run on a task
            # now, like commands — the loop keeps receiving. Frames
            # carry request_id, so concurrent fetches cannot confuse
            # the server; the 30 s timeout + the session-teardown
            # cancel in _session() bound every task.
            task = asyncio.create_task(self._handle_chart_request(msg), name="nse-channel-chart-req")
            self._chart_request_tasks.add(task)
            task.add_done_callback(self._chart_request_tasks.discard)
        elif mtype == "chart.subscribe":
            if self._chart is not None:
                error_frame = self._chart.handle_subscribe(msg)
                if error_frame is not None:
                    self._enqueue_chart(error_frame)
        elif mtype == "chart.unsubscribe":
            if self._chart is not None:
                self._chart.handle_unsubscribe(msg)
        elif mtype == POLICY_FRAME_TYPE:
            # W6 (PROPOSED frame type): a fresh policy envelope. The
            # gate validates and stores it; the trailing executor picks
            # it up on the next poll cycle. An invalid/expired envelope
            # is rejected — fail closed.
            if self._policy is not None:
                self._policy.handle_envelope_frame(msg)
        else:
            # Unknown frames are silently ignored, never fatal (§5.2).
            logger.debug("[CHANNEL] ignored server message type={}", mtype)

    async def _handle_chart_request(self, msg: dict[str, Any]) -> None:
        """Answer one ``chart.request`` (§17.1.1) on a background task.

        Runs off the receive loop (latency hot spot 1) — so it must
        never raise: an unhandled exception in a fire-and-forget task
        would leave the server with no answer at all. Every failure
        path answers with a chart error frame instead.
        """
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
        except Exception as exc:  # noqa: BLE001 — a chart failure must answer, never kill the task silently
            logger.warning("[CHANNEL] chart.request {} failed: {}", msg.get("request_id"), type(exc).__name__)
            frames = [
                build_chart_error_frame(
                    request_id=msg.get("request_id"),
                    subscription_id=None,
                    symbol=str(msg.get("symbol") or ""),
                    timeframe=str(msg.get("timeframe") or ""),
                    code="chart_error",
                    message="chart request failed",
                )
            ]
        for frame in frames:
            self._enqueue_chart(frame)

    async def _handle_command(self, msg: dict[str, Any]) -> None:
        if self._executor is None:
            return
        command_id = str(msg.get("command_id") or "")
        started = datetime.now(UTC)
        try:
            _, result = await run_in_threadpool(self._executor.execute, msg)
        except Exception as exc:  # noqa: BLE001 — a command must never kill the channel
            # Crash OUTSIDE the executor's own handlers: without a
            # fallback the server would hold the command until TTL
            # expiry with no action.result at all. Emit a
            # rejection-shaped result (§6.5) so the outcome is
            # terminal and auditable instead of silent.
            logger.exception("[CHANNEL] command handler crashed: {}", type(exc).__name__)
            if not command_id:
                return
            from ..server.commands import SOURCE_TOOL, build_action_result

            raw_operation = msg.get("operation")
            operation: dict[str, Any] = raw_operation if isinstance(raw_operation, dict) else {}
            op_type = str(operation.get("type") or "unknown")
            result = build_action_result(
                device_id=self._device_id(),
                command_id=command_id,
                account_id=str(msg.get("account_id") or ""),
                status="rejected",
                source_tool=SOURCE_TOOL.get(op_type, op_type),
                started_at=started,
                completed_at=datetime.now(UTC),
                error={"code": "HANDLER_CRASHED", "message": f"command handler crashed: {type(exc).__name__}"},
                user_id=self._rest.user_id if self._rest is not None else None,
            )
        self._enqueue_result(result)

    # ------------------------------------------------------------------
    def build_manifest_payload(self) -> dict[str, Any]:
        """Current §4.1 manifest payload (sent over REST by the caller)."""
        return build_manifest(self._device_id(), driver=self.ctx.driver)
