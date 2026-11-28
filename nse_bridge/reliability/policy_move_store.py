"""Durable persistence for executed policy moves (audit P1-8).

An executed autonomous trailing/breakeven/TP move is a financial
action that must be reconcilable server-side — but the diagnostic
queue is the WRONG store for it: it is a transient delivery buffer
(events leave it on delivery, age out by TTL after 1 h, and dedup
by event type), so a delivered move would stop being retrievable
locally. The executor's in-memory ``_history`` (bounded deque, 50)
has the same flaw across a restart.

This module is therefore the durable audit record, following the
exact pattern of :mod:`nse_bridge.reliability.result_store` (DO NOT
diverge from it without reason): :class:`PolicyMoveStore` keeps a
bounded JSONL file (``policy_moves.jsonl`` in the bridge
``data_dir``) of :class:`nse_bridge.server.policy.PolicyMoveRecord`
dicts — one line per executed or failed move. Only executed/failed
moves are persisted: refused/no-op evaluations (``no_tighter_stop``,
``no_better_tp``) are the steady state of a 2 s poll loop and would
fill the ledger with noise, exactly as the Platform's trailing-TP
ledger records only events.

Bounds (same discipline as the result store):

- at most 500 records (a move is rare — step-gated, one per ticket
  per poll at most — so 500 is months of trailing history, not a
  day's worth);
- at most 1 MiB on disk; a corrupt line is skipped with a warning
  (never fatal — a damaged store must not block startup);
- the file is created lazily on the first persisted move, never at
  construction (tests and read-only runs create no files);
- writes go through a temp file + atomic replace, so a crash
  mid-write cannot corrupt the store.

Delivery to the Platform is NOT this store's job: the executor also
emits the move as a WARN ``policy.move.executed`` telemetry event
(INFO events are never queued by design — see ``telemetry.py`` — so
a financially significant autonomous move goes out as WARN), which
the durable diagnostic queue + ``HttpDiagnosticsSink`` carry to
``POST /api/v1/connector/diagnostics`` (CONTRACT §5.3 move-report
event). This store is the local, restart-proof, API-queryable copy.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = ["POLICY_MOVE_STORE_MAX_BYTES", "POLICY_MOVE_STORE_MAX_ITEMS", "PolicyMoveStore"]

POLICY_MOVE_STORE_MAX_ITEMS = 500  # P1-8 bound: executed/failed moves only
POLICY_MOVE_STORE_MAX_BYTES = 1_048_576  # 1 MiB on disk


def _decision_id(record: Any) -> str | None:
    if isinstance(record, dict):
        value = record.get("decision_id")
        return str(value) if value else None
    return None


def _valid_record(record: Any) -> bool:
    """A persisted move record carries at least a ``decision_id`` and
    the §17.3 traceability fields. Anything else is corruption, never
    a crash."""
    if not isinstance(record, dict):
        return False
    if not _decision_id(record):
        return False
    return isinstance(record.get("rule_id"), str) and isinstance(record.get("ticket"), int)


class PolicyMoveStore:
    """Bounded JSONL store of executed/failed policy moves."""

    def __init__(
        self,
        path: Path,
        *,
        max_items: int = POLICY_MOVE_STORE_MAX_ITEMS,
        max_bytes: int = POLICY_MOVE_STORE_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def load(self) -> list[dict[str, Any]]:
        """All persisted move records, oldest first. Missing file → [].
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
            logger.warning("[POLICY] cannot read durable policy-move store {}: {}", self.path, exc)
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                logger.warning("[POLICY] skipping corrupt line in durable policy-move store {}", self.path)
                continue
            if _valid_record(record):
                records.append(record)
            else:
                logger.warning(
                    "[POLICY] skipping malformed record in durable policy-move store {}",
                    self.path,
                )
        return records

    def append(self, record: dict[str, Any]) -> bool:
        """Persist one move ``record``. Returns False when it was
        dropped (invalid shape). Evicts the oldest persisted record
        WITH a warning when the bound is hit. A record with a
        ``decision_id`` already stored is replaced in place (an
        in-flight move completing updates its own row, never
        duplicates it)."""
        if not _valid_record(record):
            logger.warning("[POLICY] refusing to persist a move record with no decision_id")
            return False
        with self._lock:
            records = self._load_locked()
            records = [r for r in records if _decision_id(r) != _decision_id(record)]
            if len(records) >= self.max_items:
                evicted = records.pop(0)
                logger.warning(
                    "[POLICY] durable policy-move store full (max {}): evicted oldest "
                    "move decision_id={} — it was already delivered via diagnostics, "
                    "never assumed lost",
                    self.max_items,
                    _decision_id(evicted),
                )
            records.append(record)
            self._rewrite_locked(records)
            return True

    def list_moves(
        self,
        *,
        limit: int = 50,
        ticket: int | None = None,
        kind: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Persisted moves, NEWEST first, filtered + bounded. The
        local API (`GET /api/v1/policy/moves`) reads through this —
        the newest move is what an operator reconciling a stop wants
        first."""
        records = self.load()
        if ticket is not None:
            records = [r for r in records if r.get("ticket") == ticket]
        if kind is not None:
            records = [r for r in records if r.get("kind") == kind]
        if status is not None:
            records = [r for r in records if r.get("status") == status]
        records.reverse()
        return records[: max(0, limit)]

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
                "[POLICY] durable policy-move store over {} bytes: evicted oldest decision_id={}",
                self.max_bytes,
                _decision_id(dropped),
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
