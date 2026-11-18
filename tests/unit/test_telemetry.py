"""Unit tests for the structured telemetry pipeline (§8).

Label: unit. Covers the event shape (exactly the nine contract
fields), the redaction proof (a secret value never appears in an
emitted event), the level rules (INFO never queued, ERROR reported
immediately, DEBUG off by default), queue bounds / expiry / dedup /
retry-backoff, and the inert diagnostics adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nse_bridge.logging_setup import clear_registered_secrets, register_secret
from nse_bridge.reliability.telemetry import (
    DEBUG,
    ERROR,
    WARN,
    DiagnosticQueue,
    NullDiagnosticsSink,
    TelemetryEmitter,
    TelemetryEvent,
)

pytestmark = pytest.mark.unit

# The nine contract fields, in order (ARCHITECTURE-CONTRACT §8).
EVENT_FIELDS = [
    "timestamp",
    "severity",
    "event_type",
    "device_id",
    "correlation_id",
    "command_id",
    "component",
    "error_code",
    "sanitized_details",
]


class RecordingSink:
    """A DiagnosticsSink that captures what it was offered."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.received: list[TelemetryEvent] = []
        self.attempts = 0

    def send(self, event: TelemetryEvent) -> bool:
        self.attempts += 1
        if self.accept:
            self.received.append(event)
        return self.accept


def _event(**overrides) -> TelemetryEvent:
    fields = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "severity": WARN,
        "event_type": "test.event",
        "device_id": "dev-1",
        "correlation_id": "corr-1",
        "command_id": None,
        "component": "test",
        "error_code": None,
        "sanitized_details": {},
    }
    fields.update(overrides)
    return TelemetryEvent(**fields)


# ---------------------------------------------------------------------------
# Event shape
# ---------------------------------------------------------------------------
def test_event_has_exactly_the_nine_contract_fields():
    event = _event()
    assert list(event.to_dict().keys()) == EVENT_FIELDS
    assert len(event.to_dict()) == 9
    parsed = event.to_json()
    assert '"event_type": "test.event"' in parsed


def test_event_timestamp_is_utc_iso():
    event = _event()
    parsed = datetime.fromisoformat(event.timestamp)
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# Redaction proof
# ---------------------------------------------------------------------------
def test_secret_value_never_appears_in_an_emitted_event():
    secret = "sk-live-9f8e7d6c5b4a-very-secret"
    token = "Bearer abcdef1234567890"
    register_secret(secret)
    try:
        emitter = TelemetryEmitter("dev-1")
        event = emitter.emit(
            ERROR,
            "test.secret_leak",
            component="test",
            error_code="boom",
            details={
                "message": f"call failed with token {token} and key {secret}",
                "api_key": "api-key-value-12345678",
                "password": "hunter2-hunter2",
                "nested": {"server_token": secret},
            },
        )
        assert event is not None
        blob = event.to_json()
        assert secret not in blob
        assert "abcdef1234567890" not in blob
        assert "api-key-value-12345678" not in blob
        assert "hunter2-hunter2" not in blob
        assert "[REDACTED]" in blob
        # The queued copy is redacted too.
        assert len(emitter.queue) == 1
        assert secret not in emitter.queue.snapshot()[0].to_json()
    finally:
        clear_registered_secrets()


def test_labelled_secret_patterns_redacted_without_a_registered_value():
    emitter = TelemetryEmitter("dev-1")
    event = emitter.emit(
        WARN,
        "test.patterns",
        component="test",
        details={"note": "Authorization: Bearer supersecretvalue123"},
    )
    assert event is not None
    assert "supersecretvalue123" not in event.to_json()


# ---------------------------------------------------------------------------
# Level rules
# ---------------------------------------------------------------------------
def test_info_events_are_local_only_never_queued():
    emitter = TelemetryEmitter("dev-1")
    for _ in range(5):
        emitter.info("lifecycle.connected", component="test")
    assert emitter.emitted == 5
    assert emitter.info_local_only == 5
    assert len(emitter.queue) == 0


