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

Snapshot providers are injectable callables so tests can drive the
engine without MT5; the defaults read the live terminal through the
existing local commands (no driver rewrite).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..timeutil import normalize_utc
from .errors import ServerError
from .queue import BoundedPendingQueue
from .rest import RestClient
from .uuid7 import new_uuid7_str

if TYPE_CHECKING:
    from ..engine.common import BridgeContext

__all__ = [
    "BATCH_KINDS",
    "EPOCH_ISO",
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


def parse_broker_time(value: Any) -> datetime | None:
    """Parse a broker timestamp per the §3.3 rule.

    RFC 3339 or timezone-less ISO; timezone-less values are read as
    UTC (the connector reports UTC receipt time separately). Epoch
    numbers are accepted defensively. Unparseable values return None
    and never order anything.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
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
    lists = [
        items[key]
        for key in ("positions", "orders", "deals")
        if isinstance(items.get(key), list)
    ]
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

    def advance_from_items(self, items: Any) -> int:
        """Advance from every broker-time value found in ``items``.

        Returns how many distinct later times were applied (0 or 1 in
        practice — the checkpoint only ever moves to the max).
        """
        latest: datetime | None = None
        stack = [items]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in _BROKER_TIME_KEYS:
                        parsed = parse_broker_time(value)
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
        account_snapshot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        positions_snapshot: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        history_orders: Callable[[datetime, datetime], Awaitable[dict[str, Any]]] | None = None,
        history_positions: Callable[[datetime, datetime], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.rest = rest
        self.ctx = ctx
        self.page_size = page_size
        self.page_pause_s = page_pause_s
        self._sleep = sleep or asyncio.sleep
        self.checkpoint = checkpoint or SyncCheckpoint()
        self._pending_history: BoundedPendingQueue[dict[str, Any]] = BoundedPendingQueue(
            MAX_PENDING_BATCHES, "sync/history"
        )
        # Latest unsent snapshot per kind (account / positions only).
        self._pending_snapshots: dict[str, dict[str, Any]] = {}
        self._flush_lock = asyncio.Lock()
        self._flush_stop = asyncio.Event()
        self._flush_task: asyncio.Task | None = None
        self._account_snapshot = account_snapshot or self._default_account_snapshot
        self._positions_snapshot = positions_snapshot or self._default_positions_snapshot
        self._history_orders = history_orders or self._default_history_orders
        self._history_positions = history_positions or self._default_history_positions

    # ------------------------------------------------------------------
    # queueing
    # ------------------------------------------------------------------
    def enqueue(self, kind: str, items: dict[str, Any]) -> dict[str, Any]:
        """Queue a batch. Snapshot kinds keep only the latest batch;
        history kinds go to the bounded 32-queue (oldest evicted with
        a warning, §3.3). Returns the built batch."""
        batch = build_batch(kind, items)
        if kind in ("account", "positions"):
            self._pending_snapshots[kind] = batch
        else:
            self._pending_history.put(batch, item_id=batch["batch_id"])
        return batch

    def pending_count(self) -> int:
        return len(self._pending_snapshots) + len(self._pending_history)

    # ------------------------------------------------------------------
    # flushing
    # ------------------------------------------------------------------
    async def flush(self) -> tuple[int, int]:
        """Send queued batches in order: account, positions, history.

        Stops at the first failure, keeping the rest queued. Returns
        ``(sent, failed)``. A 401/403 stops sync immediately (§3.1) —
        the REST client already refuses to retry those with the same
        key; the caller surfaces the distinct user-facing state.
        """
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
                try:
                    ack = await self.rest.send_batch(batch)
                    if ack.batch_id and ack.batch_id != batch["batch_id"]:
                        logger.warning(
                            "[SYNC] ack batch_id {} does not match sent {}",
                            ack.batch_id, batch["batch_id"],
                        )
                    sent += 1
                except ServerError as exc:
                    failed += 1
                    logger.warning(
                        "[SYNC] batch {} (kind {}) failed: {} — keeping {} queued",
                        batch["batch_id"], batch["kind"], exc.code.value, self.pending_count(),
                    )
                    # Re-queue the failed batch AND every batch after
                    # it, in original order (they were already popped).
                    self._requeue_in_order(batches[index:])
                    break
        return sent, failed

    def _requeue_in_order(self, batches: list[dict[str, Any]]) -> None:
        """Put unsent batches back in original order.

        Snapshot kinds go back to the latest slot; history kinds go
        back to the FRONT of the bounded queue, in order.
        """
        history = [b for b in batches if b["kind"] not in ("account", "positions")]
        for batch in batches:
            if batch["kind"] in ("account", "positions"):
                self._pending_snapshots.setdefault(batch["kind"], batch)
        if history:
            remaining = self._pending_history.clear()
            for batch in history + remaining:
                self._pending_history.put(batch, item_id=batch.get("batch_id"))

    async def flush_loop(self, interval_s: float = 5.0) -> None:
        """Background flusher (started by the app lifespan)."""
        self._flush_stop.clear()
        while not self._flush_stop.is_set():
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 — flusher must not die
                logger.warning("[SYNC] flush loop error: {}", type(exc).__name__)
            try:
                await asyncio.wait_for(self._flush_stop.wait(), timeout=interval_s)
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
        await self.flush()
        return batch

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
            records = _primary_records(kind, items)
            pages = _history_pages(kind, items, self.page_size)
            for page_index, page_items in enumerate(pages):
                self.enqueue(kind, page_items)
                self.checkpoint.advance_from_items(page_items)
                await self.flush()
                sent[kind] += 1
                if page_index < len(pages) - 1:
                    await self._sleep(self.page_pause_s)
            logger.info("[SYNC] {} sent {} page(s) ({} records)", kind, len(pages), len(records))
        return sent

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
        self.checkpoint.reset()
        logger.warning(
            "[SYNC] account switch: discarded {} queued history batches + {} snapshots; "
            "checkpoint reset to epoch for a fresh backfill (§7.3)",
            len(dropped), dropped_snapshots,
        )

    # ------------------------------------------------------------------
    # default providers (live terminal, via existing local commands)
    # ------------------------------------------------------------------
    @staticmethod
    def account_items(acc: Any, state: dict[str, Any]) -> dict[str, Any]:
        """Map a local account model + driver state to §3.3 account items.

        Mapping notes: ``trade_mode`` 0/1/2 → ``demo``/``contest``/
        ``live`` (§7.1); ``broker`` is not exposed by the local account
        model — omitted, never invented; ``terminal.build`` is a NUMBER
        the driver does not expose — omitted, never invented (§4.1);
        ``terminal.timestamp`` is UTC receipt time (the broker-time
        rule applies to timestamps INSIDE the payload, which stay
        verbatim broker values where present).
        """
        trade_mode = {0: "demo", 1: "contest", 2: "live"}.get(
            getattr(acc, "trade_mode", 0) or 0, "demo"
        )
        account = {
            "login": str(acc.login),
            "server": acc.server,
            "name": acc.name,
            "type": trade_mode,
            "currency": acc.currency,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "margin_free": acc.margin_free,
            "profit": acc.profit,
            "credit": acc.credit,
        }
        terminal = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "connection": {"state": state.get("state")},
            "program_name": state.get("terminal_version"),
        }
        return {"account": account, "terminal": terminal}

    async def _default_account_snapshot(self) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.common import account_info_model

        assert self.ctx is not None
        acc = await run_in_threadpool(account_info_model, self.ctx)
        state = self.ctx.driver.connection_state_dict()
        return self.account_items(acc, state)

    @staticmethod
    def positions_items(
        positions: list[dict[str, Any]], orders: list[dict[str, Any]]
    ) -> dict[str, Any]:
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

    async def _default_history_orders(
        self, from_dt: datetime, to_dt: datetime
    ) -> dict[str, Any]:
        from starlette.concurrency import run_in_threadpool

        from ..engine.orders import _history_deals, _history_orders
        from ..mt5.types import build_deal_model, build_history_order_model
        from ..mt5.types import f as _f

        assert self.ctx is not None
        offset = self.ctx.settings.broker_utc_offset_minutes

        def job() -> dict[str, Any]:
            # Broker-time range: the MT5 API takes datetimes in the
            # broker timebase; convert from UTC with the broker offset.
            from datetime import timedelta as _td


            broker_from = from_dt + _td(minutes=offset)
            broker_to = to_dt + _td(minutes=offset)
            raw_orders = _history_orders(self.ctx, broker_from, broker_to, None)
            raw_deals = _history_deals(self.ctx, broker_from, broker_to, None)
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

    async def _default_history_positions(
        self, from_dt: datetime, to_dt: datetime
    ) -> dict[str, Any]:
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
    history_positions page carries a ``positions`` chunk.
    """
    if kind == "history_orders":
        pages: list[dict[str, Any]] = []
        for key in ("orders", "deals"):
            for chunk in chunk_items(list(items.get(key, [])), page_size):
                page = {"orders": [], "deals": []}
                page[key] = chunk
                pages.append(page)
        return pages
    return [{"positions": chunk} for chunk in chunk_items(list(items.get("positions", [])), page_size)]
