"""Journal event shape (spec §8, gap §2.8 / §5 Phase C3).

A :class:`JournalEvent` is one raw execution fact, built ONLY from
actual MT5 state and history (live positions/orders snapshots, history
orders/deals, closed-position summaries, real broker execution
responses, real connection-state transitions). The Bridge NEVER invents
a trading event and NEVER infers a fill from a submitted request: a
submitted order becomes an ``order_accepted`` / ``deal_executed`` event
only when MT5 history (or the broker's own response) says so. See
:mod:`nse_bridge.journal.collector` for the per-source mapping table.

There is NO journal/event upload channel in CONTRACT v1 (dependency
D6): the only sync kinds are ``account | positions | history_orders |
history_positions`` (§3.3). Journal events therefore live locally in
the collector's bounded store for the Execution screen (Phase C4) and
diagnostics; the server today derives journal entries from the sync
batches. This module defines the STABLE event shape C4 consumes — do
not rename fields or change ``to_dict()`` keys without updating
``docs/JOURNAL.md`` and the C4 consumer.

Dedup key (documented, stable across re-syncs)::

    (event_type, deal_id | order_id | position_id | ticket, execution_time)

The first non-None identifier wins, in that order. An event carrying
neither an identifier nor an execution time (e.g. an order submitted
before the broker assigns a ticket) has no identifying key and is
NEVER deduped — each such submission is a distinct event, and its
``event_id`` is random. An event WITH an identifying key gets a
deterministic ``event_id`` (``evt-<sha256(key)[:32]>``) so a re-sync
reproduces the same id and the server/GUI can dedup on it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = [
    "JournalEvent",
    "JournalEventType",
    "dedup_key",
    "event_id_for_key",
]


class JournalEventType(str, Enum):
    """Spec §8 event types. Values are the stable wire/local names."""

    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    POSITION_OPENED = "position_opened"
    POSITION_MODIFIED = "position_modified"
    POSITION_PARTIALLY_CLOSED = "position_partially_closed"
    POSITION_FULLY_CLOSED = "position_fully_closed"
    PENDING_CREATED = "pending_created"
    PENDING_MODIFIED = "pending_modified"
    PENDING_CANCELED = "pending_canceled"
    DEAL_EXECUTED = "deal_executed"
    EXECUTION_RESPONSE_RECEIVED = "execution_response_received"
    BROKER_ERROR = "broker_error"
    TERMINAL_CONNECTION_LOST = "terminal_connection_lost"
    ACCOUNT_CONNECTION_CHANGED = "account_connection_changed"


def dedup_key(event: JournalEvent) -> tuple[str, str, str] | None:
    """The event-level dedup key, or None when the event is unidentifiable.

    Returns None when the event carries no identifier (deal / order /
    position / ticket) AND no execution time: such events are never
    deduped (see the module docstring).
    """
    ref = event.deal_id or event.order_id or event.position_id or event.ticket
    when = event.execution_time or event.broker_time or ""
    if ref is None and not when:
        return None
    return (event.event_type.value, "" if ref is None else str(ref), when)


def event_id_for_key(key: tuple[str, str, str] | None) -> str:
    """Deterministic id for an identified event, random id otherwise."""
    if key is None:
        return f"evt-{uuid.uuid4().hex[:32]}"
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()
    return f"evt-{digest[:32]}"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class JournalEvent:
    """One raw journal event — the FULL spec §8 field set.

    ``execution_time`` is the broker event time converted to UTC ISO
    (``...Z``); ``broker_time`` keeps the verbatim broker timestamp
    string when the source record carried one (never re-localized,
    §3.3 broker-time rule). ``server_timestamp`` is the UTC receipt
    time on this Bridge (connector time, not broker time). All money
    fields are raw broker values; ``None`` means "not reported by the
    broker for this event", never zero. ``source`` names the local
    evidence the event was built from (``mt5_history``,
    ``mt5_state_delta``, ``execution_response``, ``terminal``,
    ``account``). ``detail`` is a short sanitized note (never a secret,
    never a stack trace).
    """

    event_type: JournalEventType
    ticket: int | None = None
    position_id: int | None = None
    order_id: int | None = None
    deal_id: int | None = None
    symbol: str | None = None
    volume: float | None = None
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    commission: float | None = None
    swap: float | None = None
    profit: float | None = None
    execution_time: str | None = None
    broker_time: str | None = None
    magic: int | None = None
    broker_retcode: int | None = None
    retcode_label: str | None = None
    correlation_id: str | None = None
    source_command_id: str | None = None
    server_timestamp: str = field(default_factory=_utc_now_iso)
    source: str = "mt5_history"
    detail: str | None = None
    account_login: int | None = None
    event_id: str = ""

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = event_id_for_key(dedup_key(self))

    def to_dict(self) -> dict[str, Any]:
        """Stable dict shape (consumed by the Phase C4 GUI)."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "ticket": self.ticket,
            "position_id": self.position_id,
            "order_id": self.order_id,
            "deal_id": self.deal_id,
            "symbol": self.symbol,
            "volume": self.volume,
            "price": self.price,
            "sl": self.sl,
            "tp": self.tp,
            "commission": self.commission,
            "swap": self.swap,
            "profit": self.profit,
            "execution_time": self.execution_time,
            "broker_time": self.broker_time,
            "magic": self.magic,
            "broker_retcode": self.broker_retcode,
            "retcode_label": self.retcode_label,
            "correlation_id": self.correlation_id,
            "source_command_id": self.source_command_id,
            "server_timestamp": self.server_timestamp,
            "source": self.source,
            "detail": self.detail,
            "account_login": self.account_login,
        }