def test_warn_events_are_queued_not_sent_immediately():
    sink = RecordingSink()
    emitter = TelemetryEmitter("dev-1", sink=sink)
    emitter.warn("test.warn", component="test", error_code="w1")
    assert len(emitter.queue) == 1
    assert sink.attempts == 0  # WARN waits for a drain
    assert emitter.drain() == 1
    assert sink.attempts == 1
    assert len(emitter.queue) == 0


def test_error_events_attempt_immediate_delivery():
    sink = RecordingSink()
    emitter = TelemetryEmitter("dev-1", sink=sink)
    emitter.error("test.error", component="test", error_code="e1")
    assert sink.attempts == 1
    assert len(sink.received) == 1
    assert len(emitter.queue) == 0  # accepted immediately


def test_error_without_a_sink_stays_queued():
    emitter = TelemetryEmitter("dev-1")
    emitter.error("test.error", component="test", error_code="e1")
    assert len(emitter.queue) == 1
    assert emitter.drain() == 0  # no sink attached: nothing is sent anywhere


def test_debug_dropped_by_default_emitted_in_diagnostic_mode():
    off = TelemetryEmitter("dev-1", diagnostic_mode=False)
    assert off.debug("test.debug", component="test") is None
    assert off.debug_dropped == 1
    assert off.emitted == 0

    on = TelemetryEmitter("dev-1", diagnostic_mode=True)
    event = on.debug("test.debug", component="test")
    assert event is not None
    assert event.severity == DEBUG
    assert on.debug_dropped == 0


# ---------------------------------------------------------------------------
# Queue: bounds / dedup / expiry / retry
# ---------------------------------------------------------------------------
def test_queue_bound_evicts_oldest_with_a_warning():
    queue = DiagnosticQueue(max_items=3)
    for i in range(4):
        assert queue.put(_event(event_type=f"test.{i}", correlation_id=f"c{i}")) is True
    assert len(queue) == 3
    assert queue.evicted == 1
    assert [e.event_type for e in queue.snapshot()] == ["test.1", "test.2", "test.3"]


def test_queue_dedups_identical_events():
    queue = DiagnosticQueue(max_items=10)
    assert queue.put(_event()) is True
    assert queue.put(_event()) is False  # same event_type + error + correlation
    assert queue.put(_event(correlation_id="corr-2")) is True  # different id: distinct
    assert len(queue) == 2
    assert queue.deduped == 1


