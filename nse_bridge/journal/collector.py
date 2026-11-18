"""Journal-event collector (spec §8, gap §2.8 / §5 Phase C3).

Builds :class:`~nse_bridge.journal.events.JournalEvent` records ONLY
from actual MT5 state and history. The source-of-truth rule (never
invent, never infer a fill from a submitted request) is enforced by
construction: every builder below takes an MT5 record — a live
position/order snapshot row, a history order/deal, a closed-position
summary, a real broker execution response, or a real connection-state
transition — and returns nothing when the record does not support an
event.

Source table (event -> authoritative evidence)::

    order_submitted               live execution path, real submitted request
                                  (record_execution_response / record_order_submitted)
    order_accepted                history order FILLED/PARTIAL, or broker
                                  retcode DONE on the execution response
    order_rejected                history order REJECTED, or broker error retcode
    position_opened               snapshot diff: ticket appears in live positions
    position_modified             snapshot diff: SL/TP (or uncorroborated volume)
                                  changed on a live position
    position_partially_closed     closed-position summary, volume_out < volume_in,
                                  or snapshot volume drop corroborated by an
                                  OUT/INOUT deal for the same position_id
    position_fully_closed         closed-position summary, volume_out >= volume_in,
                                  or snapshot disappearance corroborated by an
                                  OUT deal (see diff_snapshots)
    pending_created               snapshot diff: ticket appears in live orders
    pending_modified              snapshot diff: price/SL/TP/volume changed
    pending_canceled              history order CANCELED/EXPIRED (pending type)
    deal_executed                 history deal, trade type BUY/SELL
    execution_response_received   real broker response to an execution request
    broker_error                  real broker error retcode / driver error
    terminal_connection_lost      real driver connection-state transition
    account_connection_changed    real account/login transition

Deliberate non-events: a position that merely VANISHES from a snapshot
(with no corroborating OUT deal and no closed-position summary) emits
NO close event — the terminal may have restarted, the poll may have
failed, or the close may not have happened. The next history pass
emits the close with its deal facts. Likewise an order that vanishes
from the live book emits nothing until history shows CANCELED/EXPIRED/
FILLED. A first snapshot establishes the baseline and emits nothing.

Ranges & pagination: every history collection takes explicit
``from_dt``/``to_dt`` bounds (both required; ``from_dt > to_dt`` is an
error). There is NO unlimited download: the default path is the sync
checkpoint range ``[checkpoint, now]`` and records outside the range
are dropped, never emitted. Records are processed in pages of
``page_size`` (default 500, mirroring sync §3.3); after each page the
shared durable checkpoint (``SyncCheckpoint`` from B1, owned by the
sync engine — the collector never builds a second checkpoint
mechanism) advances to the max event time seen and the engine's
persist callback runs.

Dedup: event-level, by the documented key in
:mod:`nse_bridge.journal.events` (event type, deal/order/position/
ticket id, execution time). The seen-set is bounded (``max_seen``,
default 10,000 — evicts oldest first); the emitted-event store is
bounded too (``max_events``, default 1,000 — oldest dropped). A second
collection of the same range therefore emits nothing new. ``reset()``
clears both (account switch: the new account must not inherit the old
account's dedup state).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..retcodes import TRADE_RETCODE_DONE, translate_retcode
from ..timeutil import broker_epoch_to_utc, iso, normalize_utc
from .events import JournalEvent, JournalEventType, dedup_key

__all__ = [
    "JournalCollector",
    "events_from_closed_position",
    "events_from_deal",
    "events_from_history_order",
]

#: MT5 order ``type`` values that are pending orders (LIMIT/STOP).
PENDING_ORDER_TYPES = frozenset({2, 3, 4, 5, 6, 7})
#: MT5 history-order ``state`` values (ORDER_STATE_LABELS).
_STATE_STARTED = 0
_STATE_PLACED = 1
_STATE_CANCELED = 2
_STATE_PARTIAL = 3
_STATE_FILLED = 4
_STATE_REJECTED = 5
_STATE_EXPIRED = 7
#: MT5 deal ``entry`` values (DEAL_ENTRY_LABELS).
_ENTRY_IN = 0
_ENTRY_OUT = 1
_ENTRY_INOUT = 2
_ENTRY_OUT_BY = 3


def _parse_time(value: Any, broker_offset_minutes: int = 0) -> datetime | None:
    """Parse a broker time value (ISO string, epoch, datetime) to aware UTC.

    ISO strings are read via the shared ``normalize_utc`` rule (naive =
    UTC); epoch seconds are broker-local seconds, converted with the
    broker offset. Unparseable values return None and never order or
    place an event.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return broker_epoch_to_utc(float(value), broker_offset_minutes)
        except (OverflowError, OSError, ValueError):
            return None
    return normalize_utc(value, broker_offset_minutes)


