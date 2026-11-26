"""Sync engine (CONTRACT v1 §3.3) — the ONLY sync channel.

Wire shape (flat struct — NOT the full ``MessageEnvelope``, delta
D-SYNC-1)::

    {
      "batch_id": "<uuidv7, the idempotency key>",
      "kind": "account | positions | history_orders | history_positions",
      "items": { ... raw MT5 JSON, broker timestamps verbatim ... },
      "count": 42,
      "produced_at": "<RFC3339 UTC>"
    }

Rules implemented here:

- Kinds are exactly the four above; no per-batch ``account_id`` on
  the wire (the server attributes by device binding, §7.2).
- Broker-time rule (§3.3): timestamps inside payloads are the broker's
  verbatim strings; the Platform stores them as opaque broker time
  plus the connector-reported UTC receipt time (``produced_at``). This
  engine never re-localizes broker timestamps.
- Checkpoint (§3.3): the MAXIMUM broker time seen, never moving
  backwards; the next history query range is
  ``[checkpoint | "1970-01-01T00:00:00Z", now]``; page size 500;
  250 ms pause between pages. A fresh device starts from epoch (full
  backfill; server dedup makes it safe).
- Pending-queue bound (§3.3): at most 32 unacknowledged HISTORY
  batches; the oldest is evicted WITH a warning log when full — never
  silently discarded. Snapshot kinds (``account`` / ``positions``)
  keep only the LATEST unsent batch each: a newer snapshot supersedes
  the older one, so the queue can never grow from the 1 s / 2 s
  pollers. An evicted history batch is re-created from the checkpoint
  on the next cycle.
- DROP POLICY (B4, stated once here — the queue, the checkpoint and
  the dead-letter list implement it together, in this order):
  1. Snapshots first: a newer ``account`` / ``positions`` batch
     supersedes the older unsent one. The dropped snapshot is stale
     by definition — the next poller cycle builds a fresher one — so
     dropping it loses nothing.
  2. History next, oldest first: when the 32-batch history queue is
     full the OLDEST batch is evicted. It is the safest to drop
     because the checkpoint has NOT advanced past it (the checkpoint
     only moves on a server ack), so the next ``sync_history`` cycle
     re-fetches exactly that range and re-creates the batch.
  3. Dead-letter last, never silently: a batch that exhausts its
     bounded retries (or fails non-retryably) leaves the queue for
     the dead-letter list — recorded with its attempt count and error
     code, surfaced as an ERROR telemetry event, and kept (bounded
     32, oldest dead-letter entry discarded with a warning) until the
     operator / next cycle deals with it. A dead-lettered HISTORY
     batch is re-created on the next ``sync_history`` from the
     checkpoint (new batch id, fresh attempts); a dead-lettered
     snapshot is superseded by the next poller snapshot. Nothing is
     ever dropped without a log line and a recovery path.
- Retries (B4): a batch is retried at most ``SYNC_MAX_ATTEMPTS``
  flush-level attempts (the REST client already retries each send
  internally, bounded, with backoff — see ``server/rest.py``), with
  the flush loop backing off exponentially between attempts
  (``SYNC_FLUSH_BASE_INTERVAL_S`` doubling to
  ``SYNC_FLUSH_BACKOFF_CAP_S``). Non-retryable failures (server
  rejection ``accepted: false``, auth errors) dead-letter on the
  FIRST failure — retrying them can never succeed. Retries reuse
  the SAME ``batch_id`` (the idempotency key): if the server stored
  the batch but the ack was lost, the retry is deduped server-side
  instead of double-applied. There is no infinite silent retry: a
  batch either gets accepted, or lands in the dead-letter list with
  an ERROR log + telemetry event.
- Resume without duplication (B4): on reconnect / restart the engine
  restores the checkpoint from the durable store and re-fetches from
  it — the checkpoint only ever advances on a server ack, so the
  re-fetched range is exactly the undelivered range; replays reuse
  the same batch id while queued and get a fresh id only when
  re-created from the checkpoint after dead-lettering. Enqueueing a
  batch id that is already pending is a no-op (returns the queued
  batch) — a replay can never double-queue the same batch.
- Ack 200 ``{accepted, batch_id}``; ``accepted: false`` carries an
  error (§3.6). Unacked batches stay queued; recovery after downtime
  uses the checkpoint + history range queries.
- History dedup (§3.3): history records are deduped client-side by
  ``order_id`` / ``deal_id``; records without a ticket id are DROPPED
  client-side (they cannot be reconciled server-side).
- Account switch (§7.3): on switch, pending old-account batches are
  discarded (they must never be sent under the new account), the
  checkpoint resets to epoch (fresh backfill for the new account),
  and the caller re-sends ``account`` then ``positions`` then history.
- Journal + accounting labelling (spec §§8-9, Phase C3): when a
  ``JournalCollector`` is attached, the records this engine already
  fetches feed it (history records in ``sync_history``, live
  snapshots in ``send_positions``) — history is never fetched twice
  for the journal, and journaling never breaks sync. The ``account``
  batch carries a ``provenance`` map and every history page carries
  ``provenance`` (+ ``balance_operations`` on deals pages); see
  ``docs/JOURNAL.md``. There is no journal upload channel in
  CONTRACT v1 (dependency D6) and no new sync kind (D8).

Snapshot providers are injectable callables so tests can drive the
engine without MT5; the defaults read the live terminal through the
existing local commands (no driver rewrite).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..journal.accounting import label_account_snapshot, label_closed_position, surface_balance_operations
from ..journal.collector import JournalCollector
from ..timeutil import normalize_utc
from .errors import ServerError, ServerErrorCode
from .queue import BoundedPendingQueue
from .rest import RestClient
from .sync_checkpoint import SyncCheckpointStore
from .uuid7 import new_uuid7_str

if TYPE_CHECKING:
    from ..engine.common import BridgeContext

__all__ = [
    "BATCH_KINDS",
    "EPOCH_ISO",
    "SYNC_DEAD_LETTER_MAX_ITEMS",
    "SYNC_FLUSH_BACKOFF_CAP_S",
    "SYNC_FLUSH_BASE_INTERVAL_S",
    "SYNC_MAX_ATTEMPTS",
    "SyncBatch",
    "SyncCheckpoint",
    "SyncEngine",
    "build_batch",
    "chunk_items",
    "count_items",
    "parse_broker_time",
]

BATCH_KINDS = ("account", "positions", "history_orders", "history_positions")
EPOCH_ISO = "1970-01-01T00:00:00Z"
PAGE_SIZE = 500
PAGE_PAUSE_S = 0.25
MAX_PENDING_BATCHES = 32

#: Bounded retries (B4): a batch gets at most this many flush-level
#: send attempts before it dead-letters. Each send already retries
#: internally in the REST client (bounded, with backoff), so this is
#: the outer bound — the total worst case per batch is
#: SYNC_MAX_ATTEMPTS * (RestClient.max_retries + 1) sends, after
#: which the batch is terminal, never retried silently forever.
SYNC_MAX_ATTEMPTS = 5
#: Dead-letter list bound: oldest dead-letter entry is discarded
#: WITH a warning when full (same discipline as every other queue).
SYNC_DEAD_LETTER_MAX_ITEMS = 32
#: Flush-loop cadence: base interval, doubling after each consecutive
#: failed flush up to the cap (never a tight retry loop into a dead
#: server — the 2026-11-26 live session queued 2 batches for 10+ min
#: with no backoff story at all).
SYNC_FLUSH_BASE_INTERVAL_S = 5.0
SYNC_FLUSH_BACKOFF_CAP_S = 60.0

#: Sync retry classification (B4, CONTRACT §3.6): the terminal codes
#: are the ones that can never succeed on retry — fatal auth
#: (``ServerError.is_fatal_auth``: key invalid/revoked, subscription
#: expired, device limit, account not authorized, feature not
#: enabled) and ``malformed_response`` (the server rejected the batch
#: with ``accepted: false``). Everything else — 429 rate-limited,
#: 5xx server-unavailable, transport errors — is retryable with
#: backoff. Classification is by CODE, not by the ``retryable`` flag
#: on the exception: a 5xx raised without the flag set (a test fake,
#: a future caller) is still retryable, and a server rejection is
#: still terminal even if some caller marked it retryable.
_TERMINAL_SYNC_CODES = frozenset({ServerErrorCode.MALFORMED_RESPONSE})


def _is_terminal_sync_error(exc: ServerError) -> bool:
    """True when retrying this batch can never succeed (§3.6)."""
    return exc.is_fatal_auth or exc.code in _TERMINAL_SYNC_CODES


#: Item keys scanned (recursively) for broker times when advancing the
#: checkpoint. A value that does not parse orders nothing (§3.3).
_BROKER_TIME_KEYS = (
    "time",
    "time_setup",
    "time_done",
    "time_utc",
    "time_setup_utc",
    "open_time",
    "close_time",
)


def parse_broker_time(value: Any, offset_minutes: int = 0) -> datetime | None:
    """Parse a broker timestamp per the §3.3 rule.

    RFC 3339 or timezone-less ISO; timezone-less values are read as
    UTC (the connector reports UTC receipt time separately). Epoch
    numbers are broker-local seconds and MUST be shifted back by
    ``offset_minutes`` (R1: reading them as UTC puts the checkpoint
    ``offset`` ahead of true UTC for any positive-offset broker,
    inverting the next history range and stalling history sync
    permanently — this matches ``normalize_utc``/``broker_epoch_to_utc``
    in ``timeutil``). Unparseable values return None and never order
    anything.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            broker_local = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return broker_local - timedelta(minutes=offset_minutes)
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return normalize_utc(text, 0)
    return None


