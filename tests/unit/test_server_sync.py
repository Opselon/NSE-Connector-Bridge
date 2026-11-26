"""Unit tests for the sync engine (CONTRACT v1 §3.3) + manifest (§4.1).

Label: unit. No MT5, no network — the REST client and the snapshot
providers are fakes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from loguru import logger

from nse_bridge.server.manifest import build_manifest, manifest_hash
from nse_bridge.server.queue import BoundedPendingQueue
from nse_bridge.server.rest import SyncAck
from nse_bridge.server.sync import (
    SyncCheckpoint,
    SyncEngine,
    build_batch,
    chunk_items,
    count_items,
    parse_broker_time,
)

pytestmark = pytest.mark.unit


class FakeRest:
    """Records batches; optionally fails the next send."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_next = False

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        if self.fail_next:
            self.fail_next = False
            from nse_bridge.server.errors import ServerError, ServerErrorCode

            raise ServerError(ServerErrorCode.SERVER_UNAVAILABLE, "boom", retryable=True)
        self.sent.append(batch)
        return SyncAck(accepted=True, batch_id=batch["batch_id"])


def _engine(rest=None, **overrides):
    rest = rest or FakeRest()
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    engine = SyncEngine(rest, None, sleep=_sleep, **overrides)
    engine._sleeps = sleeps  # type: ignore[attr-defined]
    return engine


@pytest.mark.unit
def test_build_batch_flat_wire_shape():
    batch = build_batch("account", {"account": {"login": "1"}, "terminal": {}})
    assert set(batch) == {"batch_id", "kind", "items", "count", "produced_at"}
    assert batch["kind"] == "account"
    assert batch["count"] == 1  # single account snapshot
    assert batch["produced_at"].endswith("Z")
    assert "account_id" not in batch  # no per-batch account_id (§3.3, D-SYNC-2)
    # batch_id is a uuidv7 (version nibble 7)
    assert batch["batch_id"][14] == "7"


@pytest.mark.unit
def test_build_batch_rejects_unknown_kind():
    with pytest.raises(ValueError):
        build_batch("ticks", {"ticks": []})


@pytest.mark.unit
def test_count_items_sums_record_lists():
    assert count_items("positions", {"positions": [{}, {}], "orders": [{}]}) == 3
    assert count_items("history_orders", {"orders": [{}], "deals": [{}, {}]}) == 3
    assert count_items("positions", {"positions": [], "orders": []}) == 0
    assert count_items("account", {"account": {}, "terminal": {}}) == 1


@pytest.mark.unit
def test_parse_broker_time_rules():
    assert parse_broker_time("2026-11-16T22:33:39") is not None  # timezone-less ISO
    assert parse_broker_time("2026-11-16T22:33:39Z") is not None  # RFC 3339
    assert parse_broker_time(1792000000) is not None  # epoch
    assert parse_broker_time("garbage") is None  # unparseable never orders anything
    assert parse_broker_time(None) is None
    assert parse_broker_time("") is None


@pytest.mark.unit
def test_checkpoint_never_moves_backwards():
    cp = SyncCheckpoint()
    assert cp.iso == "1970-01-01T00:00:00Z"
    assert cp.advance("2026-11-16T22:33:39") is True
    assert cp.advance("2026-11-16T22:00:00") is False  # backwards: refused
    assert cp.advance("garbage") is False  # unparseable: refused
    assert cp.iso == "2026-11-16T22:33:39+00:00".replace("+00:00", "Z")


@pytest.mark.unit
def test_checkpoint_advances_to_max_broker_time_in_items():
    cp = SyncCheckpoint()
    moved = cp.advance_from_items(
        {
            "orders": [
                {"ticket": 1, "time_setup": "2026-11-16T10:00:00"},
                {"ticket": 2, "time_setup": "2026-11-16T12:00:00"},
            ],
            "deals": [{"ticket": 9, "time": "2026-11-16T11:00:00"}],
        }
    )
    assert moved == 1
    assert cp.value == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_pending_queue_eviction_warns():
    queue = BoundedPendingQueue(3, "test/q")
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m), level="WARNING", format="{message}")
    try:
        for i in range(4):
            evicted = queue.put({"batch_id": f"b{i}"}, item_id=f"b{i}")
    finally:
        logger.remove(handler_id)
    assert len(queue) == 3
    assert evicted is not None and evicted["batch_id"] == "b0"
    assert "evicted oldest" in "".join(messages)


