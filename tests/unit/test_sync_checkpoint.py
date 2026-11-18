"""Unit tests for durable sync-checkpoint persistence (spec §16,
gap §5 B1 task 4).

Label: unit. No MT5, no network — the REST client and the snapshot
providers are fakes; the store lives under ``tmp_path``.

A restart with a valid checkpoint must NOT trigger a full history
backfill: the second ``SyncEngine`` over the same directory resumes
from the persisted broker time. A corrupt checkpoint is handled
(logged + epoch backfill), never a crash.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nse_bridge.server.rest import SyncAck
from nse_bridge.server.sync import EPOCH_ISO, SyncEngine
from nse_bridge.server.sync_checkpoint import CHECKPOINT_FILENAME, SyncCheckpointStore

pytestmark = pytest.mark.unit


class FakeRest:
    """Records batches; never fails."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        self.sent.append(batch)
        return SyncAck(accepted=True, batch_id=batch["batch_id"])


def _store(tmp_path: Path) -> SyncCheckpointStore:
    return SyncCheckpointStore(tmp_path / CHECKPOINT_FILENAME)


def _history_fetchers(seen_from: list[datetime]):
    """History providers returning one broker-time-bearing record each."""

    async def history_orders(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        seen_from.append(from_dt)
        return {
            "orders": [{"ticket": 1, "time_setup": "2026-11-16T12:00:00"}],
            "deals": [{"ticket": 9, "time": "2026-11-16T11:00:00"}],
        }

    async def history_positions(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        seen_from.append(from_dt)
        return {"positions": [{"ticket": 5, "close_time": "2026-11-16T10:00:00"}]}

    return history_orders, history_positions


async def _no_sleep(delay: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Store alone
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_store_roundtrip_with_account(tmp_path: Path):
    store = _store(tmp_path)
    assert store.load() is None  # missing file → no checkpoint
    assert store.save("2026-11-16T12:00:00Z", account_id="12345678901") is True
    record = store.load()
    assert record is not None
    assert record["checkpoint"] == "2026-11-16T12:00:00Z"
    assert record["account_id"] == "12345678901"
    assert (tmp_path / CHECKPOINT_FILENAME).is_file()


@pytest.mark.unit
def test_store_save_refuses_unparseable_timestamp(tmp_path: Path):
    store = _store(tmp_path)
    assert store.save("not-a-time") is False
    assert not (tmp_path / CHECKPOINT_FILENAME).exists()  # nothing written


@pytest.mark.unit
def test_store_corrupt_file_is_ignored_not_fatal(tmp_path: Path):
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text("{not json", encoding="utf-8")
    assert _store(tmp_path).load() is None
    path.write_text(json.dumps({"checkpoint": "garbage"}), encoding="utf-8")
    assert _store(tmp_path).load() is None
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert _store(tmp_path).load() is None
    path.write_text("x" * 5000, encoding="utf-8")  # oversize
    assert _store(tmp_path).load() is None


@pytest.mark.unit
def test_store_missing_file_creates_nothing(tmp_path: Path):
    store = _store(tmp_path)
    assert store.load() is None
    assert not (tmp_path / CHECKPOINT_FILENAME).exists()  # read-only construction


@pytest.mark.unit
def test_store_clear_removes_file(tmp_path: Path):
    store = _store(tmp_path)
    store.save("2026-11-16T12:00:00Z")
    assert (tmp_path / CHECKPOINT_FILENAME).is_file()
    store.clear()
    assert not (tmp_path / CHECKPOINT_FILENAME).exists()
    assert store.load() is None


# ---------------------------------------------------------------------------
# Engine: checkpoint survives a second instance over the same directory
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_checkpoint_survives_second_engine_instance(tmp_path: Path):
    store = _store(tmp_path)
    seen_from: list[datetime] = []
    orders, positions = _history_fetchers(seen_from)
    engine1 = SyncEngine(
        FakeRest(), None, sleep=_no_sleep,
        checkpoint_store=store,
        history_orders=orders, history_positions=positions,
    )
    assert engine1.checkpoint.iso == EPOCH_ISO  # fresh start
    await engine1.sync_history()
    assert engine1.checkpoint.iso == "2026-11-16T12:00:00Z"
    assert store.load() is not None
    assert store.load()["checkpoint"] == "2026-11-16T12:00:00Z"

    # A second engine over the same directory resumes — no full backfill.
    seen_from.clear()
    engine2 = SyncEngine(
        FakeRest(), None, sleep=_no_sleep,
        checkpoint_store=_store(tmp_path),
        history_orders=orders, history_positions=positions,
    )
    assert engine2.checkpoint.iso == "2026-11-16T12:00:00Z"
    await engine2.sync_history()
    assert seen_from[0] == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.mark.unit
async def test_corrupt_checkpoint_falls_back_to_full_backfill(tmp_path: Path):
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text("{corrupt", encoding="utf-8")
    seen_from: list[datetime] = []
    orders, positions = _history_fetchers(seen_from)
    engine = SyncEngine(
        FakeRest(), None, sleep=_no_sleep,
        checkpoint_store=_store(tmp_path),
        history_orders=orders, history_positions=positions,
    )
    assert engine.checkpoint.iso == EPOCH_ISO  # corrupt → epoch, no crash
    await engine.sync_history()  # the backfill itself still works
    assert engine.checkpoint.iso == "2026-11-16T12:00:00Z"
    assert seen_from[0] == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.unit
def test_checkpoint_from_another_account_is_ignored(tmp_path: Path):
    store = _store(tmp_path)
    store.save("2026-11-16T12:00:00Z", account_id="111")
    # This device is bound to a different account now (§7.3).
    ctx = SimpleNamespace(
        driver=SimpleNamespace(
            connection_state_dict=lambda: {"login": "222"},
        )
    )
    engine = SyncEngine(FakeRest(), ctx, checkpoint_store=_store(tmp_path))
    assert engine.checkpoint.iso == EPOCH_ISO


@pytest.mark.unit
def test_account_switch_resets_and_persists_epoch(tmp_path: Path):
    store = _store(tmp_path)
    store.save("2026-11-16T12:00:00Z", account_id="12345678901")
    engine = SyncEngine(FakeRest(), None, checkpoint_store=_store(tmp_path))
    assert engine.checkpoint.iso == "2026-11-16T12:00:00Z"
    engine.reset_for_account_switch()
    assert engine.checkpoint.iso == EPOCH_ISO
    # The reset is persisted: a restart must not resurrect the old
    # account's checkpoint for the new account.
    assert _store(tmp_path).load()["checkpoint"] == EPOCH_ISO


@pytest.mark.unit
async def test_engine_without_store_keeps_in_memory_behaviour(tmp_path: Path):
    """No store wired → the old in-memory checkpoint still works."""
    seen_from: list[datetime] = []
    orders, positions = _history_fetchers(seen_from)
    engine = SyncEngine(
        FakeRest(), None, sleep=_no_sleep,
        history_orders=orders, history_positions=positions,
    )
    assert engine.checkpoint_store is None
    await engine.sync_history()
    assert engine.checkpoint.iso == "2026-11-16T12:00:00Z"
    assert not (tmp_path / CHECKPOINT_FILENAME).exists()
