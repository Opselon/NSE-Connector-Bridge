"""Unit tests for the durable result store + queue (§6.5).

Label: unit. A Bridge restart mid-operation must not silently lose
an acknowledged command's result: results are mirrored to a bounded
JSONL file and re-queued on startup.
"""

from __future__ import annotations

import json

import pytest

from nse_bridge.reliability.result_store import DurableResultQueue, DurableResultStore

pytestmark = pytest.mark.unit


def _result(correlation_id: str) -> dict:
    return {
        "protocol_version": "1.0",
        "event_type": "action.result",
        "correlation_id": correlation_id,
        "status": "executed",
    }


# ---------------------------------------------------------------------------
# DurableResultStore
# ---------------------------------------------------------------------------
def test_store_file_created_lazily_on_first_append(tmp_path):
    path = tmp_path / "pending_results.jsonl"
    store = DurableResultStore(path)
    assert not path.exists()
    assert store.load() == []
    assert store.append(_result("c1")) is True
    assert path.exists()
    assert [r["correlation_id"] for r in store.load()] == ["c1"]


def test_store_round_trip_and_remove(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    store.append(_result("c1"))
    store.append(_result("c2"))
    assert store.remove("c1") == 1
    assert [r["correlation_id"] for r in store.load()] == ["c2"]
    assert store.remove("missing") == 0
    store.clear()
    assert store.load() == []


def test_store_bound_evicts_oldest_with_warning(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl", max_items=2)
    store.append(_result("c1"))
    store.append(_result("c2"))
    store.append(_result("c3"))
    assert [r["correlation_id"] for r in store.load()] == ["c2", "c3"]


def test_store_rejects_results_without_a_correlation_id(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    assert store.append({"event_type": "action.result"}) is False
    assert store.load() == []


def test_store_skips_corrupt_lines_never_fatal(tmp_path):
    path = tmp_path / "pending_results.jsonl"
    path.write_text(
        json.dumps(_result("c1")) + "\nthis is not json\n" + json.dumps(_result("c2")) + "\n",
        encoding="utf-8",
    )
    store = DurableResultStore(path)
    assert [r["correlation_id"] for r in store.load()] == ["c1", "c2"]


def test_store_byte_cap_evicts_oldest(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl", max_items=32, max_bytes=120)
    store.append(_result("c1"))
    store.append(_result("c2"))
    store.append(_result("c3"))
    loaded = store.load()
    assert len(loaded) <= 2
    assert loaded[-1]["correlation_id"] == "c3"


# ---------------------------------------------------------------------------
# DurableResultQueue (drop-in for the channel's in-memory queue)
# ---------------------------------------------------------------------------
def test_queue_mirrors_put_remove_and_pop_to_the_store(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    queue = DurableResultQueue(32, "test/results", store)
    queue.put(_result("c1"), item_id="c1")
    queue.put(_result("c2"), item_id="c2")
    assert [r["correlation_id"] for r in store.load()] == ["c1", "c2"]

    queue.remove(lambda r: r.get("correlation_id") == "c1")
    assert [r["correlation_id"] for r in store.load()] == ["c2"]

    assert queue.pop_oldest()["correlation_id"] == "c2"
    assert store.load() == []


def test_queue_reloads_persisted_results_on_construction(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    first = DurableResultQueue(32, "test/results", store)
    first.put(_result("c1"), item_id="c1")
    first.put(_result("c2"), item_id="c2")
    # Simulate a restart: a fresh queue over the same store file.
    second = DurableResultQueue(32, "test/results", store)
    assert len(second) == 2
    assert [r["correlation_id"] for r in second.snapshot()] == ["c1", "c2"]


def test_queue_eviction_also_evicts_from_the_store(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    queue = DurableResultQueue(2, "test/results", store)
    queue.put(_result("c1"), item_id="c1")
    queue.put(_result("c2"), item_id="c2")
    queue.put(_result("c3"), item_id="c3")
    assert [r["correlation_id"] for r in queue.snapshot()] == ["c2", "c3"]
    assert [r["correlation_id"] for r in store.load()] == ["c2", "c3"]


def test_queue_clear_clears_the_store(tmp_path):
    store = DurableResultStore(tmp_path / "pending_results.jsonl")
    queue = DurableResultQueue(32, "test/results", store)
    queue.put(_result("c1"), item_id="c1")
    queue.clear()
    assert len(queue) == 0
    assert store.load() == []