@pytest.mark.unit
async def test_sync_engine_snapshots_keep_only_latest():
    rest = FakeRest()
    engine = _engine(rest)
    engine.enqueue("account", {"account": {"login": "1"}, "terminal": {}})
    engine.enqueue("account", {"account": {"login": "1"}, "terminal": {}})
    engine.enqueue("positions", {"positions": [], "orders": []})
    assert engine.pending_count() == 2  # one account + one positions
    sent, failed = await engine.flush()
    assert (sent, failed) == (2, 0)
    assert [b["kind"] for b in rest.sent] == ["account", "positions"]


@pytest.mark.unit
async def test_sync_engine_history_queue_bounded_at_32():
    rest = FakeRest()
    engine = _engine(rest)
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m), level="WARNING", format="{message}")
    try:
        for _ in range(33):
            engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    finally:
        logger.remove(handler_id)
    assert engine.pending_count() == 32
    assert "evicted oldest" in "".join(messages)
    sent, failed = await engine.flush()
    assert sent == 32 and failed == 0


@pytest.mark.unit
async def test_sync_engine_flush_stops_at_first_failure_and_keeps_rest():
    rest = FakeRest()
    engine = _engine(rest)
    engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    engine.enqueue("history_orders", {"orders": [{"ticket": 2}], "deals": []})
    rest.fail_next = True
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.pending_count() == 2  # both still queued, in order
    sent, failed = await engine.flush()
    assert (sent, failed) == (2, 0)


@pytest.mark.unit
async def test_sync_history_pages_500_with_250ms_pause():
    rest = FakeRest()
    orders = [{"ticket": i, "time_setup": "2026-11-16T10:00:00"} for i in range(1200)]

    async def fake_history_orders(from_dt, to_dt):
        return {"orders": orders, "deals": []}

    async def fake_history_positions(from_dt, to_dt):
        return {"positions": []}

    engine = _engine(rest, history_orders=fake_history_orders, history_positions=fake_history_positions)
    sent = await engine.sync_history()
    assert sent["history_orders"] == 3  # 500 + 500 + 200
    assert [b["count"] for b in rest.sent] == [500, 500, 200]
    assert engine._sleeps == [0.25, 0.25]  # between pages only, never after the last
    assert engine.checkpoint.value == datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


@pytest.mark.unit
async def test_handle_sync_request_sends_account_then_positions_then_history():
    rest = FakeRest()

    async def account_snapshot():
        return {"account": {"login": "1"}, "terminal": {}}

    async def positions_snapshot():
        return {"positions": [], "orders": []}

    async def history_orders(from_dt, to_dt):
        return {"orders": [], "deals": []}

    async def history_positions(from_dt, to_dt):
        return {"positions": []}

    engine = _engine(
        rest,
        account_snapshot=account_snapshot,
        positions_snapshot=positions_snapshot,
        history_orders=history_orders,
        history_positions=history_positions,
    )
    await engine.handle_sync_request()
    assert [b["kind"] for b in rest.sent] == ["account", "positions"]


@pytest.mark.unit
def test_reset_for_account_switch_discards_old_batches():
    engine = _engine()
    engine.enqueue("account", {"account": {"login": "1"}, "terminal": {}})
    engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    engine.checkpoint.advance("2026-11-16T12:00:00")
    engine.reset_for_account_switch()
    assert engine.pending_count() == 0
    assert engine.checkpoint.iso == "1970-01-01T00:00:00Z"


@pytest.mark.unit
def test_chunk_items_pages():
    assert chunk_items(list(range(1200)), 500) == [
        list(range(500)),
        list(range(500, 1000)),
        list(range(1000, 1200)),
    ]
    assert chunk_items([], 500) == []


