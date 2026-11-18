"""Journal package (spec §8, gap §2.8 / §5 Phase C3).

Public surface: the stable :class:`JournalEvent` shape, the
:class:`JournalCollector` that builds events only from actual MT5
state/history, and the accounting labelling in
:mod:`nse_bridge.journal.accounting`. There is no journal upload
channel in CONTRACT v1 (dependency D6) — events live in the
collector's bounded in-memory store for the Phase C4 Execution screen
and diagnostics; the server derives journal entries from the §3.3
sync batches until a contract channel exists.
"""

from __future__ import annotations

from .accounting import (
    LOCALLY_DERIVED,
    MT5_SUPPLIED,
    NOT_REPORTED,
    LabelledValue,
    classify_deal,
    label_account_snapshot,
    label_closed_position,
    label_deal,
    surface_balance_operations,
)
from .collector import (
    JournalCollector,
    events_from_closed_position,
    events_from_deal,
    events_from_history_order,
)
from .events import JournalEvent, JournalEventType, dedup_key, event_id_for_key

__all__ = [
    "LOCALLY_DERIVED",
    "MT5_SUPPLIED",
    "NOT_REPORTED",
    "JournalCollector",
    "JournalEvent",
    "JournalEventType",
    "LabelledValue",
    "classify_deal",
    "dedup_key",
    "event_id_for_key",
    "events_from_closed_position",
    "events_from_deal",
    "events_from_history_order",
    "label_account_snapshot",
    "label_closed_position",
    "label_deal",
    "surface_balance_operations",
]
