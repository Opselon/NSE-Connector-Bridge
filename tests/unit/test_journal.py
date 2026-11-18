"""Unit tests for the journal-event collector (spec §8, gap §2.8 / §5 C3).

Label: unit. No MT5, no network — records are plain model-dict shapes
(as produced by ``nse_bridge.mt5.types.build_*_model(...).model_dump()``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nse_bridge.journal.collector import (
    JournalCollector,
    events_from_closed_position,
    events_from_deal,
    events_from_history_order,
)
from nse_bridge.journal.events import JournalEvent, JournalEventType, dedup_key
from nse_bridge.server.sync import SyncCheckpoint

pytestmark = pytest.mark.unit

FROM = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
TO = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)


def _deal(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ticket": 101,
        "order": 55,
        "position_id": 7,
        "symbol": "XAUUSD.a",
        "type": 0,
        "entry": 0,
        "volume": 0.1,
        "price": 2650.5,
        "profit": 0.0,
        "commission": -1.5,
        "swap": 0.0,
        "fee": 0.0,
        "magic": 123,
        "time": 1763280000,
        "time_utc": "2026-11-16T10:00:00",
    }
    record.update(overrides)
    return record


def _history_order(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ticket": 55,
        "symbol": "XAUUSD.a",
        "type": 0,
        "volume_initial": 0.1,
        "volume_current": 0.0,
        "price_open": 2650.5,
        "sl": 2640.0,
        "tp": 2670.0,
        "state": 4,
        "state_label": "FILLED",
        "magic": 123,
        "position_id": 7,
        "time_setup": 1763279940,
        "time_setup_utc": "2026-11-16T09:59:00",
        "time_done": 1763280000,
        "time_done_utc": "2026-11-16T10:00:00",
    }
    record.update(overrides)
    return record


def _closed_position(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "position_id": 7,
        "symbol": "XAUUSD.a",
        "volume_in": 0.2,
        "volume_out": 0.2,
        "profit": 12.5,
        "commission": -3.0,
        "swap": 0.0,
        "fee": 0.0,
        "open_time_utc": "2026-11-16T09:00:00",
        "close_time_utc": "2026-11-16T11:00:00",
        "direction": "buy",
    }
    record.update(overrides)
    return record


def _position(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ticket": 7,
        "symbol": "XAUUSD.a",
        "type": 0,
        "volume": 0.2,
        "price_open": 2650.5,
        "price_current": 2655.0,
        "sl": 2640.0,
        "tp": 2670.0,
        "profit": 9.0,
        "swap": 0.0,
        "commission": -1.5,
        "magic": 123,
        "time": 1763276400,
        "time_utc": "2026-11-16T09:00:00",
    }
    record.update(overrides)
    return record


def _order(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ticket": 88,
        "symbol": "XAUUSD.a",
        "type": 2,
        "type_label": "BUY_LIMIT",
        "volume_initial": 0.1,
        "volume_current": 0.1,
        "price_open": 2600.0,
        "price_current": 2650.5,
        "sl": 2590.0,
        "tp": 2620.0,
        "state": 1,
        "state_label": "PLACED",
        "magic": 123,
        "time_setup": 1763276400,
        "time_setup_utc": "2026-11-16T09:00:00",
    }
    record.update(overrides)
    return record


# ----------------------------------------------------------------------
# event shape
# ----------------------------------------------------------------------


def test_journal_event_carries_full_spec_section_8_field_set():
    event = JournalEvent(
        event_type=JournalEventType.DEAL_EXECUTED,
        ticket=101,
        position_id=7,
        order_id=55,
        deal_id=101,
        symbol="XAUUSD.a",
        volume=0.1,
        price=2650.5,
        sl=2640.0,
        tp=2670.0,
        commission=-1.5,
        swap=0.0,
        profit=12.5,
        execution_time="2026-11-16T10:00:00+00:00",
        magic=123,
        broker_retcode=10009,
        correlation_id="corr-1",
        source_command_id="cmd-1",
    )
    data = event.to_dict()
    assert set(data) == {
        "event_id",
        "event_type",
        "ticket",
        "position_id",
        "order_id",
        "deal_id",
        "symbol",
        "volume",
        "price",
        "sl",
        "tp",
        "commission",
        "swap",
        "profit",
        "execution_time",
        "broker_time",
        "magic",
        "broker_retcode",
        "retcode_label",
        "correlation_id",
        "source_command_id",
        "server_timestamp",
        "source",
        "detail",
        "account_login",
    }
    assert data["event_type"] == "deal_executed"
    assert data["server_timestamp"].endswith("Z")
    assert data["event_id"].startswith("evt-")


def test_dedup_key_is_type_plus_id_plus_execution_time():
    event = JournalEvent(
        event_type=JournalEventType.DEAL_EXECUTED,
        deal_id=101,
        execution_time="2026-11-16T10:00:00+00:00",
    )
    assert dedup_key(event) == ("deal_executed", "101", "2026-11-16T10:00:00+00:00")
    # Same deal id but a different type is a different event.
    other = JournalEvent(
        event_type=JournalEventType.POSITION_OPENED, deal_id=101, execution_time="2026-11-16T10:00:00+00:00"
    )
    assert dedup_key(other) != dedup_key(event)
    # No identifier and no time: never deduped.
    assert dedup_key(JournalEvent(event_type=JournalEventType.ORDER_SUBMITTED)) is None


def test_identified_events_get_deterministic_event_ids():
    first = JournalEvent(
        event_type=JournalEventType.DEAL_EXECUTED, deal_id=101, execution_time="2026-11-16T10:00:00+00:00"
    )
    second = JournalEvent(
        event_type=JournalEventType.DEAL_EXECUTED, deal_id=101, execution_time="2026-11-16T10:00:00+00:00"
    )
    assert first.event_id == second.event_id
    unidentified = JournalEvent(event_type=JournalEventType.ORDER_SUBMITTED)
    assert unidentified.event_id != JournalEvent(event_type=JournalEventType.ORDER_SUBMITTED).event_id


# ----------------------------------------------------------------------
# history mapping (pure builders)
# ----------------------------------------------------------------------


def test_history_order_filled_maps_to_order_accepted():
    events = events_from_history_order(_history_order())
    assert [e.event_type for e in events] == [JournalEventType.ORDER_ACCEPTED]
    event = events[0]
    assert event.order_id == 55 and event.ticket == 55
    assert event.symbol == "XAUUSD.a" and event.magic == 123
    assert event.execution_time == "2026-11-16T10:00:00+00:00"
    assert event.broker_time == "2026-11-16T10:00:00"


def test_history_order_rejected_and_canceled_pending():
    rejected = events_from_history_order(_history_order(state=5, state_label="REJECTED"))
    assert [e.event_type for e in rejected] == [JournalEventType.ORDER_REJECTED]
    canceled = events_from_history_order(_history_order(type=2, state=2, state_label="CANCELED"))
    assert [e.event_type for e in canceled] == [JournalEventType.PENDING_CANCELED]
    expired = events_from_history_order(_history_order(type=3, state=7, state_label="EXPIRED"))
    assert [e.event_type for e in expired] == [JournalEventType.PENDING_CANCELED]
    # PLACED/STARTED history orders are not events on their own.
    assert events_from_history_order(_history_order(state=1, state_label="PLACED")) == []
    # Ticket-less records are dropped (cannot be reconciled, §3.3 rule).
    assert events_from_history_order(_history_order(ticket=0)) == []


def test_deal_maps_to_exactly_one_deal_executed():
    events = events_from_deal(_deal())
    assert [e.event_type for e in events] == [JournalEventType.DEAL_EXECUTED]
    event = events[0]
    assert event.deal_id == 101 and event.order_id == 55 and event.position_id == 7
    assert event.volume == 0.1 and event.price == 2650.5 and event.commission == -1.5
    assert event.execution_time == "2026-11-16T10:00:00+00:00"


def test_non_trade_deals_emit_no_journal_trade_event():
    # BALANCE / CREDIT / COMMISSION deals are accounting facts, not trades.
    for deal_type in (2, 3, 4, 5, 6, 7, 12):
        assert events_from_deal(_deal(type=deal_type)) == []
    assert events_from_deal(_deal(ticket=0)) == []


def test_closed_position_full_vs_partial_close():
    full = events_from_closed_position(_closed_position())
    assert [e.event_type for e in full] == [JournalEventType.POSITION_FULLY_CLOSED]
    assert full[0].position_id == 7 and full[0].volume == 0.2 and full[0].profit == 12.5
    partial = events_from_closed_position(_closed_position(volume_out=0.1))
    assert [e.event_type for e in partial] == [JournalEventType.POSITION_PARTIALLY_CLOSED]
    assert events_from_closed_position(_closed_position(position_id=0)) == []


# ----------------------------------------------------------------------
# collector: bounded ranges + dedup
# ----------------------------------------------------------------------


def test_same_range_synced_twice_yields_no_duplicates():
    collector = JournalCollector()
    first = collector.collect_from_history(
        orders=[_history_order()],
        deals=[_deal()],
        closed_positions=[_closed_position()],
        from_dt=FROM,
        to_dt=TO,
    )
    assert len(first) == 3  # accepted + deal executed + fully closed
    second = collector.collect_from_history(
        orders=[_history_order()],
        deals=[_deal()],
        closed_positions=[_closed_position()],
        from_dt=FROM,
        to_dt=TO,
    )
    assert second == []
    assert collector.duplicates_dropped == 3
    assert len(collector.events()) == 3


def test_bounded_range_fetches_only_that_range():
    seen: list[tuple[datetime, datetime]] = []

    def fetch_orders(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        seen.append((from_dt, to_dt))
        return {"orders": [_history_order()], "deals": [_deal()]}

    def fetch_positions(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        seen.append((from_dt, to_dt))
        return {"positions": [_closed_position()]}

    collector = JournalCollector()
    events = collector.collect_range(
        FROM, TO, fetch_history_orders=fetch_orders, fetch_history_positions=fetch_positions
    )
    assert len(events) == 3
    # The fetchers receive the EXACT requested bounds (never widened).
    assert seen == [(FROM, TO), (FROM, TO)]
    # Records outside the range are dropped, never emitted.
    old_deal = _deal(time_utc="2026-11-15T10:00:00")
    dropped = collector.collect_from_history(deals=[old_deal], from_dt=FROM, to_dt=TO)
    assert dropped == []
    assert collector.out_of_range_dropped == 1


def test_collect_requires_explicit_bounds():
    collector = JournalCollector()
    with pytest.raises(ValueError):
        collector.collect_from_history(deals=[_deal()], from_dt=TO, to_dt=FROM)
    with pytest.raises(ValueError):
        collector.collect_range(TO, FROM, fetch_history_orders=lambda a, b: {}, fetch_history_positions=lambda a, b: {})


def test_collector_advances_shared_checkpoint_never_backwards():
    checkpoint = SyncCheckpoint()
    persisted: list[str] = []
    collector = JournalCollector(checkpoint=checkpoint, persist_checkpoint=lambda: persisted.append(checkpoint.iso))
    collector.collect_from_history(deals=[_deal()], from_dt=FROM, to_dt=TO)
    assert checkpoint.value == datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    assert persisted == [checkpoint.iso]
    # An older range re-collected later must not move the checkpoint back.
    collector.collect_from_history(
        deals=[_deal(time_utc="2026-11-16T09:00:00", ticket=102)],
        from_dt=FROM,
        to_dt=TO,
    )
    assert checkpoint.value == datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


def test_seen_set_is_bounded_oldest_evicted_first():
    collector = JournalCollector(max_seen=2)
    collector.collect_from_history(deals=[_deal(ticket=1)], from_dt=FROM, to_dt=TO)
    collector.collect_from_history(deals=[_deal(ticket=2)], from_dt=FROM, to_dt=TO)
    collector.collect_from_history(deals=[_deal(ticket=3)], from_dt=FROM, to_dt=TO)
    assert collector.seen_count == 2
    # Ticket 1 was evicted, so it is emitted again on re-collection.
    again = collector.collect_from_history(deals=[_deal(ticket=1)], from_dt=FROM, to_dt=TO)
    assert len(again) == 1


def test_event_store_is_bounded():
    collector = JournalCollector(max_events=2)
    for ticket in (1, 2, 3):
        collector.collect_from_history(deals=[_deal(ticket=ticket)], from_dt=FROM, to_dt=TO)
    assert [e.deal_id for e in collector.events()] == [2, 3]


# ----------------------------------------------------------------------
# snapshot diffs
# ----------------------------------------------------------------------


def test_first_snapshot_is_baseline_and_emits_nothing():
    collector = JournalCollector()
    assert collector.diff_snapshots(None, None, [_position()], [_order()]) == []


def test_snapshot_diff_open_modify_pending_create_modify():
    collector = JournalCollector()
    collector.diff_snapshots([], [], [_position()], [_order()])
    opened = collector.diff_snapshots([_position()], [_order()], [_position()], [_order()])
    assert opened == []  # no change, no event
    modified = collector.diff_snapshots([_position()], [_order()], [_position(sl=2630.0)], [_order(price_open=2610.0)])
    assert {e.event_type for e in modified} == {JournalEventType.POSITION_MODIFIED, JournalEventType.PENDING_MODIFIED}
    created = collector.diff_snapshots(
        [_position()], [_order()], [_position(), _position(ticket=9)], [_order(), _order(ticket=99)]
    )
    assert {e.event_type for e in created} == {JournalEventType.POSITION_OPENED, JournalEventType.PENDING_CREATED}


def test_vanished_position_without_deal_emits_no_close_event():
    # The source-of-truth rule: a disappearance alone is not a close.
    collector = JournalCollector()
    collector.diff_snapshots([], [], [_position()], [])
    assert collector.diff_snapshots([_position()], [], [], []) == []
    # With a corroborating OUT deal, the disappearance IS a full close.
    out_deal = _deal(entry=1, position_id=7, time_utc="2026-11-16T12:00:00")
    closed = collector.diff_snapshots([_position()], [], [], [], deals=[out_deal])
    assert [e.event_type for e in closed] == [JournalEventType.POSITION_FULLY_CLOSED]
    assert closed[0].position_id == 7 and closed[0].deal_id == 101


def test_volume_drop_without_deal_is_a_modification_not_a_close():
    collector = JournalCollector()
    collector.diff_snapshots([], [], [_position()], [])
    events = collector.diff_snapshots([_position()], [], [_position(volume=0.1)], [])
    assert [e.event_type for e in events] == [JournalEventType.POSITION_MODIFIED]
    assert "NOT asserted" in (events[0].detail or "")


def test_volume_drop_with_out_deal_is_a_partial_close():
    collector = JournalCollector()
    collector.diff_snapshots([], [], [_position()], [])
    out_deal = _deal(entry=1, position_id=7, volume=0.1, time_utc="2026-11-16T12:00:00")
    events = collector.diff_snapshots([_position()], [], [_position(volume=0.1)], [], deals=[out_deal])
    assert [e.event_type for e in events] == [JournalEventType.POSITION_PARTIALLY_CLOSED]
    assert events[0].volume == pytest.approx(0.1)


# ----------------------------------------------------------------------
# execution responses & connection events
# ----------------------------------------------------------------------


def test_execution_response_done_accepts_and_errors_reject():
    collector = JournalCollector()
    ok = collector.record_execution_response(broker_retcode=10009, ticket=55, symbol="XAUUSD.a")
    assert [e.event_type for e in ok] == [
        JournalEventType.EXECUTION_RESPONSE_RECEIVED,
        JournalEventType.ORDER_ACCEPTED,
    ]
    assert ok[0].retcode_label is not None and "DONE" in ok[0].retcode_label
    failed = collector.record_execution_response(broker_retcode=10004, ticket=56, symbol="XAUUSD.a")
    assert [e.event_type for e in failed] == [
        JournalEventType.EXECUTION_RESPONSE_RECEIVED,
        JournalEventType.BROKER_ERROR,
        JournalEventType.ORDER_REJECTED,
    ]
    assert failed[1].broker_retcode == 10004


def test_order_submitted_never_implies_a_fill():
    collector = JournalCollector()
    event = collector.record_order_submitted(symbol="XAUUSD.a", volume=0.1)
    assert event is not None and event.event_type == JournalEventType.ORDER_SUBMITTED
    assert event.deal_id is None and event.position_id is None
    # A second identical submission (no ticket yet) is a distinct event.
    again = collector.record_order_submitted(symbol="XAUUSD.a", volume=0.1)
    assert again is not None and again.event_id != event.event_id


def test_terminal_and_account_connection_events():
    collector = JournalCollector()
    lost = collector.record_terminal_connection_lost(detail="terminal_info().connected is False")
    assert lost is not None and lost.event_type == JournalEventType.TERMINAL_CONNECTION_LOST
    changed = collector.record_account_connection_changed(login=123456, detail="login changed 111 -> 123456")
    assert changed is not None and changed.event_type == JournalEventType.ACCOUNT_CONNECTION_CHANGED
    assert changed.account_login == 123456


def test_reset_clears_dedup_state_for_account_switch():
    collector = JournalCollector()
    collector.collect_from_history(deals=[_deal()], from_dt=FROM, to_dt=TO)
    collector.reset()
    assert collector.events() == [] and collector.seen_count == 0
    again = collector.collect_from_history(deals=[_deal()], from_dt=FROM, to_dt=TO)
    assert len(again) == 1  # same deal re-emitted for the new account


# ----------------------------------------------------------------------
# sync wiring (Phase C3: journal + labelling inside existing batches)
# ----------------------------------------------------------------------

from nse_bridge.journal.accounting import LOCALLY_DERIVED, MT5_SUPPLIED, NOT_REPORTED
from nse_bridge.server.rest import SyncAck
from nse_bridge.server.sync import SyncEngine, _history_pages


class _FakeRest:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_batch(self, batch: dict[str, Any]) -> SyncAck:
        self.sent.append(batch)
        return SyncAck(accepted=True, batch_id=batch["batch_id"])


def _sync_engine(rest=None, **overrides):
    rest = rest or _FakeRest()

    async def _sleep(delay: float) -> None:
        return None

    engine = SyncEngine(rest, None, sleep=_sleep, **overrides)
    return engine


def test_account_items_carries_provenance_map():
    from types import SimpleNamespace

    acc = SimpleNamespace(
        login=123456,
        server="Broker-Demo",
        name="Trader",
        trade_mode=0,
        currency="USD",
        balance=10000.0,
        equity=10012.5,
        margin=100.0,
        margin_free=9912.5,
        profit=12.5,
        credit=0.0,
    )
    items = SyncEngine.account_items(acc, {"state": "CONNECTED"})
    assert items["provenance"]["balance"] == MT5_SUPPLIED
    assert items["provenance"]["equity"] == MT5_SUPPLIED
    assert items["provenance"]["credit"] == MT5_SUPPLIED
    assert items["provenance"]["type"] == LOCALLY_DERIVED
    assert items["provenance"]["realized_profit"] == NOT_REPORTED
    # The account values themselves are untouched by the labelling.
    assert items["account"]["balance"] == 10000.0


def test_history_pages_carry_provenance_and_balance_operations():
    pages = _history_pages(
        "history_orders",
        {
            "orders": [_history_order()],
            "deals": [_deal(type=2, profit=500.0), _deal(ticket=102, type=0)],
        },
        500,
    )
    assert len(pages) == 2
    orders_page, deals_page = pages
    assert orders_page["provenance"]["orders"] == MT5_SUPPLIED
    assert orders_page["balance_operations"] == {
        "deposits": [],
        "withdrawals": [],
        "balance_adjustments": [],
        "fees": [],
        "credits": [],
    }
    assert [d["deal_id"] for d in deals_page["balance_operations"]["deposits"]] == [101]
    assert deals_page["provenance"]["balance_operations"] == MT5_SUPPLIED
    positions_pages = _history_pages("history_positions", {"positions": [_closed_position()]}, 500)
    assert positions_pages[0]["provenance"]["positions"] == LOCALLY_DERIVED


async def test_sync_history_feeds_journal_and_second_sync_emits_nothing_new():
    rest = _FakeRest()
    journal = JournalCollector()

    async def history_orders(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        return {"orders": [_history_order()], "deals": [_deal()]}

    async def history_positions(from_dt: datetime, to_dt: datetime) -> dict[str, Any]:
        return {"positions": [_closed_position()]}

    engine = _sync_engine(rest, history_orders=history_orders, history_positions=history_positions, journal=journal)
    await engine.sync_history()
    assert len(journal.events()) == 3
    # Orders page + deals page + one closed-positions page.
    assert [b["kind"] for b in rest.sent] == ["history_orders", "history_orders", "history_positions"]
    # The deals page carries the balance surfacing (here: no balance ops).
    deals_batches = [b for b in rest.sent if b["kind"] == "history_orders" and b["items"]["deals"]]
    assert deals_batches[0]["items"]["balance_operations"]["deposits"] == []
    # A second sync over the SAME range re-sends batches (server dedups
    # by batch/ticket) but emits NO new journal events.
    engine.checkpoint.reset()  # force the same [epoch, now] range again
    await engine.sync_history()
    assert len(journal.events()) == 3
    assert journal.duplicates_dropped == 3


async def test_send_positions_diffs_snapshots_into_journal():
    rest = _FakeRest()
    journal = JournalCollector()
    snapshots = [
        {"positions": [_position()], "orders": [_order()]},
        {"positions": [_position(sl=2630.0)], "orders": [_order()]},
    ]
    calls = {"n": 0}

    async def positions_snapshot() -> dict[str, Any]:
        item = snapshots[min(calls["n"], 1)]
        calls["n"] += 1
        return item

    engine = _sync_engine(rest, positions_snapshot=positions_snapshot, journal=journal)
    await engine.send_positions()  # baseline: no events
    assert journal.events() == []
    await engine.send_positions()  # SL changed: one modification event
    assert [e.event_type for e in journal.events()] == [JournalEventType.POSITION_MODIFIED]
    assert engine.journal_events()[0]["event_type"] == "position_modified"


async def test_account_switch_resets_journal():
    rest = _FakeRest()
    journal = JournalCollector()
    journal.collect_from_history(deals=[_deal()], from_dt=FROM, to_dt=TO)
    engine = _sync_engine(rest, journal=journal)
    engine.reset_for_account_switch()
    assert journal.events() == [] and journal.seen_count == 0
