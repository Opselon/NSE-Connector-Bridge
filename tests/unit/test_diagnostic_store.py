"""Unit tests for the durable diagnostic store + queue (spec §12, §16).

Label: unit. A Bridge restart must not silently lose queued ERROR/WARN
diagnostic events: they are mirrored to a bounded JSONL file and
re-queued on startup, with the existing retry/backoff/dedup/TTL/limits
semantics preserved. The queue still drains by TTL until a diagnostics
endpoint exists (D1) — persistence preserves the events, it does not
invent a delivery channel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nse_bridge.reliability.diagnostic_store import (
    DurableDiagnosticQueue,
    DurableDiagnosticStore,
)
from nse_bridge.reliability.telemetry import TelemetryEvent

pytestmark = pytest.mark.unit


def _event(event_type: str, **overrides) -> TelemetryEvent:
    fields = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "severity": "WARN",
        "event_type": event_type,
        "device_id": "dev-1",
        "correlation_id": None,
        "command_id": None,
        "component": "test",
        "error_code": None,
        "sanitized_details": {},
    }
    fields.update(overrides)
    return TelemetryEvent(**fields)


def _queue(tmp_path, **kwargs):
    store = DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl")
    return DurableDiagnosticQueue(store, **kwargs)


class RecordingSink:
    """A DiagnosticsSink that captures what it was offered."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.received: list[TelemetryEvent] = []

    def send(self, event: TelemetryEvent) -> bool:
        if self.accept:
            self.received.append(event)
        return self.accept


# ---------------------------------------------------------------------------
# DurableDiagnosticStore
# ---------------------------------------------------------------------------
def test_store_file_created_lazily_on_first_append(tmp_path):
    path = tmp_path / "pending_diagnostics.jsonl"
    store = DurableDiagnosticStore(path)
    assert not path.exists()
    assert store.load() == []
    record = {"dedup_key": "k1", "attempts": 0, "event": _event("test.e1").to_dict()}
    assert store.append(record) is True
    assert path.exists()
    assert [r["dedup_key"] for r in store.load()] == ["k1"]


def test_store_round_trip_and_remove(tmp_path):
    store = DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl")
    for key in ("k1", "k2"):
        store.append({"dedup_key": key, "attempts": 0, "event": _event(f"test.{key}").to_dict()})
    assert store.remove("k1") == 1
    assert [r["dedup_key"] for r in store.load()] == ["k2"]
    assert store.remove("missing") == 0
    store.clear()
    assert store.load() == []


def test_store_bound_evicts_oldest(tmp_path):
    store = DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl", max_items=2)
    for key in ("k1", "k2", "k3"):
        store.append({"dedup_key": key, "attempts": 0, "event": _event(f"test.{key}").to_dict()})
    assert [r["dedup_key"] for r in store.load()] == ["k2", "k3"]


def test_store_rejects_invalid_records(tmp_path):
    store = DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl")
    assert store.append({"dedup_key": "k1"}) is False  # no event
    assert store.append({"dedup_key": "", "attempts": 0, "event": _event("t").to_dict()}) is False
    assert store.append({"dedup_key": "k1", "attempts": -1, "event": _event("t").to_dict()}) is False
    assert store.load() == []
    assert not (tmp_path / "pending_diagnostics.jsonl").exists()


# ---------------------------------------------------------------------------
# DurableDiagnosticQueue — survives restart
# ---------------------------------------------------------------------------
def test_enqueue_restart_events_are_present(tmp_path):
    queue = _queue(tmp_path)
    queue.put(_event("test.e1", correlation_id="c1"))
    queue.put(_event("test.e2", correlation_id="c2", error_code="boom"))
    assert len(queue) == 2

    # Simulate a process restart: a new queue over the same directory.
    reloaded = _queue(tmp_path)
    assert len(reloaded) == 2
    assert [e.event_type for e in reloaded.snapshot()] == ["test.e1", "test.e2"]
    assert reloaded.snapshot()[1].error_code == "boom"


def test_queue_file_not_created_until_first_event(tmp_path):
    path = tmp_path / "pending_diagnostics.jsonl"
    queue = _queue(tmp_path)
    assert not path.exists()
    assert len(queue) == 0
    queue.put(_event("test.e1"))
    assert path.exists()


