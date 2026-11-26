"""Bounded pending queues (CONTRACT v1 §3.3 / §5.4 / §6.5).

The contract bounds every outbound queue the Connector keeps:

- sync batches: at most 32 unacknowledged batches; the oldest is
  evicted WITH a warning log when full — never silently discarded
  (§3.3). An evicted batch is re-created from the checkpoint on the
  next cycle.
- command results: the same 32-batch bound semantics (§6.5) — results
  are persisted across reconnects and never dropped silently; a
  dropped-from-the-wire result is re-derived from server state on the
  next ``sync_request`` (§5.4).

This module is the single implementation of that bound. Eviction
always logs a warning naming the queue and the evicted item's id.

DROP POLICY (B4, stated once here — the sync engine's docstring
states the full order across snapshots / history / dead-letter):
this queue evicts OLDEST first, because the oldest entry is the one
whose loss is recoverable: for sync history the checkpoint has not
advanced past it, so the next cycle re-creates it from the
checkpoint; for command results the result is re-derived from server
state on the next ``sync_request``. The NEWEST entry is never the one
dropped — it is the freshest state and the one the server has waited
for least. Eviction is a bounded, logged, recoverable event — never
a silent discard.
"""

from __future__ import annotations

import threading
from collections import deque

from loguru import logger

__all__ = ["BoundedPendingQueue"]


class BoundedPendingQueue[T]:
    """FIFO queue with a hard bound; oldest evicted WITH a warning."""

    def __init__(self, max_items: int, name: str) -> None:
        if max_items < 1:
            raise ValueError("max_items must be >= 1")
        self.max_items = max_items
        self.name = name
        self._items: deque[T] = deque()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, item: T, *, item_id: str | None = None) -> T | None:
        """Append ``item``; return the evicted oldest item, if any.

        Eviction is logged as a warning (§3.3: never silently
        discarded) and the evicted item is returned so the caller can
        account for it (e.g. re-create a batch from the checkpoint).
        """
        evicted: T | None = None
        with self._lock:
            if len(self._items) >= self.max_items:
                evicted = self._items.popleft()
                logger.warning(
                    "[QUEUE] {} full (max {}): evicted oldest item id={} — "
                    "it must be re-created/re-derived, never assumed delivered",
                    self.name,
                    self.max_items,
                    _describe(evicted),
                )
            self._items.append(item)
        return evicted

    def pop_oldest(self) -> T | None:
        with self._lock:
            return self._items.popleft() if self._items else None

    def peek_oldest(self) -> T | None:
        with self._lock:
            return self._items[0] if self._items else None

    def snapshot(self) -> list[T]:
        with self._lock:
            return list(self._items)

    def remove(self, predicate) -> int:
        """Remove every item matching ``predicate``; return how many."""
        with self._lock:
            kept = [i for i in self._items if not predicate(i)]
            removed = len(self._items) - len(kept)
            self._items = deque(kept)
        return removed

    def clear(self) -> list[T]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items


def _describe(item: object) -> str:
    if isinstance(item, dict):
        for key in ("batch_id", "correlation_id", "command_id", "message_id"):
            if item.get(key):
                return str(item[key])
        return item.get("type", "?") if isinstance(item.get("type"), str) else "?"
    return str(item)[:64]