@pytest.mark.unit
def test_flush_delay_backoff_values():
    from nse_bridge.server.sync import SyncEngine as _SE

    assert _SE.flush_delay_s(0) == 5.0
    assert _SE.flush_delay_s(1) == 10.0
    assert _SE.flush_delay_s(2) == 20.0
    assert _SE.flush_delay_s(3) == 40.0
    assert _SE.flush_delay_s(4) == 60.0  # capped, never hammers a dead server
    assert _SE.flush_delay_s(10) == 60.0


class _AlwaysFailingRest:
    """Fails every send; retryable or not, per construction."""

    def __init__(self, *, retryable: bool = True) -> None:
        from nse_bridge.server.errors import ServerErrorCode

        self._retryable = retryable
        self._code = ServerErrorCode.SERVER_UNAVAILABLE if retryable else ServerErrorCode.MALFORMED_RESPONSE
        self.calls = 0

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        from nse_bridge.server.errors import ServerError

        self.calls += 1
        raise ServerError(self._code, "boom", retryable=self._retryable)


class _FlakyRest:
    """Fails the first ``fail_times`` sends (retryable), then accepts."""

    def __init__(self, *, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.sent: list[dict[str, Any]] = []
        self.calls = 0

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        from nse_bridge.server.errors import ServerError, ServerErrorCode

        self.calls += 1
        if self.calls <= self.fail_times:
            raise ServerError(ServerErrorCode.SERVER_UNAVAILABLE, "boom", retryable=True)
        self.sent.append(batch)
        return SyncAck(accepted=True, batch_id=batch["batch_id"])


@pytest.mark.unit
async def test_flush_retries_with_the_same_batch_id():
    """A lost ack must not double-apply: the retry reuses the batch id
    (the idempotency key), so the server dedups it."""
    rest = _FlakyRest(fail_times=1)
    engine = _engine(rest)
    batch = engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.pending_count() == 1
    assert engine.attempts_for(batch["batch_id"]) == 1
    sent, failed = await engine.flush()
    assert (sent, failed) == (1, 0)
    assert [b["batch_id"] for b in rest.sent] == [batch["batch_id"]]
    assert engine.attempts_for(batch["batch_id"]) == 0  # ack clears the count
    assert engine.dead_letter_count() == 0


@pytest.mark.unit
async def test_non_retryable_failure_dead_letters_on_first_failure():
    """A server rejection (accepted: false / malformed) can never
    succeed on retry — it dead-letters immediately, not after 5."""
    rest = _AlwaysFailingRest(retryable=False)
    engine = _engine(rest)
    batch = engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.pending_count() == 0  # gone from the queue, terminal
    assert engine.dead_letter_count() == 1
    entry = engine.dead_letter_snapshot()[0]
    assert entry["batch"]["batch_id"] == batch["batch_id"]
    assert entry["attempts"] == 1
    assert entry["error_code"] == "malformed_response"
    # A second flush does NOT resurrect it.
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 0)
    assert rest.calls == 1


@pytest.mark.unit
async def test_retryable_failure_dead_letters_after_max_attempts():
    from nse_bridge.server.sync import SYNC_MAX_ATTEMPTS

    rest = _AlwaysFailingRest(retryable=True)
    engine = _engine(rest)
    batch = engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    for attempt in range(SYNC_MAX_ATTEMPTS - 1):
        sent, failed = await engine.flush()
        assert (sent, failed) == (0, 1)
        assert engine.pending_count() == 1  # still queued, still retryable
        assert engine.attempts_for(batch["batch_id"]) == attempt + 1
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.pending_count() == 0
    assert engine.dead_letter_count() == 1
    assert engine.dead_letter_snapshot()[0]["attempts"] == SYNC_MAX_ATTEMPTS
    assert engine.dead_letter_snapshot()[0]["error_code"] == "server_unavailable"
    assert rest.calls == SYNC_MAX_ATTEMPTS  # bounded — never an infinite silent retry