def count_items(kind: str, items: dict[str, Any]) -> int:
    """Record count for a batch.

    The sum of the record lists when any is present (a history_orders
    batch carries both ``orders`` and ``deals``); 1 for the single
    account snapshot, which carries no record list.
    """
    if not isinstance(items, dict):
        return 0
    lists = [items[key] for key in ("positions", "orders", "deals") if isinstance(items.get(key), list)]
    if lists:
        return sum(len(value) for value in lists)
    return 1  # e.g. the single account snapshot


def build_batch(kind: str, items: dict[str, Any], *, batch_id: str | None = None) -> dict[str, Any]:
    """Build one flat sync batch (§3.3 wire shape)."""
    if kind not in BATCH_KINDS:
        raise ValueError(f"unknown sync batch kind {kind!r}; kinds are exactly {BATCH_KINDS}")
    return {
        "batch_id": batch_id or new_uuid7_str(),
        "kind": kind,
        "items": items,
        "count": count_items(kind, items),
        "produced_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def chunk_items(items: list[Any], page_size: int = PAGE_SIZE) -> list[list[Any]]:
    """Split a record list into pages of at most ``page_size`` (§3.3)."""
    return [items[i : i + page_size] for i in range(0, len(items), page_size)]


@dataclass
class SyncBatch:
    kind: str
    items: dict[str, Any]
    batch: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.batch = build_batch(self.kind, self.items)


class SyncCheckpoint:
    """History checkpoint: max broker time seen, never backwards (§3.3)."""

    def __init__(self, start: str = EPOCH_ISO) -> None:
        parsed = parse_broker_time(start) or datetime(1970, 1, 1, tzinfo=UTC)
        self._value = parsed

    @property
    def value(self) -> datetime:
        return self._value

    @property
    def iso(self) -> str:
        return self._value.isoformat().replace("+00:00", "Z")

    def advance(self, candidate: Any) -> bool:
        """Move the checkpoint to ``candidate`` if it is later.

        Returns True when the checkpoint moved. Unparseable candidates
        and backwards candidates never move it.
        """
        parsed = parse_broker_time(candidate)
        if parsed is None or parsed <= self._value:
            return False
        self._value = parsed
        return True

    def advance_from_items(self, items: Any, offset_minutes: int = 0) -> int:
        """Advance from every broker-time value found in ``items``.

        ``offset_minutes`` is the broker UTC offset applied to
        broker-local epoch values (R1); ISO strings are UTC by
        convention and are never shifted. Returns how many distinct
        later times were applied (0 or 1 in practice — the checkpoint
        only ever moves to the max).
        """
        latest: datetime | None = None
        stack = [items]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in _BROKER_TIME_KEYS:
                        parsed = parse_broker_time(value, offset_minutes)
                        if parsed is not None and (latest is None or parsed > latest):
                            latest = parsed
                    else:
                        stack.append(value)
            elif isinstance(node, list):
                stack.extend(node)
        if latest is None:
            return 0
        return 1 if self.advance(latest) else 0

    def reset(self) -> None:
        """Reset to epoch (fresh backfill — used on account switch, §7.3)."""
        self._value = datetime(1970, 1, 1, tzinfo=UTC)


class SyncEngine:
    """Builds, queues and flushes sync batches over the REST client.

    Snapshot providers are ``async`` callables returning the ``items``
    dict for a kind; the defaults read the live terminal. ``sleep`` is
    injectable so tests can observe the 250 ms page pause.

    Checkpoint persistence (§3.3, spec §16): when ``checkpoint_store``
    is given, the checkpoint is restored from it at construction and
    persisted every time it advances, so a Bridge restart resumes
    history from the last synced broker time instead of re-running a
    full backfill. A corrupt or foreign-account record is ignored
    (epoch backfill) — startup never crashes on a damaged store.
    """

    def __init__(
        self,
        rest: RestClient,
        ctx: BridgeContext | None = None,
        *,
        page_size: int = PAGE_SIZE,
        page_pause_s: float = PAGE_PAUSE_S,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        checkpoint: SyncCheckpoint | None = None,
        checkpoint_store: SyncCheckpointStore | None = None,
        account_snapshot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        positions_snapshot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        history_orders: Callable[[datetime, datetime], Awaitable[dict[str, Any]]] | None = None,
        history_positions: Callable[[datetime, datetime], Awaitable[dict[str, Any]]] | None = None,
        journal: JournalCollector | None = None,
    ) -> None:
        self.rest = rest
        self.ctx = ctx
        self.page_size = page_size
        self.page_pause_s = page_pause_s
        self._sleep = sleep or asyncio.sleep
        self.checkpoint_store = checkpoint_store
        self.checkpoint = checkpoint or self._restore_checkpoint()
        self._pending_history: BoundedPendingQueue[dict[str, Any]] = BoundedPendingQueue(
            MAX_PENDING_BATCHES, "sync/history"
        )
        # Latest unsent snapshot per kind (account / positions only).
        self._pending_snapshots: dict[str, dict[str, Any]] = {}
        # B4 retry/dead-letter state. ``_attempts`` counts flush-level
        # send attempts per batch id (reset on ack, on dead-letter, on
        # supersede, on account switch). ``_dead_letter`` is the
        # terminal state: entries are {"batch", "attempts",
        # "error_code"} — a batch lands here only after exhausting its
        # retries or failing non-retryably, and is never silently
        # retried again. ``_accepted_batch_ids`` records every batch
        # id the server acked in this process — sync_history advances
        # the checkpoint ONLY for ids in this set (a batch that merely
        # left the queue — evicted, dead-lettered — must never advance
        # it; the pre-B4 "not pending means accepted" check did exactly
        # that on the dead-letter path).
        self._attempts: dict[str, int] = {}
        self._dead_letter: list[dict[str, Any]] = []
        self._accepted_batch_ids: set[str] = set()
        self._consecutive_flush_failures = 0
        self._flush_lock = asyncio.Lock()
        self._flush_stop = asyncio.Event()
        self._flush_task: asyncio.Task | None = None
        self._account_snapshot = account_snapshot or self._default_account_snapshot
        self._positions_snapshot = positions_snapshot or self._default_positions_snapshot
        self._history_orders = history_orders or self._default_history_orders
        self._history_positions = history_positions or self._default_history_positions
        # Journal collector (spec §8, Phase C3): fed from the records
        # this engine already fetches — history is never fetched twice
        # for the journal. None means journaling is off (tests, or a
        # build without the collector wired in). Events live in the
        # collector's bounded store; there is no journal upload channel
        # in CONTRACT v1 (dependency D6).
        self.journal = journal
        self._last_positions: list[dict[str, Any]] | None = None
        self._last_orders: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # checkpoint persistence (spec §16)
    # ------------------------------------------------------------------
    def _bound_account_id(self) -> str | None:
        """MT5 login currently bound to this device, if any (§7.2)."""
        if self.ctx is None:
            return None
        try:
            login = self.ctx.driver.connection_state_dict().get("login")
        except Exception:  # noqa: BLE001 — a driver error must not break sync
            return None
        return str(login) if login is not None else None

    def _restore_checkpoint(self) -> SyncCheckpoint:
        """Restore the checkpoint from the store, or start at epoch.

        A stored checkpoint is honoured only when it belongs to the
        currently bound account (or was recorded without an account);
        a record from another account is ignored so the new account
        gets its full backfill (§7.3). A missing/corrupt store means
        epoch — the store already logged why.
        """
        if self.checkpoint_store is None:
            return SyncCheckpoint()
        record = self.checkpoint_store.load()
        if record is None:
            return SyncCheckpoint()
        stored_account = record.get("account_id")
        bound = self._bound_account_id()
        if stored_account is not None and bound is not None and stored_account != bound:
            logger.warning(
                "[SYNC] stored checkpoint belongs to account {} but this device "
                "is bound to {} — ignoring it, full backfill (§7.3)",
                stored_account,
                bound,
            )
            return SyncCheckpoint()
        restored = SyncCheckpoint(start=str(record["checkpoint"]))
        logger.info(
            "[SYNC] restored sync checkpoint {} (account {}) — resuming, no full backfill",
            restored.iso,
            stored_account or bound or "unknown",
        )
        return restored

    def _persist_checkpoint(self) -> None:
        """Persist the current checkpoint (best-effort, never raises)."""
        if self.checkpoint_store is None:
            return
        try:
            self.checkpoint_store.save(self.checkpoint.iso, account_id=self._bound_account_id())
        except Exception as exc:  # noqa: BLE001 — persistence must never break sync
            logger.warning("[SYNC] could not persist sync checkpoint: {}", type(exc).__name__)

    # ------------------------------------------------------------------
    # queueing
    # ------------------------------------------------------------------
    def enqueue(self, kind: str, items: dict[str, Any], *, batch_id: str | None = None) -> dict[str, Any]:
        """Queue a batch. Snapshot kinds keep only the latest batch;
        history kinds go to the bounded 32-queue (oldest evicted with
        a warning, §3.3). Returns the built batch.

        Replay dedup (B4): when ``batch_id`` is given and a batch
        with that id is already pending, the queued batch is returned
        unchanged — a replay after reconnect can never double-queue
        the same batch. A superseded snapshot drops its attempt count
        (the new snapshot starts fresh).
        """
        if batch_id is not None:
            for pending in self._pending_snapshots.values():
                if pending.get("batch_id") == batch_id:
                    logger.debug("[SYNC] replay dedup: batch {} already queued, not re-queued", batch_id)
                    return pending
            for pending in self._pending_history.snapshot():
                if pending.get("batch_id") == batch_id:
                    logger.debug("[SYNC] replay dedup: batch {} already queued, not re-queued", batch_id)
                    return pending
        batch = build_batch(kind, items, batch_id=batch_id)
        if kind in ("account", "positions"):
            previous = self._pending_snapshots.get(kind)
            if previous is not None:
                self._attempts.pop(previous.get("batch_id", ""), None)
            self._pending_snapshots[kind] = batch
        else:
            self._pending_history.put(batch, item_id=batch["batch_id"])
        return batch

    def pending_count(self) -> int:
        return len(self._pending_snapshots) + len(self._pending_history)

    def dead_letter_count(self) -> int:
        """How many batches reached the terminal dead-letter state."""
        return len(self._dead_letter)

    def dead_letter_snapshot(self) -> list[dict[str, Any]]:
        """Dead-letter entries (oldest first): each is ``{"batch",
        "attempts", "error_code"}``. The batch dicts are the queued
        copies — callers must not mutate them."""
        return [dict(entry) for entry in self._dead_letter]

    def attempts_for(self, batch_id: str) -> int:
        """Flush-level send attempts recorded for ``batch_id`` (0 when
        never attempted / already acked / dead-lettered)."""
        return self._attempts.get(batch_id, 0)

    def _dead_letter_batch(self, batch: dict[str, Any], error_code: str) -> None:
        """Move ``batch`` to the terminal dead-letter state (B4).

        Bounded (32, oldest entry discarded with a warning), logged
        as an ERROR, and reported as ERROR telemetry — a dead-lettered
        batch is never retried silently. Recovery: a history batch is
        re-created on the next ``sync_history`` from the checkpoint
        (the checkpoint never advanced past it); a snapshot batch is
        superseded by the next poller snapshot.
        """
        batch_id = str(batch.get("batch_id", "?"))
        attempts = self._attempts.pop(batch_id, 0)
        if len(self._dead_letter) >= SYNC_DEAD_LETTER_MAX_ITEMS:
            dropped = self._dead_letter.pop(0)
            logger.warning(
                "[SYNC] dead-letter list full (max {}): discarded oldest dead-lettered batch {}",
                SYNC_DEAD_LETTER_MAX_ITEMS,
                dropped["batch"].get("batch_id"),
            )
        self._dead_letter.append({"batch": batch, "attempts": attempts, "error_code": error_code})
        logger.error(
            "[SYNC] batch {} (kind {}) dead-lettered after {} attempt(s): {} — "
            "it will NOT be retried; history is re-created from the checkpoint on the next sync",
            batch_id,
            batch.get("kind"),
            attempts,
            error_code,
        )
        telemetry = getattr(self.ctx, "telemetry", None) if self.ctx is not None else None
        if telemetry is not None:
            telemetry.error(
                "sync.batch_dead_letter",
                component="server.sync",
                error_code=error_code,
                details={"batch_id": batch_id, "kind": batch.get("kind"), "attempts": attempts},
            )

    @staticmethod
    def flush_delay_s(consecutive_failures: int, *, base_s: float = SYNC_FLUSH_BASE_INTERVAL_S) -> float:
        """Flush-loop delay after ``consecutive_failures`` failed
        flushes: base interval doubling per failure, capped — so a
        dead server is not hammered every 5 s forever, and a
        recovered server is retried within a minute at most."""
        if consecutive_failures <= 0:
            return base_s
        return min(base_s * (2**consecutive_failures), SYNC_FLUSH_BACKOFF_CAP_S)

    def _is_batch_pending(self, batch_id: str) -> bool:
        """True when a batch is still queued (not yet accepted by the server)."""
        for batch in self._pending_snapshots.values():
            if batch.get("batch_id") == batch_id:
                return True
        return any(b.get("batch_id") == batch_id for b in self._pending_history.snapshot())

    # ------------------------------------------------------------------
    # flushing
    # ------------------------------------------------------------------
    async def flush(self) -> tuple[int, int]:
        """Send queued batches in order: account, positions, history.

        Stops at the first failure, keeping the rest queued. Returns
        ``(sent, failed)``. A 401/403 stops sync immediately (§3.1) —
        the REST client already refuses to retry those with the same
        key; the caller surfaces the distinct user-facing state. When
        enrolment is FAILED_AUTH (terminal for this key) nothing is
        sent at all — batches stay queued, unacked, for a future key.

        Bounded retries (B4): every failed send counts one attempt
        against that batch id. A retryable failure (429 / 5xx /
        transport) is re-queued with the SAME batch id (idempotency
        key — a lost ack never double-applies server-side) until
        ``SYNC_MAX_ATTEMPTS``; then the batch dead-letters. A
        non-retryable failure (server rejection ``accepted: false``,
        auth errors) dead-letters on the FIRST failure — retrying it
        can never succeed. An unexpected (non-ServerError) send
        failure is treated as retryable, and the unsent batches are
        re-queued before it propagates — a popped batch is never lost
        to an exception. ``CancelledError`` re-queues then re-raises.
        """
        enrolment = getattr(self.ctx, "enrolment", None) if self.ctx is not None else None
        if enrolment is not None and enrolment.to_dict().get("state") == "failed_auth":
            logger.debug(
                "[SYNC] enrolment failed_auth — sync stopped, keeping {} batches queued (§3.1)",
                self.pending_count(),
            )
            return 0, 0
        sent = failed = 0
        async with self._flush_lock:
            batches: list[dict[str, Any]] = []
            for kind in ("account", "positions"):
                batch = self._pending_snapshots.pop(kind, None)
                if batch is not None:
                    batches.append(batch)
            while True:
                batch = self._pending_history.pop_oldest()
                if batch is None:
                    break
                batches.append(batch)
            for index, batch in enumerate(batches):
                batch_id = str(batch.get("batch_id", "?"))
                try:
                    ack = await self.rest.send_batch(batch)
                except asyncio.CancelledError:
                    # Outcome unknown (may have reached the server) —
                    # re-queue with the SAME batch id and let the
                    # caller decide; never swallow a cancellation.
                    self._requeue_in_order(batches[index:])
                    raise
                except ServerError as exc:
                    failed += 1
                    self._attempts[batch_id] = self._attempts.get(batch_id, 0) + 1
                    attempts = self._attempts[batch_id]
                    # Terminal by CODE (§3.6 — see _is_terminal_sync_error),
                    # never by the exception's retryable flag.
                    dead_lettered = _is_terminal_sync_error(exc) or attempts >= SYNC_MAX_ATTEMPTS
                    if dead_lettered:
                        logger.warning(
                            "[SYNC] batch {} (kind {}) failed non-retryably ({}) or exhausted "
                            "its {} attempts — dead-lettering, keeping {} queued",
                            batch_id,
                            batch["kind"],
                            exc.code.value,
                            SYNC_MAX_ATTEMPTS,
                            self.pending_count(),
                        )
                        self._dead_letter_batch(batch, exc.code.value)
                    else:
                        logger.warning(
                            "[SYNC] batch {} (kind {}) failed: {} (attempt {}/{}) — keeping {} queued",
                            batch_id,
                            batch["kind"],
                            exc.code.value,
                            attempts,
                            SYNC_MAX_ATTEMPTS,
                            self.pending_count(),
                        )
                        telemetry = getattr(self.ctx, "telemetry", None) if self.ctx is not None else None
                        if telemetry is not None:
                            telemetry.warn(
                                "sync.batch_failed",
                                component="server.sync",
                                error_code=exc.code.value,
                                details={
                                    "batch_id": batch_id,
                                    "kind": batch["kind"],
                                    "attempts": attempts,
                                    "pending": self.pending_count(),
                                },
                            )
                    # Re-queue every batch AFTER the failed one, in
                    # original order (they were already popped). The
                    # failed batch itself is re-queued too, unless it
                    # just dead-lettered.
                    rest = batches[index + 1 :]
                    if dead_lettered:
                        self._requeue_in_order(rest)
                    else:
                        self._requeue_in_order([batch] + rest)
                    break
                except Exception as exc:  # noqa: BLE001 — an unexpected send failure must not lose popped batches
                    failed += 1
                    self._attempts[batch_id] = self._attempts.get(batch_id, 0) + 1
                    attempts = self._attempts[batch_id]
                    logger.warning(
                        "[SYNC] batch {} (kind {}) failed unexpectedly: {} (attempt {}/{}) — keeping {} queued",
                        batch_id,
                        batch["kind"],
                        type(exc).__name__,
                        attempts,
                        SYNC_MAX_ATTEMPTS,
                        self.pending_count(),
                    )
                    if attempts >= SYNC_MAX_ATTEMPTS:
                        self._dead_letter_batch(batch, type(exc).__name__)
                        self._requeue_in_order(batches[index + 1 :])
                    else:
                        self._requeue_in_order(batches[index:])
                    break
                if ack.batch_id and ack.batch_id != batch["batch_id"]:
                    logger.warning(
                        "[SYNC] ack batch_id {} does not match sent {}",
                        ack.batch_id,
                        batch["batch_id"],
                    )
                self._attempts.pop(batch_id, None)
                self._accepted_batch_ids.add(batch_id)
                sent += 1
        if failed:
            self._consecutive_flush_failures += 1
        else:
            self._consecutive_flush_failures = 0
        return sent, failed

    def _requeue_in_order(self, batches: list[dict[str, Any]]) -> None:
        """Put unsent batches back in original order.

        Snapshot kinds go back to the latest slot; history kinds go
        back to the FRONT of the bounded queue, in order. A batch
        already queued (replayed concurrently) is skipped, never
        duplicated.
        """
        history = [b for b in batches if b["kind"] not in ("account", "positions")]
        for batch in batches:
            if batch["kind"] in ("account", "positions"):
                self._pending_snapshots.setdefault(batch["kind"], batch)
        if history:
            remaining = self._pending_history.clear()
            for batch in history + remaining:
                if self._is_batch_pending(str(batch.get("batch_id", ""))):
                    continue
                self._pending_history.put(batch, item_id=batch.get("batch_id"))

    async def flush_loop(self, interval_s: float = 5.0) -> None:
        """Background flusher (started by the app lifespan).

        After consecutive failed flushes the wait backs off
        exponentially (``flush_delay_s``: 5 s → 10 s → 20 s → 40 s,
        capped at 60 s) instead of hammering a dead server every 5 s
        forever; the first successful flush resets the delay to the
        base interval.
        """
        self._flush_stop.clear()
        while not self._flush_stop.is_set():
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 — flusher must not die
                self._consecutive_flush_failures += 1
                logger.warning("[SYNC] flush loop error: {}", type(exc).__name__)
            try:
                delay = self.flush_delay_s(self._consecutive_flush_failures, base_s=interval_s)
                await asyncio.wait_for(self._flush_stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    def start_flush_loop(self, interval_s: float = 5.0) -> asyncio.Task:
        self._flush_task = asyncio.create_task(self.flush_loop(interval_s), name="nse-sync-flush")
        return self._flush_task

    async def stop_flush_loop(self) -> None:
        self._flush_stop.set()
        if self._flush_task is not None:
            try:
                await asyncio.wait_for(self._flush_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._flush_task.cancel()
            self._flush_task = None

    # ------------------------------------------------------------------
    # snapshot sends
    # ------------------------------------------------------------------
    async def send_account(self) -> dict[str, Any]:
        """Build the authoritative account-identity batch (§7.2) and flush it."""
        items = await self._account_snapshot()
        batch = self.enqueue("account", items)
        await self.flush()
        return batch

    async def send_positions(self) -> dict[str, Any]:
        items = await self._positions_snapshot()
        batch = self.enqueue("positions", items)
        self._journal_diff_positions(items)
        await self.flush()
        return batch

    def _journal_diff_positions(self, items: dict[str, Any]) -> None:
        """Feed the live positions/orders snapshot to the journal.

        The first snapshot is the baseline (no events); later ones
        emit opened/modified/pending events from real state deltas.
        Journaling must never break sync, so failures are logged only.
        """
        if self.journal is None:
            return
        try:
            positions = list(items.get("positions", []))
            orders = list(items.get("orders", []))
            self.journal.diff_snapshots(self._last_positions, self._last_orders, positions, orders)
            self._last_positions = positions
            self._last_orders = orders
        except Exception as exc:  # noqa: BLE001 — journaling must never break sync
            logger.warning("[SYNC] journal snapshot diff failed: {}", type(exc).__name__)

    async def handle_sync_request(self) -> None:
        """§5.2 / §7.3 — server asks for an out-of-band refresh.

        Re-send an ``account`` batch and then ``positions`` (this is
        also the account-switch trigger path), then history from the
        checkpoint. Fresh sync, never merged across accounts.
        """
        await self.send_account()
        await self.send_positions()
        await self.sync_history()

    async def sync_history(self) -> dict[str, int]:
        """History backfill from the checkpoint to now (§3.3).

        Queries ``[checkpoint, now]``, chunks each kind into 500-item
        pages with a 250 ms pause between pages, and advances the
        checkpoint to the max broker time seen (never backwards).
        Returns ``{"history_orders": n, "history_positions": m}`` page counts.
        """
        from_dt = self.checkpoint.value
        to_dt = datetime.now(UTC)
        sent: dict[str, int] = {"history_orders": 0, "history_positions": 0}
        for kind, fetch in (
            ("history_orders", self._history_orders),
            ("history_positions", self._history_positions),
        ):
            try:
                items = await fetch(from_dt, to_dt)
            except Exception as exc:  # noqa: BLE001 — one kind must not kill the other
                logger.warning("[SYNC] history fetch {} failed: {}", kind, type(exc).__name__)
                continue
            self._journal_collect_history(kind, items, from_dt, to_dt)
            records = _primary_records(kind, items)
            pages = _history_pages(kind, items, self.page_size)
            for page_index, page_items in enumerate(pages):
                batch = self.enqueue(kind, page_items)
                await self.flush()
                # Advance ONLY once the batch is accepted (Codex P1 — a
                # second history-loss mechanism, distinct from R1): the
                # checkpoint used to advance on ENQUEUE, before the batch
                # was accepted. If the process restarted after the
                # checkpoint persisted but before the (in-memory
                # re-queued) batch was flushed, that range was never
                # re-fetched — silent history loss. B4 tightens the
                # check itself: "not pending" is NOT proof of delivery
                # (a dead-lettered or queue-evicted batch is also not
                # pending), so the checkpoint advances only for batch
                # ids the flush actually acked (``_accepted_batch_ids``).
                # A still-pending batch is retried and the checkpoint
                # stays behind, so a restart re-fetches it; a
                # dead-lettered batch is re-created from the checkpoint
                # on the next cycle with a fresh batch id.
                if batch["batch_id"] in self._accepted_batch_ids:
                    # R1: broker-local epochs shift by the configured
                    # offset; without a ctx there is no configured
                    # offset (0 = UTC).
                    offset = self.ctx.settings.broker_utc_offset_minutes if self.ctx is not None else 0
                    if self.checkpoint.advance_from_items(page_items, offset):
                        # Persist on every advance: a restart resumes from
                        # here instead of re-running the full backfill (§16).
                        self._persist_checkpoint()
                sent[kind] += 1
                if page_index < len(pages) - 1:
                    await self._sleep(self.page_pause_s)
            logger.info("[SYNC] {} sent {} page(s) ({} records)", kind, len(pages), len(records))
        return sent

    def _journal_collect_history(self, kind: str, items: dict[str, Any], from_dt: datetime, to_dt: datetime) -> None:
        """Feed just-fetched history records to the journal collector.

        The collector builds events only from these MT5 records, with
        the same bounded range the sync used. Journaling must never
        break sync, so failures are logged only.
        """
        if self.journal is None:
            return
        try:
            if kind == "history_orders":
                self.journal.collect_from_history(
                    orders=list(items.get("orders", [])),
                    deals=list(items.get("deals", [])),
                    from_dt=from_dt,
                    to_dt=to_dt,
                )
            else:
                self.journal.collect_from_history(
                    closed_positions=list(items.get("positions", [])),
                    from_dt=from_dt,
                    to_dt=to_dt,
                )
        except Exception as exc:  # noqa: BLE001 — journaling must never break sync
            logger.warning("[SYNC] journal history collection failed: {}", type(exc).__name__)

    def journal_events(self) -> list[dict[str, Any]]:
        """Current journal events as stable dicts (Phase C4 GUI input)."""
        if self.journal is None:
            return []
        return self.journal.event_dicts()

    # ------------------------------------------------------------------
    # account switch (§7.3)
    # ------------------------------------------------------------------
    def reset_for_account_switch(self) -> None:
        """Discard pending old-account batches; reset the checkpoint.

        The old account's batches must never be sent under the new
        account, and the new account gets a fresh backfill from epoch.
        The caller then re-sends account → positions → history.
        """
        dropped = self._pending_history.clear()
        dropped_snapshots = len(self._pending_snapshots)
        self._pending_snapshots.clear()
        self._last_positions = None
        self._last_orders = None
        # B4: the old account's retry/dead-letter state must not leak
        # into the new account — its batches are gone, so their
        # attempts, dead letters and acks go with them.
        self._attempts.clear()
        self._dead_letter.clear()
        self._accepted_batch_ids.clear()
        self._consecutive_flush_failures = 0
        if self.journal is not None:
            # The new account must not inherit the old account's dedup
            # state or events (§7.3).
            self.journal.reset()
        self.checkpoint.reset()
        # Persist the reset: the new account must not inherit the old
        # account's checkpoint on the next restart (§7.3).
        self._persist_checkpoint()
        logger.warning(
            "[SYNC] account switch: discarded {} queued history batches + {} snapshots; "
            "checkpoint reset to epoch for a fresh backfill (§7.3)",
            len(dropped),
            dropped_snapshots,
        )

    # ------------------------------------------------------------------
    # default providers (live terminal, via existing local commands)
    # ------------------------------------------------------------------
    @staticmethod
    def account_items(acc: Any, state: dict[str, Any]) -> dict[str, Any]:
        """Map a local account model + driver state to §3.3 account items.

        Mapping notes: ``trade_mode`` 0/1/2 → ``demo``/``contest``/
        ``live`` (§7.1). An unreported ``trade_mode`` (None, or an
        unknown integer) OMITS ``type`` entirely (R5): labelling it
        ``demo`` would let a live account slip past the contract's
        demo-only gate, and ``"unknown"`` is outside the contract's
        ``demo|live|contest`` enum. ``broker`` is not exposed by the
        local account model — omitted, never invented; ``terminal.build``
        is a NUMBER the driver does not expose — omitted, never invented
        (§4.1); ``terminal.timestamp`` is UTC receipt time (the
        broker-time rule applies to timestamps INSIDE the payload, which
        stay verbatim broker values where present). ``margin_level`` /
        ``leverage`` / ``swap`` / ``commission`` ride the batch (spec §5)
        as raw broker values, labelled ``mt5_supplied`` by
        ``label_account_snapshot``.
        """
        raw_mode = getattr(acc, "trade_mode", None)
        account: dict[str, Any] = {
            "login": str(acc.login),
            "server": acc.server,
            "name": acc.name,
            "currency": acc.currency,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "margin_free": acc.margin_free,
            "margin_level": getattr(acc, "margin_level", None),
            "leverage": getattr(acc, "leverage", None),
            "profit": acc.profit,
            "swap": getattr(acc, "swap", None),
            "commission": getattr(acc, "commission", None),
            "credit": acc.credit,
        }
        trade_mode = {0: "demo", 1: "contest", 2: "live"}.get(raw_mode) if raw_mode is not None else None
        if trade_mode is not None:
            account["type"] = trade_mode
        terminal = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "connection": {"state": state.get("state")},
            "program_name": state.get("terminal_version"),
        }
        # Accounting provenance (spec §9, Phase C3 / D8): which values
        # the broker supplied vs the Bridge derived. The labelling
        # rides inside the existing ``account`` batch — no new sync
        # kind. ``realized_profit`` is always ``not_reported`` here;
        # it lives in deal history.
        provenance = {key: labelled.provenance for key, labelled in label_account_snapshot(account).items()}
        return {"account": account, "terminal": terminal, "provenance": provenance}

    async def _default_account_snapshot(self) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.common import account_info_model

        assert self.ctx is not None
        acc = await run_in_threadpool(account_info_model, self.ctx)
        state = self.ctx.driver.connection_state_dict()
        return self.account_items(acc, state)

    @staticmethod
    def positions_items(positions: list[dict[str, Any]], orders: list[dict[str, Any]]) -> dict[str, Any]:
        """Map local position/order models to §3.3 positions items:
        ``{positions: [...], orders: [...]}`` (open positions + pending
        orders, live field names)."""
        return {"positions": positions, "orders": orders}

    async def _default_positions_snapshot(self) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.orders import _fetch_orders
        from ..engine.trading import fetch_positions
        from ..mt5.types import build_order_model, build_position_model

        assert self.ctx is not None
        offset = self.ctx.settings.broker_utc_offset_minutes
        positions = await run_in_threadpool(fetch_positions, self.ctx)
        orders = await run_in_threadpool(_fetch_orders, self.ctx)
        return self.positions_items(
            [build_position_model(r, offset).model_dump() for r in positions],
            [build_order_model(r, offset).model_dump() for r in orders],
        )

    async def _default_history_orders(self, from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.orders import _history_deals, _history_orders
        from ..mt5.types import build_deal_model, build_history_order_model
        from ..mt5.types import f as _f

        assert self.ctx is not None
        offset = self.ctx.settings.broker_utc_offset_minutes
        ctx = self.ctx

        def job() -> dict[str, Any]:
            # Broker-time range: the MT5 API takes datetimes in the
            # broker timebase; convert from UTC with the broker offset.
            from datetime import timedelta as _td

            broker_from = from_dt + _td(minutes=offset)
            broker_to = to_dt + _td(minutes=offset)
            raw_orders = _history_orders(ctx, broker_from, broker_to, None)
            raw_deals = _history_deals(ctx, broker_from, broker_to, None)
            orders: list[dict[str, Any]] = []
            seen_orders: set[Any] = set()
            for raw in raw_orders:
                ticket = _f(raw, "ticket")
                if ticket in (None, 0, ""):  # dropped client-side (§3.3)
                    continue
                if ticket in seen_orders:  # dedup by order_id (§3.3)
                    continue
                seen_orders.add(ticket)
                orders.append(build_history_order_model(raw, offset).model_dump())
            deals: list[dict[str, Any]] = []
            seen_deals: set[Any] = set()
            for raw in raw_deals:
                deal_id = _f(raw, "ticket")
                if deal_id in (None, 0, ""):
                    continue
                if deal_id in seen_deals:
                    continue
                seen_deals.add(deal_id)
                deals.append(build_deal_model(raw, offset).model_dump())
            return {"orders": orders, "deals": deals}

        return await run_in_threadpool(job)

    async def _default_history_positions(self, from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.orders import HistoryRequest, get_trading_history_positions

        assert self.ctx is not None
        result = await run_in_threadpool(
            get_trading_history_positions,
            self.ctx,
            HistoryRequest(
                from_utc=from_dt.isoformat().replace("+00:00", "Z"),
                to_utc=to_dt.isoformat().replace("+00:00", "Z"),
            ),
        )
        return {"positions": result.get("positions", [])}


def _primary_records(kind: str, items: dict[str, Any]) -> list[Any]:
    if kind == "history_orders":
        return list(items.get("orders", [])) + list(items.get("deals", []))
    return list(items.get("positions", []))


def _history_pages(kind: str, items: dict[str, Any], page_size: int) -> list[dict[str, Any]]:
    """Chunk one kind's records into 500-item page batches (§3.3).

    A history_orders page carries either an ``orders`` chunk or a
    ``deals`` chunk (never a mixed page split by dict equality); a
    history_positions page carries a ``positions`` chunk. Every page
    also carries accounting labelling (spec §9, Phase C3 / D8): a
    ``provenance`` map and, on deals pages, ``balance_operations`` —
    the deposits/withdrawals/adjustments/fees/credits classified from
    that page's own deals (raw broker values, no sums). No new sync
    kind is invented; the labelling rides inside the existing batches.
    """
    if kind == "history_orders":
        pages: list[dict[str, Any]] = []
        for key in ("orders", "deals"):
            for chunk in chunk_items(list(items.get(key, [])), page_size):
                page: dict[str, Any] = {"orders": [], "deals": []}
                page[key] = chunk
                page["balance_operations"] = (
                    surface_balance_operations(chunk) if key == "deals" else surface_balance_operations([])
                )
                page["provenance"] = {
                    "orders": "mt5_supplied",
                    "deals": "mt5_supplied",
                    "balance_operations": "mt5_supplied",
                }
                pages.append(page)
        return pages
    return [
        {"positions": chunk, "provenance": label_closed_position()}
        for chunk in chunk_items(list(items.get("positions", [])), page_size)
    ]
