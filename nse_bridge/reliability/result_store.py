"""Durable persistence for unacknowledged command results (§6.5).

Today the pending command-result queue is in-memory only
(:class:`nse_bridge.server.queue.BoundedPendingQueue`): a Bridge
restart mid-operation silently loses every result that was produced
but not yet delivered over the WS channel. The server then never
learns the outcome of an acknowledged command — the round-trip
invariant ("every issued command reaches exactly one terminal state")
breaks at the delivery step, not the execution step.

This module fixes the delivery half: :class:`DurableResultStore`
keeps a bounded JSONL file (``pending_results.jsonl`` in the bridge
``data_dir``) mirroring the in-memory queue, and
:class:`DurableResultQueue` is the drop-in queue that keeps the two
in sync. On startup the persisted results are re-queued and the
channel re-sends them on reconnect (§6.5); on a successful send the
result is removed from both the queue and the file.

Bounds (same discipline as the in-memory queue, §3.3/§6.5):

- at most 32 items (the §6.5 bound); the oldest is evicted WITH a
  warning when full — never silently discarded;
- at most 1 MiB on disk; a corrupt line is skipped with a warning
  (never fatal — a damaged store must not block startup);
- the file is created lazily on the first persisted result, never at
  construction (tests and read-only runs create no files);
- writes go through a temp file + atomic replace, so a crash
  mid-write cannot corrupt the store.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from ..server.queue import BoundedPendingQueue

__all__ = ["DurableResultQueue", "DurableResultStore"]

RESULT_STORE_MAX_ITEMS = 32  # §6.5 bound, same as the in-memory queue
RESULT_STORE_MAX_BYTES = 1_048_576  # 1 MiB on disk


def _correlation_id(result: Any) -> str | None:
    if isinstance(result, dict):
        value = result.get("correlation_id") or result.get("command_id")
        return str(value) if value else None
    return None


class DurableResultStore:
    """Bounded JSONL mirror of the pending-results queue."""

    def __init__(
        self,
        path: Path,
        *,
        max_items: int = RESULT_STORE_MAX_ITEMS,
        max_bytes: int = RESULT_STORE_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        """All persisted results, oldest first. Missing file → [].
        Corrupt lines are skipped with a warning, never fatal."""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        results: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[RESULTS] cannot read durable result store {}: {}", self.path, exc)
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                logger.warning("[RESULTS] skipping corrupt line in durable result store {}", self.path)
                continue
            if isinstance(item, dict):
                results.append(item)
        return results

    def append(self, result: dict[str, Any]) -> bool:
        """Persist ``result``. Returns False when it was dropped
        (invalid shape). Evicts the oldest persisted result WITH a
        warning when the bound is hit."""
        if not isinstance(result, dict) or not _correlation_id(result):
            logger.warning("[RESULTS] refusing to persist a result with no correlation_id")
            return False
        with self._lock:
            items = self._load_locked()
            items = [i for i in items if _correlation_id(i) != _correlation_id(result)]
            if len(items) >= self.max_items:
                evicted = items.pop(0)
                logger.warning(
                    "[RESULTS] durable result store full (max {}): evicted oldest "
                    "result correlation_id={} — it must be re-derived from server "
                    "state, never assumed delivered",
                    self.max_items,
                    _correlation_id(evicted),
                )
            items.append(result)
            self._rewrite_locked(items)
            return True

    def remove(self, correlation_id: str) -> int:
        """Remove every persisted result with this id. Returns how many."""
        with self._lock:
            items = self._load_locked()
            kept = [i for i in items if _correlation_id(i) != correlation_id]
            removed = len(items) - len(kept)
            if removed:
                self._rewrite_locked(kept)
            return removed

    def clear(self) -> None:
        with self._lock:
            self._rewrite_locked([])

    # ------------------------------------------------------------------
    def _rewrite_locked(self, items: list[dict[str, Any]]) -> None:
        """Write ``items`` atomically (temp file + replace). Enforces
        the byte cap by dropping the oldest items first."""
        payload = "\n".join(json.dumps(i, ensure_ascii=False, default=str) for i in items)
        if payload:
            payload += "\n"
        encoded = payload.encode("utf-8")
        while len(encoded) > self.max_bytes and items:
            dropped = items.pop(0)
            logger.warning(
                "[RESULTS] durable result store over {} bytes: evicted oldest result correlation_id={}",
                self.max_bytes,
                _correlation_id(dropped),
            )
            payload = "\n".join(json.dumps(i, ensure_ascii=False, default=str) for i in items)
            if payload:
                payload += "\n"
            encoded = payload.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(encoded)
        os.replace(tmp, self.path)


class DurableResultQueue(BoundedPendingQueue[dict[str, Any]]):
    """A :class:`BoundedPendingQueue` for command results whose
    contents are mirrored to a :class:`DurableResultStore`.

    Drop-in replacement for the channel's in-memory results queue:
    ``put`` persists, ``remove``/``pop_oldest``/``clear`` un-persist.
    Persisted results are loaded back into the queue at construction,
    so a Bridge restart re-queues unacknowledged results for delivery
    on reconnect (§6.5). The store file is only created on the first
    persisted result — construction itself is read-only.
    """

    def __init__(self, max_items: int, name: str, store: DurableResultStore) -> None:
        super().__init__(max_items, name)
        self.store = store
        for result in store.load():
            # Bypass the store mirror on reload (already persisted);
            # the bound still applies — oldest evicted with a warning.
            super().put(result, item_id=_correlation_id(result))

    def put(self, item: dict[str, Any], *, item_id: str | None = None) -> dict[str, Any] | None:
        evicted = super().put(item, item_id=item_id or _correlation_id(item))
        if evicted is not None and _correlation_id(evicted) is not None:
            self.store.remove(str(_correlation_id(evicted)))
        self.store.append(item)
        return evicted

    def pop_oldest(self) -> dict[str, Any] | None:
        item = super().pop_oldest()
        if item is not None and _correlation_id(item) is not None:
            self.store.remove(str(_correlation_id(item)))
        return item

    def remove(self, predicate) -> int:  # type: ignore[no-untyped-def]  # matches the base signature
        before = self.snapshot()
        removed = super().remove(predicate)
        if removed:
            before_ids = {_correlation_id(i) for i in before}
            after_ids = {_correlation_id(i) for i in self.snapshot()}
            for correlation_id in before_ids - after_ids:
                if correlation_id is not None:
                    self.store.remove(correlation_id)
        return removed

    def clear(self) -> list[dict[str, Any]]:
        items = super().clear()
        self.store.clear()
        return items
