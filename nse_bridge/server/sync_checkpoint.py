"""Durable sync checkpoint (CONTRACT v1 §3.3, spec §16).

The history checkpoint — the maximum broker time seen, never moving
backwards — was in-memory only (:class:`nse_bridge.server.sync.SyncCheckpoint`):
a Bridge restart reset it to epoch (``1970-01-01T00:00:00Z``) and
re-ran a full history backfill. That is safe (the server dedups by
ticket id) but wasteful, and it contradicts spec §16 ("persist …
necessary synchronization checkpoints").

This module fixes the restart half with the same bounded-JSON
pattern as :mod:`nse_bridge.reliability.result_store` (single JSON
object, not JSONL — a checkpoint is one small record, not a queue):

- at most 4 KiB on disk (one record; anything larger is corrupt);
- writes go through a temp file + atomic replace, so a crash
  mid-write cannot corrupt the store;
- the file is created lazily on the first persisted checkpoint, never
  at construction (tests and read-only runs create no files);
- a missing file → no checkpoint (fresh backfill from epoch);
- a corrupt file (unparseable JSON, wrong shape, unparseable
  timestamp, oversize) is logged with a warning and treated as "no
  checkpoint" — startup NEVER crashes on a damaged store, it falls
  back to a full backfill;
- the record carries the bound account login (``account_id``) when
  known, so a checkpoint is only restored for the account it was
  recorded for — a checkpoint from a previous account must never
  skip the new account's history (§7.3: an account switch resets to
  epoch).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

__all__ = ["CHECKPOINT_FILENAME", "SyncCheckpointStore"]

CHECKPOINT_FILENAME = "sync_checkpoint.json"
#: One small record; anything larger cannot be a checkpoint.
CHECKPOINT_MAX_BYTES = 4_096


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())  # 3.11+ parses "Z"
    except ValueError:
        return None


class SyncCheckpointStore:
    """Bounded single-record JSON store for the sync checkpoint."""

    def __init__(self, path: Path, *, max_bytes: int = CHECKPOINT_MAX_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def load(self) -> dict[str, Any] | None:
        """The persisted record, or None when there is none / it is corrupt.

        Returns ``{"checkpoint": "<ISO>", "account_id": "<login>" | None}``.
        A corrupt record is logged with a warning and treated as absent
        — the caller falls back to a full backfill from epoch, never
        raising.
        """
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            logger.warning("[SYNC] cannot read sync checkpoint store {}: {}", self.path, exc)
            return None
        if len(raw) > self.max_bytes:
            logger.warning(
                "[SYNC] sync checkpoint store {} is {} bytes (max {}) — ignoring it, falling back to a full backfill",
                self.path,
                len(raw),
                self.max_bytes,
            )
            return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning(
                "[SYNC] corrupt sync checkpoint store {} — ignoring it, falling back to a full backfill",
                self.path,
            )
            return None
        if not isinstance(record, dict):
            logger.warning("[SYNC] sync checkpoint store {} is not an object — ignoring it", self.path)
            return None
        if _parse_iso(record.get("checkpoint")) is None:
            logger.warning(
                "[SYNC] sync checkpoint store {} has no parseable checkpoint — ignoring it",
                self.path,
            )
            return None
        account_id = record.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            logger.warning("[SYNC] sync checkpoint store {} has a non-string account_id — ignoring it", self.path)
            return None
        return {"checkpoint": str(record["checkpoint"]), "account_id": account_id}

    def save(self, checkpoint_iso: str, *, account_id: str | None = None) -> bool:
        """Persist ``checkpoint_iso`` (atomically). Returns False when
        the value was refused (unparseable timestamp — never written)."""
        if _parse_iso(checkpoint_iso) is None:
            logger.warning("[SYNC] refusing to persist an unparseable checkpoint {!r}", checkpoint_iso)
            return False
        record = {"version": 1, "checkpoint": checkpoint_iso, "account_id": account_id}
        with self._lock:
            self._rewrite_locked(record)
        return True

    def clear(self) -> None:
        """Remove the persisted checkpoint (best-effort)."""
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("[SYNC] cannot remove sync checkpoint store {}: {}", self.path, exc)

    # ------------------------------------------------------------------
    def _rewrite_locked(self, record: dict[str, Any]) -> None:
        """Write ``record`` atomically (temp file + replace)."""
        encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(encoded)
        os.replace(tmp, self.path)