def _event_time_iso(
    record: dict[str, Any], *keys: str, broker_offset_minutes: int = 0
) -> tuple[str | None, str | None]:
    """(UTC ISO execution time, verbatim broker time string) for a record.

    Prefers a ``*_utc`` ISO field (already UTC), then any other listed
    key. The verbatim string is returned only when the source value
    was a string (an epoch has no verbatim form to preserve).
    """
    for key in keys:
        value = record.get(key)
        if value is None or value == "":
            continue
        parsed = _parse_time(value, broker_offset_minutes)
        if parsed is None:
            continue
        verbatim = value if isinstance(value, str) else None
        return iso(parsed), verbatim
    return None, None


def _num(record: dict[str, Any], key: str) -> float | None:
    value = record.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(record: dict[str, Any], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base(
    event_type: JournalEventType,
    record: dict[str, Any],
    *,
    time_keys: tuple[str, ...],
    broker_offset_minutes: int = 0,
    correlation_id: str | None = None,
    source_command_id: str | None = None,
    account_login: int | None = None,
    source: str = "mt5_history",
    detail: str | None = None,
) -> JournalEvent:
    execution_time, broker_time = _event_time_iso(record, *time_keys, broker_offset_minutes=broker_offset_minutes)
    return JournalEvent(
        event_type=event_type,
        ticket=_int(record, "ticket"),
        position_id=_int(record, "position_id"),
        symbol=record.get("symbol") or None,
        volume=_num(record, "volume"),
        price=_num(record, "price") or _num(record, "price_open") or _num(record, "price_current"),
        sl=_num(record, "sl"),
        tp=_num(record, "tp"),
        commission=_num(record, "commission"),
        swap=_num(record, "swap"),
        profit=_num(record, "profit"),
        execution_time=execution_time,
        broker_time=broker_time,
        magic=_int(record, "magic"),
        correlation_id=correlation_id,
        source_command_id=source_command_id,
        source=source,
        detail=detail,
        account_login=account_login,
    )


def events_from_history_order(
    order: dict[str, Any],
    *,
    broker_offset_minutes: int = 0,
    correlation_id: str | None = None,
    source_command_id: str | None = None,
    account_login: int | None = None,
) -> list[JournalEvent]:
    """Journal events for one history-order record (model-dict shape).

    FILLED/PARTIAL -> order_accepted; REJECTED -> order_rejected;
    CANCELED/EXPIRED on a pending type -> pending_canceled; anything
    else (STARTED/PLACED, or a state the record does not carry) ->
    no event. A record without a ticket is dropped: it cannot be
    reconciled (same rule as sync §3.3).
    """
    if _int(order, "ticket") in (None, 0):
        return []
    state = _int(order, "state")
    if state is None:
        return []
    event = _base(
        JournalEventType.ORDER_ACCEPTED,  # placeholder; replaced below
        order,
        time_keys=("time_done_utc", "time_done", "time_setup_utc", "time_setup"),
        broker_offset_minutes=broker_offset_minutes,
        correlation_id=correlation_id,
        source_command_id=source_command_id,
        account_login=account_login,
    )
    event.order_id = event.ticket
    event.volume = _num(order, "volume_initial") or _num(order, "volume_current")
    if state in (_STATE_FILLED, _STATE_PARTIAL):
        event.event_type = JournalEventType.ORDER_ACCEPTED
        if state == _STATE_PARTIAL:
            event.detail = "order partially filled (broker state PARTIAL)"
        return [event]
    if state == _STATE_REJECTED:
        event.event_type = JournalEventType.ORDER_REJECTED
        event.detail = "order rejected by the broker (history state REJECTED; no retcode in history)"
        return [event]
    if state in (_STATE_CANCELED, _STATE_EXPIRED):
        if _int(order, "type") in PENDING_ORDER_TYPES:
            event.event_type = JournalEventType.PENDING_CANCELED
            event.detail = "pending order expired" if state == _STATE_EXPIRED else "pending order canceled"
            return [event]
        event.event_type = JournalEventType.ORDER_REJECTED
        event.detail = "order canceled before fill (history state CANCELED)"
        return [event]
    return []


def events_from_deal(
    deal: dict[str, Any],
    *,
    broker_offset_minutes: int = 0,
    correlation_id: str | None = None,
    source_command_id: str | None = None,
    account_login: int | None = None,
) -> list[JournalEvent]:
    """Journal events for one history-deal record (model-dict shape).

    Trade deals (type BUY/SELL) -> exactly one ``deal_executed``.
    Balance/credit/fee deals (BALANCE, CREDIT, CHARGE, CORRECTION,
    BONUS, COMMISSION_*) are NOT trade executions: they emit no journal
    trade event and are surfaced by
    :func:`nse_bridge.journal.accounting.surface_balance_operations`
    instead. A record without a ticket is dropped.
    """
    if _int(deal, "ticket") in (None, 0):
        return []
    if _int(deal, "type") not in (0, 1):
        return []
    event = _base(
        JournalEventType.DEAL_EXECUTED,
        deal,
        time_keys=("time_utc", "time"),
        broker_offset_minutes=broker_offset_minutes,
        correlation_id=correlation_id,
        source_command_id=source_command_id,
        account_login=account_login,
    )
    event.deal_id = event.ticket
    event.order_id = _int(deal, "order")
    return [event]


def events_from_closed_position(
    summary: dict[str, Any],
    *,
    broker_offset_minutes: int = 0,
    correlation_id: str | None = None,
    source_command_id: str | None = None,
    account_login: int | None = None,
) -> list[JournalEvent]:
    """Journal events for one closed-position summary (deal-derived).

    The summary is the local reconstruction from history deals
    (``get_trading_history_positions``): ``volume_out >= volume_in``
    means fully closed, less means partially closed. A summary without
    a ``position_id`` is dropped. Money fields here are locally
    reconstructed totals over broker-supplied deals — see
    :func:`nse_bridge.journal.accounting.label_closed_position`.
    """
    position_id = _int(summary, "position_id")
    if position_id in (None, 0):
        return []
    vol_in = _num(summary, "volume_in") or 0.0
    vol_out = _num(summary, "volume_out") or 0.0
    fully_closed = vol_out >= vol_in > 0
    event = _base(
        JournalEventType.POSITION_FULLY_CLOSED if fully_closed else JournalEventType.POSITION_PARTIALLY_CLOSED,
        summary,
        time_keys=("close_time_utc", "open_time_utc"),
        broker_offset_minutes=broker_offset_minutes,
        correlation_id=correlation_id,
        source_command_id=source_command_id,
        account_login=account_login,
    )
    event.position_id = position_id
    event.ticket = position_id
    event.volume = vol_out
    event.detail = f"closed position {position_id}: volume out {vol_out} of {vol_in}"
    return [event]


class JournalCollector:
    """Bounded, deduping collector of journal events.

    Constructed with the sync engine's shared ``SyncCheckpoint`` (B1)
    and its persist callback — the collector advances the checkpoint
    from event times but owns no store of its own. All collection
    methods are pure over the MT5 records they are handed (plus the
    live-snapshot diff), so unit tests drive them without MT5.
    """

    def __init__(
        self,
        *,
        checkpoint: Any | None = None,
        persist_checkpoint: Callable[[], None] | None = None,
        broker_offset_minutes: int = 0,
        account_login: int | None = None,
        max_seen: int = 10_000,
        max_events: int = 1_000,
    ) -> None:
        self.checkpoint = checkpoint
        self._persist_checkpoint = persist_checkpoint
        self.broker_offset_minutes = broker_offset_minutes
        self.account_login = account_login
        self.max_seen = max_seen
        self.max_events = max_events
        self._seen: dict[tuple[str, str, str], None] = {}
        self._events: deque[JournalEvent] = deque(maxlen=max_events)
        self.emitted = 0
        self.duplicates_dropped = 0
        self.out_of_range_dropped = 0
        self.unidentified_dropped = 0

    # ------------------------------------------------------------------
    # core: accept (dedup + store + checkpoint)
    # ------------------------------------------------------------------
    def _accept(self, event: JournalEvent) -> JournalEvent | None:
        """Store ``event`` unless its dedup key was already seen.

        Unidentifiable events (no key) are always stored. Returns the
        event when stored, else None.
        """
        key = dedup_key(event)
        if key is not None:
            if key in self._seen:
                self.duplicates_dropped += 1
                return None
            self._seen[key] = None
            if len(self._seen) > self.max_seen:
                self._seen.pop(next(iter(self._seen)))  # oldest first
        self._events.append(event)
        self.emitted += 1
        if (
            self.checkpoint is not None
            and event.execution_time
            and self.checkpoint.advance(event.execution_time)
            and self._persist_checkpoint is not None
        ):
            try:
                self._persist_checkpoint()
            except Exception:  # noqa: BLE001 — persistence must never break journaling
                logger.warning("[JOURNAL] checkpoint persist failed; continuing")
        return event

    def _accept_all(self, events: list[JournalEvent]) -> list[JournalEvent]:
        return [kept for event in events if (kept := self._accept(event)) is not None]

    # ------------------------------------------------------------------
    # history collection (bounded ranges, paginated)
    # ------------------------------------------------------------------
    def collect_from_history(
        self,
        *,
        orders: list[dict[str, Any]] | None = None,
        deals: list[dict[str, Any]] | None = None,
        closed_positions: list[dict[str, Any]] | None = None,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = 500,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> list[JournalEvent]:
        """Collect events from already-fetched MT5 history records.

        ``from_dt``/``to_dt`` are REQUIRED (no unbounded download);
        ``from_dt > to_dt`` raises ValueError. Records whose event
        time falls outside ``[from_dt, to_dt]`` are dropped and counted
        (``out_of_range_dropped``); records with no parseable event
        time are dropped the same way — an event that cannot be placed
        in the requested range is not emitted. Records are processed in
        pages of ``page_size`` (default 500, mirroring sync §3.3).
        """
        if from_dt > to_dt:
            raise ValueError(f"journal range is empty: from_dt {from_dt!r} is after to_dt {to_dt!r}")
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        kwargs: dict[str, Any] = {
            "broker_offset_minutes": self.broker_offset_minutes,
            "correlation_id": correlation_id,
            "source_command_id": source_command_id,
            "account_login": self.account_login,
        }
        emitted: list[JournalEvent] = []
        for records, builder in (
            (orders or [], events_from_history_order),
            (deals or [], events_from_deal),
            (closed_positions or [], events_from_closed_position),
        ):
            for start in range(0, len(records), page_size):
                for record in records[start : start + page_size]:
                    built = builder(record, **kwargs)
                    for event in built:
                        if not self._in_range(event, from_dt, to_dt):
                            self.out_of_range_dropped += 1
                            continue
                        kept = self._accept(event)
                        if kept is not None:
                            emitted.append(kept)
        return emitted

    def _in_range(self, event: JournalEvent, from_dt: datetime, to_dt: datetime) -> bool:
        """True when the event's execution time is inside the range.

        Events with no execution time at all cannot be placed in a
        bounded range and are dropped (counted as out-of-range).
        """
        if event.execution_time is None:
            return False
        parsed = _parse_time(event.execution_time, 0)
        if parsed is None:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        start = from_dt if from_dt.tzinfo is not None else from_dt.replace(tzinfo=UTC)
        end = to_dt if to_dt.tzinfo is not None else to_dt.replace(tzinfo=UTC)
        return start <= parsed <= end

    def collect_range(
        self,
        from_dt: datetime,
        to_dt: datetime,
        *,
        fetch_history_orders: Callable[[datetime, datetime], dict[str, Any]],
        fetch_history_positions: Callable[[datetime, datetime], dict[str, Any]],
        page_size: int = 500,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> list[JournalEvent]:
        """Fetch one bounded range and collect its events (paginated).

        The fetchers receive the EXACT requested bounds (the server
        decides ranges; the collector never widens them) and return the
        same shapes the sync engine consumes (``{"orders","deals"}`` /
        ``{"positions"}``). This is the standalone entry point for a
        manual/GUI resync of one range; the sync engine instead calls
        :meth:`collect_from_history` on records it already fetched, so
        history is never fetched twice.
        """
        if from_dt > to_dt:
            raise ValueError(f"journal range is empty: from_dt {from_dt!r} is after to_dt {to_dt!r}")
        items_orders = fetch_history_orders(from_dt, to_dt)
        items_positions = fetch_history_positions(from_dt, to_dt)
        return self.collect_from_history(
            orders=list(items_orders.get("orders", [])),
            deals=list(items_orders.get("deals", [])),
            closed_positions=list(items_positions.get("positions", [])),
            from_dt=from_dt,
            to_dt=to_dt,
            page_size=page_size,
            correlation_id=correlation_id,
            source_command_id=source_command_id,
        )

    # ------------------------------------------------------------------
    # live snapshot diffs
    # ------------------------------------------------------------------
    def diff_snapshots(
        self,
        prev_positions: list[dict[str, Any]] | None,
        prev_orders: list[dict[str, Any]] | None,
        curr_positions: list[dict[str, Any]],
        curr_orders: list[dict[str, Any]],
        *,
        deals: list[dict[str, Any]] | None = None,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> list[JournalEvent]:
        """Events from a live positions/orders snapshot transition.

        The first call (``prev_*`` None) establishes the baseline and
        emits nothing — pre-existing positions are not "opened" by the
        Bridge. A position that vanishes emits NOTHING without a
        corroborating OUT deal (see the module docstring); an order
        that vanishes likewise. ``deals`` (recent history deals, when
        the caller has them) corroborate volume drops and
        disappearances into partial/full closes.
        """
        if prev_positions is None or prev_orders is None:
            return []
        kwargs: dict[str, Any] = {
            "broker_offset_minutes": self.broker_offset_minutes,
            "correlation_id": correlation_id,
            "source_command_id": source_command_id,
            "account_login": self.account_login,
        }
        out_deals: dict[int, dict[str, Any]] = {}
        for deal in deals or []:
            pos_id = _int(deal, "position_id")
            if pos_id is not None and _int(deal, "entry") in (_ENTRY_OUT, _ENTRY_INOUT, _ENTRY_OUT_BY):
                out_deals.setdefault(pos_id, deal)

        emitted: list[JournalEvent] = []
        prev_pos = {t: p for p in prev_positions if (t := _int(p, "ticket")) is not None}
        curr_pos = {t: p for p in curr_positions if (t := _int(p, "ticket")) is not None}
        for ticket, pos in curr_pos.items():
            previous = prev_pos.get(ticket)
            if previous is None:
                event = _base(
                    JournalEventType.POSITION_OPENED,
                    pos,
                    time_keys=("time_utc", "time"),
                    source="mt5_state_delta",
                    **kwargs,
                )
                event.position_id = ticket
                emitted.extend(self._accept_all([event]))
                continue
            curr_volume = _num(pos, "volume")
            prev_volume = _num(previous, "volume")
            if curr_volume is not None and prev_volume is not None and curr_volume < prev_volume:
                corroborating = out_deals.get(ticket)
                if corroborating is not None:
                    event = _base(
                        JournalEventType.POSITION_PARTIALLY_CLOSED,
                        corroborating,
                        time_keys=("time_utc", "time"),
                        source="mt5_state_delta",
                        detail=f"position {ticket} volume fell from {previous.get('volume')} to {pos.get('volume')}",
                        **kwargs,
                    )
                    event.deal_id = _int(corroborating, "ticket")
                    event.order_id = _int(corroborating, "order")
                    event.position_id = ticket
                    event.volume = prev_volume - curr_volume
                    emitted.extend(self._accept_all([event]))
                    continue
                # Volume fell with no corroborating deal: report the
                # change, never assert a close.
                event = _base(
                    JournalEventType.POSITION_MODIFIED,
                    pos,
                    time_keys=("time_utc", "time"),
                    source="mt5_state_delta",
                    detail=f"position {ticket} volume changed without a corroborating deal — close NOT asserted",
                    **kwargs,
                )
                event.position_id = ticket
                emitted.extend(self._accept_all([event]))
                continue
            if _num(pos, "sl") != _num(previous, "sl") or _num(pos, "tp") != _num(previous, "tp"):
                event = _base(
                    JournalEventType.POSITION_MODIFIED,
                    pos,
                    time_keys=("time_utc", "time"),
                    source="mt5_state_delta",
                    **kwargs,
                )
                event.position_id = ticket
                emitted.extend(self._accept_all([event]))
        for ticket, pos in prev_pos.items():
            if ticket not in curr_pos and ticket in out_deals:
                deal = out_deals[ticket]
                event = _base(
                    JournalEventType.POSITION_FULLY_CLOSED,
                    deal,
                    time_keys=("time_utc", "time"),
                    source="mt5_state_delta",
                    detail=f"position {ticket} left the live book with a corroborating OUT deal",
                    **kwargs,
                )
                event.deal_id = _int(deal, "ticket")
                event.order_id = _int(deal, "order")
                event.position_id = ticket
                emitted.extend(self._accept_all([event]))

        prev_ord = {t: o for o in prev_orders if (t := _int(o, "ticket")) is not None}
        curr_ord = {t: o for o in curr_orders if (t := _int(o, "ticket")) is not None}
        for ticket, order in curr_ord.items():
            previous = prev_ord.get(ticket)
            if previous is None:
                event = _base(
                    JournalEventType.PENDING_CREATED,
                    order,
                    time_keys=("time_setup_utc", "time_setup"),
                    source="mt5_state_delta",
                    **kwargs,
                )
                event.order_id = ticket
                emitted.extend(self._accept_all([event]))
                continue
            changed = (
                _num(order, "price_open") != _num(previous, "price_open")
                or _num(order, "sl") != _num(previous, "sl")
                or _num(order, "tp") != _num(previous, "tp")
                or _num(order, "volume_current") != _num(previous, "volume_current")
            )
            if changed:
                event = _base(
                    JournalEventType.PENDING_MODIFIED,
                    order,
                    time_keys=("time_setup_utc", "time_setup"),
                    source="mt5_state_delta",
                    **kwargs,
                )
                event.order_id = ticket
                event.volume = _num(order, "volume_current") or _num(order, "volume_initial")
                emitted.extend(self._accept_all([event]))
        return emitted

    # ------------------------------------------------------------------
    # execution responses & connection events (real broker facts only)
    # ------------------------------------------------------------------
    def record_order_submitted(
        self,
        *,
        symbol: str,
        volume: float | None = None,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        magic: int | None = None,
        ticket: int | None = None,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> JournalEvent | None:
        """An order request the Bridge actually submitted to MT5.

        No fill is implied: this event records the submission only.
        """
        event = JournalEvent(
            event_type=JournalEventType.ORDER_SUBMITTED,
            ticket=ticket,
            order_id=ticket,
            symbol=symbol,
            volume=volume,
            price=price,
            sl=sl,
            tp=tp,
            magic=magic,
            correlation_id=correlation_id,
            source_command_id=source_command_id,
            source="execution_response",
            account_login=self.account_login,
        )
        return self._accept(event)

    def record_execution_response(
        self,
        *,
        broker_retcode: int,
        ticket: int | None = None,
        order_id: int | None = None,
        deal_id: int | None = None,
        position_id: int | None = None,
        symbol: str | None = None,
        volume: float | None = None,
        price: float | None = None,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> list[JournalEvent]:
        """Events for a REAL broker execution response (retcode + ids).

        Retcode DONE (10009) -> ``execution_response_received`` +
        ``order_accepted``. Any other retcode -> the same response
        event + ``broker_error`` + ``order_rejected``. The response
        event itself is always emitted: it is the receipt fact,
        distinct from the outcome.
        """
        label = translate_retcode(broker_retcode)
        base = JournalEvent(
            event_type=JournalEventType.EXECUTION_RESPONSE_RECEIVED,
            ticket=ticket,
            position_id=position_id,
            order_id=order_id if order_id is not None else ticket,
            deal_id=deal_id,
            symbol=symbol,
            volume=volume,
            price=price,
            broker_retcode=broker_retcode,
            retcode_label=label,
            correlation_id=correlation_id,
            source_command_id=source_command_id,
            source="execution_response",
            detail=f"broker response: {label}",
            account_login=self.account_login,
        )
        emitted = self._accept_all([base])
        if broker_retcode == TRADE_RETCODE_DONE:
            accepted = JournalEvent(
                event_type=JournalEventType.ORDER_ACCEPTED,
                ticket=ticket,
                position_id=position_id,
                order_id=order_id if order_id is not None else ticket,
                deal_id=deal_id,
                symbol=symbol,
                volume=volume,
                price=price,
                broker_retcode=broker_retcode,
                retcode_label=label,
                correlation_id=correlation_id,
                source_command_id=source_command_id,
                source="execution_response",
                detail="broker accepted the order (retcode DONE)",
                account_login=self.account_login,
            )
            emitted.extend(self._accept_all([accepted]))
        else:
            error = JournalEvent(
                event_type=JournalEventType.BROKER_ERROR,
                ticket=ticket,
                position_id=position_id,
                order_id=order_id if order_id is not None else ticket,
                deal_id=deal_id,
                symbol=symbol,
                broker_retcode=broker_retcode,
                retcode_label=label,
                correlation_id=correlation_id,
                source_command_id=source_command_id,
                source="execution_response",
                detail=f"broker rejected the request: {label}",
                account_login=self.account_login,
            )
            rejected = JournalEvent(
                event_type=JournalEventType.ORDER_REJECTED,
                ticket=ticket,
                position_id=position_id,
                order_id=order_id if order_id is not None else ticket,
                deal_id=deal_id,
                symbol=symbol,
                broker_retcode=broker_retcode,
                retcode_label=label,
                correlation_id=correlation_id,
                source_command_id=source_command_id,
                source="execution_response",
                detail=f"broker rejected the request: {label}",
                account_login=self.account_login,
            )
            emitted.extend(self._accept_all([error, rejected]))
        return emitted

    def record_broker_error(
        self,
        *,
        broker_retcode: int | None,
        detail: str,
        symbol: str | None = None,
        ticket: int | None = None,
        correlation_id: str | None = None,
        source_command_id: str | None = None,
    ) -> JournalEvent | None:
        """A broker error that actually occurred (retcode / driver error)."""
        return self._accept(
            JournalEvent(
                event_type=JournalEventType.BROKER_ERROR,
                ticket=ticket,
                order_id=ticket,
                symbol=symbol,
                broker_retcode=broker_retcode,
                retcode_label=translate_retcode(broker_retcode) if broker_retcode is not None else None,
                correlation_id=correlation_id,
                source_command_id=source_command_id,
                source="execution_response",
                detail=detail,
                account_login=self.account_login,
            )
        )

    def record_terminal_connection_lost(self, *, detail: str) -> JournalEvent | None:
        """The terminal connection was actually lost (driver state)."""
        return self._accept(
            JournalEvent(
                event_type=JournalEventType.TERMINAL_CONNECTION_LOST,
                source="terminal",
                detail=detail,
                account_login=self.account_login,
            )
        )

    def record_account_connection_changed(self, *, login: int | None, detail: str) -> JournalEvent | None:
        """The account connection actually changed (login transition)."""
        return self._accept(
            JournalEvent(
                event_type=JournalEventType.ACCOUNT_CONNECTION_CHANGED,
                account_login=login if login is not None else self.account_login,
                source="account",
                detail=detail,
            )
        )

    # ------------------------------------------------------------------
    # accessors / lifecycle
    # ------------------------------------------------------------------
    def events(self) -> list[JournalEvent]:
        """Emitted events, oldest first (bounded store)."""
        return list(self._events)

    def event_dicts(self) -> list[dict[str, Any]]:
        """Stable dicts for the Phase C4 Execution screen / diagnostics."""
        return [event.to_dict() for event in self._events]

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def reset(self) -> None:
        """Clear dedup state + stored events (account switch, §7.3)."""
        self._seen.clear()
        self._events.clear()
        self.emitted = 0
        self.duplicates_dropped = 0
        self.out_of_range_dropped = 0
        self.unidentified_dropped = 0
