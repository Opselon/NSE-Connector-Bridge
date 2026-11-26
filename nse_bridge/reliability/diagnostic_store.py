"""Durable backing for the diagnostic queue (spec §12, §16).

Today the diagnostic queue is in-memory only
(:class:`nse_bridge.reliability.telemetry.DiagnosticQueue`): a Bridge
restart loses every queued ERROR/WARN event that was produced but
not yet delivered. Only ``action.result`` payloads had a durable
(JSONL) store. The queue used to drain by TTL until a diagnostics
endpoint existed (contract dependency D1 — CONTRACT v1 §4.2 defined
none); since P5a the endpoint exists and the queue drains through
the attached HTTP sink, so an in-memory queue meant those events
simply vanished on restart.

This module fixes the persistence half, following the exact pattern
of :mod:`nse_bridge.reliability.result_store` (DO NOT diverge from
it without reason): :class:`DurableDiagnosticStore` keeps a bounded
JSONL file (``pending_diagnostics.jsonl`` in the bridge ``data_dir``)
mirroring the in-memory queue, and :class:`DurableDiagnosticQueue`
is the drop-in queue that keeps the two in sync. On startup the
persisted events are re-queued (attempt counts preserved, retry
deadlines reset — the deadline is monotonic-clock based and
meaningless across a restart); on delivery, expiry, or abandonment
the event leaves both the queue and the file.

Bounds (same discipline as the in-memory queue, §12):

- at most 128 items (the ``TELEMETRY_QUEUE_MAX_ITEMS`` bound); the
  oldest is evicted WITH a warning when full — never silently
  discarded;
- at most 1 MiB on disk; a corrupt line is skipped with a warning
  (never fatal — a damaged store must not block startup);
- the file is created lazily on the first persisted event, never at
  construction (tests and read-only runs create no files);
- writes go through a temp file + atomic replace, so a crash
  mid-write cannot corrupt the store.

What this does NOT do: it invents no upload URL or endpoint. The
sink is the :class:`HttpDiagnosticsSink`'s job (P5a): persistence
preserves the events, delivery drains them through the attached sink.
When no sink is attached the queue still drains by TTL and the sink
stays inert (:class:`NullDiagnosticsSink`).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from loguru import logger

from .telemetry import (
    TELEMETRY_QUEUE_MAX_ITEMS,
    DiagnosticQueue,
    DiagnosticsSink,
    TelemetryEvent,
)

__all__ = ["DurableDiagnosticQueue", "DurableDiagnosticStore"]

DIAGNOSTIC_STORE_MAX_ITEMS = TELEMETRY_QUEUE_MAX_ITEMS  # §12 bound, same as the in-memory queue
DIAGNOSTIC_STORE_MAX_BYTES = 1_048_576  # 1 MiB on disk


def _valid_record(record: Any) -> bool:
    """A persisted record is ``{"dedup_key", "attempts", "event"}``
    with an event dict carrying at least ``event_type`` and
    ``timestamp``. Anything else is corruption, never a crash."""
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("dedup_key"), str) or not record["dedup_key"]:
        return False
    if not isinstance(record.get("attempts"), int) or record["attempts"] < 0:
        return False
    event = record.get("event")
    return (
        isinstance(event, dict) and isinstance(event.get("event_type"), str) and isinstance(event.get("timestamp"), str)
    )


class DurableDiagnosticStore:
    """Bounded JSONL mirror of the diagnostic queue."""

    def __init__(
        self,
        path: Path,
        *,
        max_items: int = DIAGNOSTIC_STORE_MAX_ITEMS,
        max_bytes: int = DIAGNOSTIC_STORE_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        """All persisted records, oldest first. Missing file → [].
        Corrupt lines are skipped with a warning, never fatal."""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("[DIAGNOSTICS] cannot read durable diagnostic store {}: {}", self.path, exc)
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.warning("[DIAGNOSTICS] skipping corrupt line in durable diagnostic store {}", self.path)
                continue
            if _valid_record(record):
                records.append(record)
            else:
                logger.warning(
                    "[DIAGNOSTICS] skipping malformed record in durable diagnostic store {}",
                    self.path,
                )
        return records

    def append(self, record: dict[str, Any]) -> bool:
        """Persist ``record``. Returns False when it was dropped
        (invalid shape). Evicts the oldest persisted record WITH a
        warning when the bound is hit."""
        if not _valid_record(record):
            logger.warning("[DIAGNOSTICS] refusing to persist an invalid diagnostic record")
            return False
        with self._lock:
            records = self._load_locked()
            records = [r for r in records if r["dedup_key"] != record["dedup_key"]]
            if len(records) >= self.max_items:
                evicted = records.pop(0)
                logger.warning(
                    "[DIAGNOSTICS] durable diagnostic store full (max {}): evicted oldest "
                    "event_type={} — it was never delivered",
                    self.max_items,
                    evicted["event"].get("event_type"),
                )
            records.append(record)
            self._rewrite_locked(records)
            return True

    def remove(self, dedup_key: str) -> int:
        """Remove every persisted record with this key. Returns how many."""
        with self._lock:
            records = self._load_locked()
            kept = [r for r in records if r["dedup_key"] != dedup_key]
            removed = len(records) - len(kept)
            if removed:
                self._rewrite_locked(kept)
            return removed

    def replace(self, records: list[dict[str, Any]]) -> None:
        """Rewrite the store from ``records`` (oldest first), dropping
        invalid entries with a warning. The queue uses this to resync
        after a drain, so the file always mirrors the live queue."""
        with self._lock:
            valid = [r for r in records if _valid_record(r)]
            if len(valid) != len(records):
                logger.warning(
                    "[DIAGNOSTICS] dropping {} invalid record(s) while rewriting the durable diagnostic store",
                    len(records) - len(valid),
                )
            self._rewrite_locked(valid)

    def clear(self) -> None:
        with self._lock:
            self._rewrite_locked([])

    # ------------------------------------------------------------------
    def _rewrite_locked(self, records: list[dict[str, Any]]) -> None:
        """Write ``records`` atomically (temp file + replace). Enforces
        the byte cap by dropping the oldest records first. Crash-safe
        (B4): the temp file is fsynced before the replace and the
        directory is fsynced after it, so a crash leaves the old file
        or the new file, never a torn one."""
        payload = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records)
        if payload:
            payload += "\n"
        encoded = payload.encode("utf-8")
        while len(encoded) > self.max_bytes and records:
            dropped = records.pop(0)
            logger.warning(
                "[DIAGNOSTICS] durable diagnostic store over {} bytes: evicted oldest event_type={}",
                self.max_bytes,
                dropped["event"].get("event_type"),
            )
            payload = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in records)
            if payload:
                payload += "\n"
            encoded = payload.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


class DurableDiagnosticQueue(DiagnosticQueue):
    """A :class:`DiagnosticQueue` whose contents are mirrored to a
    :class:`DurableDiagnosticStore`.

    Drop-in replacement for the in-memory queue: ``put`` persists,
    ``drain`` un-persists whatever left the queue (delivered, expired,
    or abandoned after max attempts). Persisted events are loaded
    back into the queue at construction, so a Bridge restart re-queues
    undelivered diagnostics — with their attempt counts preserved and
    their retry deadlines reset (due immediately). The bound still
    applies on reload: stale events purge by TTL, and the oldest are
    evicted with a warning when over the bound. The store file is
    only created on the first persisted event — construction itself
    is read-only.
    """

    def __init__(
        self,
        store: DurableDiagnosticStore,
        *,
        max_items: int = TELEMETRY_QUEUE_MAX_ITEMS,
        ttl_s: float = 3600.0,
        max_attempts: int = 5,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 60.0,
        clock: Any | None = None,
    ) -> None:
        super().__init__(
            max_items=max_items,
            ttl_s=ttl_s,
            max_attempts=max_attempts,
            backoff_base_s=backoff_base_s,
            backoff_cap_s=backoff_cap_s,
            clock=clock,
        )
        self.store = store
        for record in store.load():
            try:
                event = TelemetryEvent(**record["event"])
            except (TypeError, ValueError):
                logger.warning(
                    "[DIAGNOSTICS] skipping persisted diagnostic event with an unusable shape: event_type={}",
                    record["event"].get("event_type"),
                )
                continue
            # Bypass the store mirror on reload (already persisted);
            # the bound + TTL still apply — oldest evicted / stale
            # purged with a warning by the base class.
            if not super().put(event):
                continue  # deduped against an entry already queued
            for entry in self._items:
                if entry.dedup_key == record["dedup_key"]:
                    entry.attempts = record["attempts"]
                    break
        # The base class only purges entries already queued when a new
        # one arrives, so a stale record re-queued above would survive
        # until the next mutation. Enforce TTL on the reloaded queue
        # itself, then mirror the result back to the file.
        with self._lock:
            self._purge_expired_locked()
        # The load may have purged stale / evicted over-bound records;
        # mirror that back to the file immediately.
        self._sync_store()

    def put(self, event: TelemetryEvent) -> bool:
        queued = super().put(event)
        if queued:
            self._sync_store()
        return queued

    def drain(self, sink: DiagnosticsSink) -> int:
        delivered = super().drain(sink)
        self._sync_store()
        return delivered

    def _sync_store(self) -> None:
        """Rewrite the store from the live queue (atomic). Lazy: no
        file is created while the queue is empty and none exists."""
        records = self.records()
        if not records and not self.store.path.is_file():
            return
        self.store.replace(records)