def test_queue_expires_stale_events_on_drain():
    queue = DiagnosticQueue(max_items=10, ttl_s=3600.0)
    stale = _event(timestamp=(datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"))
    queue.put(stale)
    queue.put(_event(event_type="test.fresh", correlation_id="fresh"))
    sink = RecordingSink()
    assert queue.drain(sink) == 1  # only the fresh event is delivered
    assert queue.expired == 1
    assert len(queue) == 0
    assert sink.received[0].event_type == "test.fresh"


def test_queue_retries_with_backoff_then_abandons():
    now = [1000.0]
    queue = DiagnosticQueue(
        max_items=10,
        max_attempts=3,
        backoff_base_s=10.0,
        backoff_cap_s=60.0,
        clock=lambda: now[0],
    )
    queue.put(_event())
    sink = RecordingSink(accept=False)
    assert queue.drain(sink) == 0
    assert len(queue) == 1  # attempt 1: stays queued, backs off
    assert queue.drain(sink) == 0  # not due yet (backoff 10 s)
    assert sink.attempts == 1
    now[0] += 11.0
    assert queue.drain(sink) == 0  # attempt 2
    now[0] += 21.0
    assert queue.drain(sink) == 0  # attempt 3 → abandoned
    assert queue.failed == 1
    assert len(queue) == 0


def test_queue_drain_survives_a_crashing_sink():
    queue = DiagnosticQueue(max_items=10, max_attempts=1)

    class CrashingSink:
        def send(self, event: TelemetryEvent) -> bool:
            raise RuntimeError("sink exploded")

    queue.put(_event())
    assert queue.drain(CrashingSink()) == 0
    assert queue.failed == 1
    assert len(queue) == 0


def test_queue_records_expose_dedup_key_and_attempts_for_persistence():
    queue = DiagnosticQueue(max_items=10)
    queue.put(_event(event_type="test.a", correlation_id="c1"))
    records = queue.records()
    assert len(records) == 1
    assert records[0]["dedup_key"] == DiagnosticQueue.dedup_key(_event(event_type="test.a", correlation_id="c1"))
    assert records[0]["attempts"] == 0
    assert records[0]["event"]["event_type"] == "test.a"
    assert list(records[0]["event"]) == EVENT_FIELDS


# ---------------------------------------------------------------------------
# The inert adapter
# ---------------------------------------------------------------------------
def test_null_sink_is_inert_and_never_delivers():
    sink = NullDiagnosticsSink()
    assert sink.send(_event()) is False
    emitter = TelemetryEmitter("dev-1", sink=sink)
    emitter.error("test.error", component="test", error_code="e1")
    # ERROR attempted immediate delivery against the inert sink: the
    # event stays queued and ages out by TTL — nothing was sent.
    assert len(emitter.queue) == 1
    assert emitter.summary()["sink_inert"] is True
    assert emitter.summary()["sink_attached"] is False


def test_emitter_summary_shape():
    emitter = TelemetryEmitter("dev-1", diagnostic_mode=True)
    emitter.info("lifecycle.x", component="test")
    summary = emitter.summary()
    assert summary["device_id"] == "dev-1"
    assert summary["diagnostic_mode"] is True
    assert summary["sink_inert"] is True
    assert summary["emitted"] == 1
    assert summary["info_local_only"] == 1
    assert summary["queue"]["queued"] == 0
    assert summary["queue"]["max_items"] == 128


# ---------------------------------------------------------------------------
# Wiring: the command executor reports terminal states as telemetry
# ---------------------------------------------------------------------------
def test_executor_reports_rejection_as_a_queued_warn_event():
    from datetime import timedelta

    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from nse_bridge.server.commands import CommandExecutor, CommandValidator
    from nse_bridge.server.rest import Entitlements
    from nse_bridge.server.uuid7 import new_uuid7_str

    now = datetime.now(UTC)
    settings = BridgeSettings(_env_file=None, server_token="<redacted>", device_id="dev-1")
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    emitter = TelemetryEmitter("dev-1")
    ctx.telemetry = emitter
    validator = CommandValidator(
        "dev-1",
        get_account_id=lambda: "12345678901",
        entitlements=Entitlements(position_manager=True, auto_trade=True),
    )
    executor = CommandExecutor(ctx, validator)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": "999",  # wrong account → §6.3 step 4 rejection
        "device_id": "dev-1",
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    _execution, result = executor.execute(frame)
    assert result["status"] == "rejected"
    assert len(emitter.queue) == 1
    event = emitter.queue.snapshot()[0]
    assert event.event_type == "command.failed"
    assert event.error_code == "WrongAccount"
    assert event.command_id == frame["command_id"]


def test_executor_reports_expiry_as_local_only_info():
    from datetime import timedelta

    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from nse_bridge.server.commands import CommandExecutor, CommandValidator
    from nse_bridge.server.rest import Entitlements
    from nse_bridge.server.uuid7 import new_uuid7_str

    now = datetime.now(UTC)
    settings = BridgeSettings(_env_file=None, server_token="<redacted>", device_id="dev-1")
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    emitter = TelemetryEmitter("dev-1")
    ctx.telemetry = emitter
    validator = CommandValidator(
        "dev-1",
        get_account_id=lambda: "12345678901",
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: now,
    )
    executor = CommandExecutor(ctx, validator)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": "12345678901",
        "device_id": "dev-1",
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    _execution, result = executor.execute(frame)
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "Expired"
    # INFO lifecycle event: logged locally, never queued for delivery.
    assert len(emitter.queue) == 0
    assert emitter.info_local_only == 1