@pytest.mark.unit
async def test_unexpected_send_failure_requeues_instead_of_losing_batches():
    """A non-ServerError from send_batch used to lose every batch the
    flush had already popped. They must survive, in order."""

    class _ExplodingRest:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.armed = True

        async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
            if self.armed:
                self.armed = False
                raise RuntimeError("transport blew up")
            self.sent.append(batch)
            return SyncAck(accepted=True, batch_id=batch["batch_id"])

    rest = _ExplodingRest()
    engine = _engine(rest)
    engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    engine.enqueue("history_orders", {"orders": [{"ticket": 2}], "deals": []})
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.pending_count() == 2
    sent, failed = await engine.flush()
    assert (sent, failed) == (2, 0)
    assert [b["items"]["orders"][0]["ticket"] for b in rest.sent] == [1, 2]


@pytest.mark.unit
async def test_enqueue_replay_dedup_same_batch_id():
    """Replaying a batch id that is already pending is a no-op — a
    reconnect replay can never double-queue the same batch."""
    rest = FakeRest()
    engine = _engine(rest)
    first = engine.enqueue(
        "history_orders", {"orders": [{"ticket": 1}], "deals": []}, batch_id="01900000000000000000000001"
    )
    second = engine.enqueue(
        "history_orders", {"orders": [{"ticket": 1}], "deals": []}, batch_id="01900000000000000000000001"
    )
    assert second is first
    assert engine.pending_count() == 1
    sent, failed = await engine.flush()
    assert (sent, failed) == (1, 0)
    assert len(rest.sent) == 1


@pytest.mark.unit
async def test_snapshot_supersede_drops_attempt_count():
    """A superseded snapshot starts fresh: the old attempt count must
    not leak into the new batch's retry budget."""
    rest = _FlakyRest(fail_times=1)
    engine = _engine(rest)
    old = engine.enqueue("account", {"account": {"login": "1"}, "terminal": {}})
    sent, failed = await engine.flush()
    assert (sent, failed) == (0, 1)
    assert engine.attempts_for(old["batch_id"]) == 1
    new = engine.enqueue("account", {"account": {"login": "1"}, "terminal": {}})
    assert engine.attempts_for(old["batch_id"]) == 0
    assert new["batch_id"] != old["batch_id"]
    sent, failed = await engine.flush()
    assert (sent, failed) == (1, 0)


@pytest.mark.unit
async def test_reset_for_account_switch_clears_retry_and_dead_letter_state():
    rest = _AlwaysFailingRest(retryable=False)
    engine = _engine(rest)
    batch = engine.enqueue("history_orders", {"orders": [{"ticket": 1}], "deals": []})
    await engine.flush()
    assert engine.dead_letter_count() == 1
    engine.reset_for_account_switch()
    assert engine.dead_letter_count() == 0
    assert engine.attempts_for(batch["batch_id"]) == 0
    assert engine._accepted_batch_ids == set()


@pytest.mark.unit
def test_queue_eviction_log_names_the_evicted_item():
    """The eviction warning used to name the NEW item's id, not the
    evicted one's — the log must identify what was actually lost."""
    queue = BoundedPendingQueue(2, "test/q")
    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(m), level="WARNING", format="{message}")
    try:
        queue.put({"batch_id": "evicted-one"}, item_id="evicted-one")
        queue.put({"batch_id": "kept-one"}, item_id="kept-one")
        queue.put({"batch_id": "new-one"}, item_id="new-one")
    finally:
        logger.remove(handler_id)
    joined = "".join(messages)
    assert "evicted-one" in joined
    assert "new-one" not in joined


@pytest.mark.unit
def test_manifest_only_truthful_fields():
    manifest = build_manifest("dev-1")
    assert manifest["device_id"] == "dev-1"
    assert manifest["action_count"] == 91  # full local registry stays intact
    assert len(manifest["actions"]) == 91
    assert manifest["manifest_hash"] == manifest_hash()
    assert len(manifest["manifest_hash"]) == 16
    # Omissions (documented in nse_bridge/server/manifest.py): no
    # invented tier assignments, no invented terminal build number,
    # no MCP server version.
    assert "tiers" not in manifest
    assert "terminal_build" not in manifest
    assert "mcp_server_version" not in manifest
    assert all("tier" not in action for action in manifest["actions"])


@pytest.mark.unit
def test_manifest_hash_deterministic():
    assert manifest_hash() == manifest_hash()
