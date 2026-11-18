"""Structured telemetry pipeline (ARCHITECTURE-CONTRACT §8, PLAN addendum §2).

A telemetry event is one structured JSON object with exactly these
fields (no more, no less — the future Platform endpoint will be
specified against this shape)::

    timestamp, severity, event_type, device_id, correlation_id,
    command_id, component, error_code, sanitized_details

Level rules (binding):

- ERROR — report immediately when possible (queued AND an immediate
  delivery attempt is made when a sink is attached).
- WARN — important operational problems; queued for delivery.
- INFO — only meaningful lifecycle events (connect, enrolment,
  command terminal state, ...). INFO events are logged locally and
  are NEVER queued for delivery: the server must not be flooded with
  success logs.
- DEBUG — local diagnostic mode only, disabled by default
  (``BridgeSettings.diagnostic_mode``, env
  ``NSE_BRIDGE_DIAGNOSTIC_MODE``). Dropped silently when off.

Secrets: every event's details pass through the Phase 1 redaction
(:func:`nse_bridge.logging_setup.sanitize_payload` /
:func:`nse_bridge.logging_setup.redact_text`) BEFORE the event is
built, queued, or delivered. A secret value can never appear in an
emitted event (proven by ``tests/unit/test_telemetry.py``).

DELIVERY GAP (CONTRACT v1 §4.2, docs/PLATFORM-GAPS.md §2): the
contract defines NO diagnostics/events upload endpoint — the server
derives health from ``last_seen_at``, WS liveness, the last accepted
batch, and the manifest. Telemetry therefore travels via the
existing truthful channels only (``action.result`` errors,
heartbeat/liveness, manifest updates, sync/batch acks), and the
local pipeline keeps ERROR/WARN events in a bounded queue with
retry/backoff/dedup/limits/expiry for the day an endpoint exists.

The adapter for that future endpoint is :class:`DiagnosticsSink`:
a protocol with a single method, ``send(event) -> bool``. The
default sink is :class:`NullDiagnosticsSink` — INERT by design: it
sends nothing, to no URL, because no endpoint exists yet. It must
stay inert until a future contract version defines the endpoint
(path, auth, payload, rate limits); inventing a production URL here
would violate the contract. A real sink is attached only when that
contract amendment lands.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

from ..logging_setup import sanitize_payload

__all__ = [
    "DEBUG",
    "ERROR",
    "INFO",
    "WARN",
    "DiagnosticQueue",
    "DiagnosticsSink",
    "NullDiagnosticsSink",
    "TelemetryEmitter",
    "TelemetryEvent",
]

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"
DEBUG = "DEBUG"

#: Queue defaults (ARCHITECTURE-CONTRACT §8: bounded, retry,
#: backoff, dedup, limits, expiration, safe cleanup after delivery).
TELEMETRY_QUEUE_MAX_ITEMS = 128
TELEMETRY_QUEUE_TTL_S = 3600.0  # an undelivered event ages out after 1 h
TELEMETRY_MAX_ATTEMPTS = 5
TELEMETRY_BACKOFF_BASE_S = 1.0
TELEMETRY_BACKOFF_CAP_S = 60.0


@dataclass(frozen=True)
class TelemetryEvent:
    """One structured JSON telemetry event — exactly the nine
    contract fields (ARCHITECTURE-CONTRACT §8)."""

    timestamp: str
    severity: str  # ERROR | WARN | INFO | DEBUG
    event_type: str
    device_id: str | None
    correlation_id: str | None
    command_id: str | None
    component: str
    error_code: str | None
    sanitized_details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity,
            "event_type": self.event_type,
            "device_id": self.device_id,
            "correlation_id": self.correlation_id,
            "command_id": self.command_id,
            "component": self.component,
            "error_code": self.error_code,
            "sanitized_details": self.sanitized_details,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


class DiagnosticsSink(Protocol):
    """Adapter interface for a future diagnostics/events upload
    endpoint (CONTRACT v1 §4.2 gap — see the module docstring).

    A single method, on purpose: the future endpoint's transport
    (REST POST, WS frame, ...) is the Platform's to define, and the
    adapter must not assume it. ``send`` returns True only when the
    event was accepted by the sink; False means "keep it queued"
    (retry/backoff/expiry apply). The adapter MUST NOT invent a
    production URL or payload shape.
    """

    def send(self, event: TelemetryEvent) -> bool: ...


class NullDiagnosticsSink:
    """INERT sink — the default until a contract version defines the
    diagnostics endpoint (docs/PLATFORM-GAPS.md §2).

    ``send`` always returns False and transmits nothing: there is no
    URL to send to, and guessing one would violate CONTRACT v1 §4.2.
    Events queued against this sink age out by TTL (see
    :class:`DiagnosticQueue`); nothing is lost silently — expiry is
    counted and exposed in the diagnostics summary.
    """

    def send(self, event: TelemetryEvent) -> bool:
        return False


@dataclass
class _QueuedEvent:
    event: TelemetryEvent
    dedup_key: str
    attempts: int = 0
    next_attempt_at: float = 0.0


class DiagnosticQueue:
    """Bounded local diagnostic queue: retry, backoff, dedup, limits,
    expiration, safe cleanup after delivery.

    - Bound: ``max_items``; when full the OLDEST entry is evicted
      WITH a warning log (never silently discarded).
    - Dedup: events sharing a dedup key (event_type + error_code +
      correlation/command id) collapse into one queued entry — a
      flapping fault does not fill the queue with copies.
    - Expiry: entries older than ``ttl_s`` are purged (counted).
    - Retry: failed sends back off (base doubling, capped) up to
      ``max_attempts``; then the entry is dropped (counted).
    - Delivery: :meth:`drain` attempts due entries against a sink and
      removes only entries the sink accepted.
    """

    def __init__(
        self,
        *,
        max_items: int = TELEMETRY_QUEUE_MAX_ITEMS,
        ttl_s: float = TELEMETRY_QUEUE_TTL_S,
        max_attempts: int = TELEMETRY_MAX_ATTEMPTS,
        backoff_base_s: float = TELEMETRY_BACKOFF_BASE_S,
        backoff_cap_s: float = TELEMETRY_BACKOFF_CAP_S,
        clock: Any | None = None,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        self.max_items = max_items
        self.ttl_s = ttl_s
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._clock = clock or time.monotonic
        self._items: deque[_QueuedEvent] = deque()
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self.evicted = 0
        self.expired = 0
        self.deduped = 0
        self.delivered = 0
        self.failed = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @staticmethod
    def dedup_key(event: TelemetryEvent) -> str:
        return "|".join(
            [
                event.event_type,
                event.error_code or "",
                event.correlation_id or event.command_id or "",
            ]
        )

    def _purge_expired_locked(self) -> None:
        kept: deque[_QueuedEvent] = deque()
        for entry in self._items:
            # Age is wall-clock time since the event was emitted;
            # the retry clock (monotonic) is a different domain.
            try:
                age = time.time() - datetime.fromisoformat(entry.event.timestamp).timestamp()
            except ValueError:
                age = 0.0
            if age > self.ttl_s:
                self._keys.discard(entry.dedup_key)
                self.expired += 1
                logger.warning(
                    "[TELEMETRY] diagnostic event expired undelivered: event_type={} error_code={}",
                    entry.event.event_type,
                    entry.event.error_code,
                )
            else:
                kept.append(entry)
        self._items = kept

    def put(self, event: TelemetryEvent) -> bool:
        """Queue ``event`` for delivery. Returns False when the event
        was deduplicated against an entry already queued."""
        now = self._clock()
        key = self.dedup_key(event)
        with self._lock:
            self._purge_expired_locked()
            if key in self._keys:
                self.deduped += 1
                logger.debug(
                    "[TELEMETRY] deduped diagnostic event: event_type={} error_code={}",
                    event.event_type,
                    event.error_code,
                )
                return False
            if len(self._items) >= self.max_items:
                evicted = self._items.popleft()
                self._keys.discard(evicted.dedup_key)
                self.evicted += 1
                logger.warning(
                    "[TELEMETRY] diagnostic queue full (max {}): evicted oldest "
                    "event_type={} error_code={} — it was never delivered",
                    self.max_items,
                    evicted.event.event_type,
                    evicted.event.error_code,
                )
            self._items.append(_QueuedEvent(event=event, dedup_key=key, next_attempt_at=now))
            self._keys.add(key)
            return True

    def drain(self, sink: DiagnosticsSink) -> int:
        """Attempt delivery of every due entry via ``sink``.

        Returns how many events were accepted. Failed sends back off
        and stay queued until ``max_attempts``; expired entries are
        purged (counted). A sink that returns False for everything
        (e.g. :class:`NullDiagnosticsSink`) delivers nothing and the
        queue drains by TTL — by design, until an endpoint exists.
        """
        now = self._clock()
        delivered = 0
        with self._lock:
            self._purge_expired_locked()
            remaining: deque[_QueuedEvent] = deque()
            for entry in self._items:
                if entry.next_attempt_at > now:
                    remaining.append(entry)
                    continue
                try:
                    accepted = sink.send(entry.event)
                except Exception as exc:  # noqa: BLE001 — a sink failure never kills the drain
                    logger.warning("[TELEMETRY] diagnostics sink failed: {}", type(exc).__name__)
                    accepted = False
                if accepted:
                    self._keys.discard(entry.dedup_key)
                    self.delivered += 1
                    delivered += 1
                    continue
                entry.attempts += 1
                if entry.attempts >= self.max_attempts:
                    self._keys.discard(entry.dedup_key)
                    self.failed += 1
                    logger.warning(
                        "[TELEMETRY] diagnostic event abandoned after {} attempts: event_type={} error_code={}",
                        entry.attempts,
                        entry.event.event_type,
                        entry.event.error_code,
                    )
                    continue
                backoff = min(self.backoff_base_s * (2 ** (entry.attempts - 1)), self.backoff_cap_s)
                entry.next_attempt_at = now + backoff
                remaining.append(entry)
            self._items = remaining
        return delivered

    def snapshot(self) -> list[TelemetryEvent]:
        with self._lock:
            return [entry.event for entry in self._items]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "queued": len(self._items),
                "max_items": self.max_items,
                "ttl_s": self.ttl_s,
                "delivered": self.delivered,
                "evicted": self.evicted,
                "expired": self.expired,
                "deduped": self.deduped,
                "failed": self.failed,
            }


class TelemetryEmitter:
    """Builds, redacts, logs, and queues structured telemetry events.

    INFO events are local-only (never queued); WARN/ERROR events go
    into the bounded :class:`DiagnosticQueue`; ERROR additionally
    triggers an immediate delivery attempt when a sink is attached.
    DEBUG events exist only when ``diagnostic_mode`` is on.
    """

    def __init__(
        self,
        device_id: str | None,
        *,
        diagnostic_mode: bool = False,
        queue: DiagnosticQueue | None = None,
        sink: DiagnosticsSink | None = None,
    ) -> None:
        self.device_id = device_id
        self.diagnostic_mode = diagnostic_mode
        self.queue = queue or DiagnosticQueue()
        # None = no sink attached yet (inert by default — see
        # NullDiagnosticsSink). Delivery only happens via an
        # explicitly attached sink; nothing is ever sent to a guessed
        # URL.
        self.sink = sink
        self.emitted = 0
        self.info_local_only = 0
        self.debug_dropped = 0

    # ------------------------------------------------------------------
    def emit(
        self,
        severity: str,
        event_type: str,
        *,
        component: str,
        error_code: str | None = None,
        correlation_id: str | None = None,
        command_id: str | None = None,
        details: dict[str, Any] | None = None,
        device_id: str | None = None,
    ) -> TelemetryEvent | None:
        """Build + redact + route one event.

        Returns the event, or None when a DEBUG event was dropped
        because diagnostic mode is off. ``details`` is sanitized
        (Phase 1 redaction) before it is stored anywhere.
        """
        if severity == DEBUG and not self.diagnostic_mode:
            self.debug_dropped += 1
            return None
        event = TelemetryEvent(
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            severity=severity,
            event_type=event_type,
            device_id=device_id if device_id is not None else self.device_id,
            correlation_id=correlation_id,
            command_id=command_id,
            component=component,
            error_code=error_code,
            sanitized_details=sanitize_payload(details or {}),
        )
        self.emitted += 1
        # Local structured log always (redaction filter applies again
        # on the sink side; double-redaction is harmless).
        log_message = f"[TELEMETRY] {event_type} severity={severity} component={component} error_code={error_code}"
        if severity == ERROR:
            logger.error(log_message)
        elif severity == WARN:
            logger.warning(log_message)
        elif severity == DEBUG:
            logger.debug(log_message)
        else:
            logger.info(log_message)
        if severity == INFO:
            # Meaningful lifecycle events only, local log — NEVER
            # queued for delivery (no success-log flooding).
            self.info_local_only += 1
            return event
        self.queue.put(event)
        if severity == ERROR and self.sink is not None:
            # ERROR reports immediately when possible.
            self.queue.drain(self.sink)
        return event

    def error(self, event_type: str, **kwargs: Any) -> TelemetryEvent | None:
        return self.emit(ERROR, event_type, **kwargs)

    def warn(self, event_type: str, **kwargs: Any) -> TelemetryEvent | None:
        return self.emit(WARN, event_type, **kwargs)

    def info(self, event_type: str, **kwargs: Any) -> TelemetryEvent | None:
        return self.emit(INFO, event_type, **kwargs)

    def debug(self, event_type: str, **kwargs: Any) -> TelemetryEvent | None:
        return self.emit(DEBUG, event_type, **kwargs)

    def drain(self) -> int:
        """Attempt delivery of queued events via the attached sink.

        Returns 0 when no sink is attached (inert default — nothing
        is sent anywhere until a contract-defined endpoint exists).
        """
        if self.sink is None:
            return 0
        return self.queue.drain(self.sink)

    def summary(self) -> dict[str, Any]:
        """Diagnostics/GUI payload: queue state + level counters."""
        return {
            "device_id": self.device_id,
            "diagnostic_mode": self.diagnostic_mode,
            "sink_attached": self.sink is not None and not isinstance(self.sink, NullDiagnosticsSink),
            "sink_inert": self.sink is None or isinstance(self.sink, NullDiagnosticsSink),
            "emitted": self.emitted,
            "info_local_only": self.info_local_only,
            "debug_dropped": self.debug_dropped,
            "queue": self.queue.stats(),
        }


# Module-level no-op emitter for components constructed without one
# (keeps optional wiring cheap; emits are local-log only, nothing
# queued, nothing sent). It is deliberately NOT shared state: each
# component that cares owns its emitter via the BridgeContext.
_NOOP_EMITTER = TelemetryEmitter(device_id=None)


def noop_emitter() -> TelemetryEmitter:
    return _NOOP_EMITTER