def test_bound_enforced_on_reload(tmp_path):
    queue = _queue(tmp_path, max_items=3)
    for i in range(5):
        queue.put(_event(f"test.{i}", correlation_id=f"c{i}"))
    assert len(queue) == 3
    assert queue.evicted == 2

    reloaded = _queue(tmp_path, max_items=3)
    assert len(reloaded) == 3
    assert [e.event_type for e in reloaded.snapshot()] == ["test.2", "test.3", "test.4"]
    # The store was trimmed to the same bound on reload.
    assert len(DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl").load()) == 3


def test_attempts_preserved_across_restart(tmp_path):
    now = [1000.0]
    queue = _queue(tmp_path, max_attempts=5, backoff_base_s=10.0, clock=lambda: now[0])
    queue.put(_event("test.e1"))
    assert queue.drain(RecordingSink(accept=False)) == 0
    assert len(queue) == 1

    reloaded = _queue(tmp_path, max_attempts=5, backoff_base_s=10.0, clock=lambda: now[0])
    assert reloaded.records()[0]["attempts"] == 1
    # The reloaded entry is due immediately (deadline reset), so the
    # next drain retries it instead of waiting out the old backoff.
    assert reloaded.drain(RecordingSink(accept=False)) == 0
    assert reloaded.records()[0]["attempts"] == 2


def test_stale_events_purge_by_ttl_on_reload(tmp_path):
    queue = _queue(tmp_path, ttl_s=3600.0)
    stale = _event(
        "test.stale",
        timestamp=(datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    )
    queue.put(stale)
    queue.put(_event("test.fresh", correlation_id="fresh"))
    # The stale entry is purged (counted) by the second put already —
    # expiry is enforced on every mutation, not only on reload.
    assert len(queue) == 1
    assert queue.expired == 1

    reloaded = _queue(tmp_path, ttl_s=3600.0)
    assert [e.event_type for e in reloaded.snapshot()] == ["test.fresh"]
    # The store was trimmed to the live queue on reload.
    assert len(DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl").load()) == 1


def test_stale_only_queue_purges_on_reload(tmp_path):
    queue = _queue(tmp_path, ttl_s=3600.0)
    stale = _event(
        "test.stale",
        timestamp=(datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    )
    queue.put(stale)
    assert len(queue) == 1  # nothing older to purge against on the first put

    reloaded = _queue(tmp_path, ttl_s=3600.0)
    assert reloaded.expired == 1
    assert len(reloaded) == 0
    assert DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl").load() == []


def test_delivered_events_leave_the_store(tmp_path):
    queue = _queue(tmp_path)
    queue.put(_event("test.e1", correlation_id="c1"))
    queue.put(_event("test.e2", correlation_id="c2"))
    sink = RecordingSink()
    assert queue.drain(sink) == 2
    assert len(queue) == 0

    reloaded = _queue(tmp_path)
    assert len(reloaded) == 0
    assert DurableDiagnosticStore(tmp_path / "pending_diagnostics.jsonl").load() == []


def test_abandoned_events_leave_the_store(tmp_path):
    queue = _queue(tmp_path, max_attempts=1)
    queue.put(_event("test.e1"))
    assert queue.drain(RecordingSink(accept=False)) == 0
    assert queue.failed == 1
    assert len(queue) == 0

    assert _queue(tmp_path, max_attempts=1).snapshot() == []


def test_dedup_survives_restart(tmp_path):
    queue = _queue(tmp_path)
    assert queue.put(_event("test.e1", correlation_id="c1")) is True
    assert queue.put(_event("test.e1", correlation_id="c1")) is False

    reloaded = _queue(tmp_path)
    assert reloaded.put(_event("test.e1", correlation_id="c1")) is False
    assert len(reloaded) == 1
    assert reloaded.deduped == 1


def test_corrupt_file_handled_log_and_start_empty(tmp_path):
    path = tmp_path / "pending_diagnostics.jsonl"
    good = {"dedup_key": "k-good", "attempts": 0, "event": _event("test.good").to_dict()}
    import json

    path.write_text(
        "this is not json\n"
        + json.dumps({"dedup_key": "k-bad"})  # malformed record: no event
        + "\n"
        + json.dumps(good)
        + "\n"
        + "\n",  # blank lines are skipped
        encoding="utf-8",
    )
    store = DurableDiagnosticStore(path)
    loaded = store.load()
    assert [r["dedup_key"] for r in loaded] == ["k-good"]

    queue = DurableDiagnosticQueue(store)
    assert len(queue) == 1
    assert queue.snapshot()[0].event_type == "test.good"


def test_unreadable_shape_event_skipped_on_reload(tmp_path):
    path = tmp_path / "pending_diagnostics.jsonl"
    import json

    # A record that passes _valid_record but whose event dict has an
    # unexpected extra field: TelemetryEvent(**...) must not crash
    # the reload.
    event = _event("test.extra").to_dict()
    event["unexpected_field"] = "boom"
    record = {"dedup_key": "k-extra", "attempts": 0, "event": event}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    queue = DurableDiagnosticQueue(DurableDiagnosticStore(path))
    assert len(queue) == 0  # skipped, never a crash


def test_missing_file_loads_empty(tmp_path):
    queue = _queue(tmp_path)
    assert len(queue) == 0
    assert queue.snapshot() == []
