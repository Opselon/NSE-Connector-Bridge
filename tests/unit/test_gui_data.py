"""Unit tests for the C4 GUI data endpoints (spec §17).

Label: unit. No MT5 — a fake MT5 module is injected into the driver.
Covers: the Symbols inventory/resolve/refresh endpoints (C1 data via
the shared resolver), the Execution recent-commands endpoint (the
executor's bounded recent history), and the journal-events endpoint
(C3 collector via the sync engine). Also covers the executor's recent
history itself (true local lifecycle states, incl. PARTIALLY_COMPLETED).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.commands import (
    CommandExecutor,
    CommandState,
    CommandValidator,
)
from nse_bridge.server.queue import BoundedPendingQueue
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
def _row(name: str, **overrides: Any) -> SimpleNamespace:
    fields = {
        "name": name,
        "description": f"{name} description",
        "path": f"Test\\{name}",
        "visible": True,
        "selected": True,
        "trade_mode": 4,
        "digits": 5,
        "point": 0.00001,
        "spread": 10,
        "spread_float": 1.0,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "trade_tick_value_loss": 1.0,
        "trade_contract_size": 100000.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_stops_level": 10,
        "trade_freeze_level": 0,
        "trade_exemode": 2,
        "trade_calc_mode": 0,
        "filling_mode": 3,
        "swap_mode": 1,
        "swap_long": 0.5,
        "swap_short": -0.7,
        "swap_rollover3days": 3,
        "currency_base": None,
        "currency_profit": None,
        "currency_margin": None,
        "margin_initial": 1000.0,
        "margin_maintenance": 500.0,
        "margin_hedged": 250.0,
        "order_gtc_mode": 0,
        "start_time": 0,
        "expiration_time": 0,
        "session_deals": 12,
        "session_turnover": 345.0,
        "session_open": 1.10,
        "session_close": 1.11,
        "session_volume": 7.5,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _FakeMt5:
    def __init__(self, rows):
        self._rows = list(rows)

    def symbols_get(self):
        return list(self._rows)

    def symbol_info(self, name):
        for row in self._rows:
            if row.name == name:
                return row
        return None

    def symbol_select(self, name, enable=True):
        return True

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass


class _FakeChannel:
    def __init__(self, payload):
        self._payload = payload

    def recent_command_executions(self, limit=50):
        data = dict(self._payload)
        data["executions"] = list(data.get("executions", []))[:limit]
        return data


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NSE_MT5_TERMINAL_PATH", raising=False)


@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx, app, driver
    driver.disconnect()


def _auth(ctx):
    return {"X-API-Key": ctx.settings.local_api_key}


def _connect(ctx, driver, rows):
    driver._mt5 = _FakeMt5(rows)
    driver._connected = True


# ----------------------------------------------------------------------
# Symbols inventory
# ----------------------------------------------------------------------
def test_symbols_inventory_reports_verbatim_names_metadata_availability(client):
    http, ctx, _, driver = client
    _connect(
        ctx,
        driver,
        [
            _row("EURUSD"),
            _row(
                "XAUUSD.a",
                visible=False,
                currency_base="XAU",
                currency_profit="USD",
                trade_contract_size=100.0,
                digits=2,
            ),
            _row("DEAD", trade_mode=0),
        ],
    )
    resp = http.get("/api/v1/symbols/inventory", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    by_name = {s["metadata"]["name"]: s for s in data["symbols"]}
    assert set(by_name) == {"EURUSD", "XAUUSD.a", "DEAD"}  # verbatim broker names
    eurusd = by_name["EURUSD"]
    assert eurusd["metadata"]["digits"] == 5
    assert eurusd["metadata"]["contract_size"] == 100000.0
    assert eurusd["metadata"]["execution_mode_label"] == "MARKET"
    assert eurusd["metadata"]["calculation_mode_label"] == "FOREX"
    assert eurusd["availability"]["tradable"] is True
    assert eurusd["availability"]["fully_tradable"] is True
    xau = by_name["XAUUSD.a"]
    assert xau["metadata"]["visible"] is False
    assert xau["metadata"]["currency_base"] == "XAU"
    assert xau["availability"]["tradable"] is True
    assert by_name["DEAD"]["availability"]["tradable"] is False
    assert by_name["DEAD"]["availability"]["reason"]  # reason stated, never empty
    assert data["summary"]["total"] == 3
    assert data["summary"]["visible"] == 2
    assert data["summary"]["hidden"] == 1
    assert data["summary"]["tradable"] == 2
    assert data["summary"]["loaded_at"] is not None


def test_symbols_inventory_disconnected_is_honest_error(client):
    http, ctx, _, _ = client
    resp = http.get("/api/v1/symbols/inventory", headers=_auth(ctx))
    assert resp.json()["ok"] is False
    assert resp.json()["error"]["code"] == "MT5_NOT_CONNECTED"


def test_symbols_resolve_resolved_ambiguous_not_found(client):
    http, ctx, _, driver = client
    _connect(
        ctx,
        driver,
        [
            _row("XAUUSD.a", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0),
            _row("XAUUSDm", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0, digits=2),
        ],
    )
    # Two candidates sharing a name core: ambiguous, never guessed.
    resp = http.post("/api/v1/symbols/resolve", json={"symbol": "XAU/USD"}, headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "ambiguous"
    assert data["exact_symbol"] is None
    assert {c["name"] for c in data["candidates"]} == {"XAUUSD.a", "XAUUSDm"}
    assert data["reason"]
    # expected metadata that leaves exactly one candidate resolves it.
    resp = http.post(
        "/api/v1/symbols/resolve",
        json={"symbol": "XAU/USD", "expected": {"digits": 2}},
        headers=_auth(ctx),
    )
    assert resp.json()["data"]["status"] == "resolved"
    assert resp.json()["data"]["exact_symbol"] == "XAUUSDm"
    # Unknown canonical ID: not_found, never a verbatim fallback.
    resp = http.post("/api/v1/symbols/resolve", json={"symbol": "XXX/YYY"}, headers=_auth(ctx))
    assert resp.json()["data"]["status"] == "not_found"
    assert resp.json()["data"]["exact_symbol"] is None


def test_symbols_resolve_single_candidate_resolves(client):
    http, ctx, _, driver = client
    _connect(
        ctx,
        driver,
        [_row("XAUUSD.a", currency_base="XAU", currency_profit="USD", trade_contract_size=100.0)],
    )
    resp = http.post("/api/v1/symbols/resolve", json={"symbol": "XAU/USD"}, headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "resolved"
    assert data["exact_symbol"] == "XAUUSD.a"
    assert data["canonical_id"] == "XAU/USD"


def test_symbols_resolve_unknown_expected_key_is_422(client):
    http, ctx, _, driver = client
    _connect(ctx, driver, [_row("EURUSD")])
    resp = http.post(
        "/api/v1/symbols/resolve",
        json={"symbol": "EUR/USD", "expected": {"not_a_field": 1}},
        headers=_auth(ctx),
    )
    assert resp.status_code == 422, resp.text


def test_symbols_refresh_re_reads_the_broker_list(client):
    http, ctx, _, driver = client
    fake = _FakeMt5([_row("EURUSD")])
    driver._mt5 = fake
    driver._connected = True
    resp = http.get("/api/v1/symbols/inventory", headers=_auth(ctx))
    assert resp.json()["data"]["summary"]["total"] == 1
    # Broker now reports a second symbol; refresh must pick it up.
    fake._rows.append(_row("GBPUSD"))
    resp = http.post("/api/v1/symbols/refresh", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["summary"]["total"] == 2
    resp = http.get("/api/v1/symbols/inventory", headers=_auth(ctx))
    assert {s["metadata"]["name"] for s in resp.json()["data"]["symbols"]} == {"EURUSD", "GBPUSD"}


# ----------------------------------------------------------------------
# Execution recent + journal events
# ----------------------------------------------------------------------
def test_execution_recent_without_channel_is_empty_not_fabricated(client):
    http, ctx, app, _ = client
    assert getattr(app.state, "channel", None) is None  # lifespan never runs in tests
    resp = http.get("/api/v1/execution/recent", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {
        "executions": [],
        "pending_results": 0,
        "executor_available": False,
        "channel_available": False,
    }


def test_execution_recent_reports_channel_data(client):
    http, ctx, app, _ = client
    app.state.channel = _FakeChannel(
        {
            "executions": [
                {
                    "command_id": "cmd-1",
                    "operation": "close_position",
                    "state": "PARTIALLY_COMPLETED",
                    "wire_status": "executed",
                    "partial": True,
                    "closed_volume": 0.4,
                    "remaining_volume": 0.6,
                    "reconciled": True,
                }
            ],
            "pending_results": 2,
            "executor_available": True,
        }
    )
    resp = http.get("/api/v1/execution/recent", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["channel_available"] is True
    assert data["pending_results"] == 2
    assert data["executions"][0]["state"] == "PARTIALLY_COMPLETED"
    assert data["executions"][0]["partial"] is True


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


def test_journal_events_without_sync_engine_is_empty(client):
    http, ctx, _, _ = client
    assert ctx.sync_engine is None
    resp = http.get("/api/v1/journal/events", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data == {"events": [], "sync_available": False, "total": 0}


def test_journal_events_newest_first_verbatim_symbol_null_profit(client):
    from nse_bridge.journal.collector import JournalCollector
    from nse_bridge.server.sync import SyncEngine

    http, ctx, _, _ = client
    collector = JournalCollector()
    from_dt = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    to_dt = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    collector.collect_from_history(
        orders=[_history_order()],
        deals=[_deal()],
        closed_positions=[_closed_position()],
        from_dt=from_dt,
        to_dt=to_dt,
    )
    ctx.sync_engine = SyncEngine(SimpleNamespace(), ctx, journal=collector)
    resp = http.get("/api/v1/journal/events", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["sync_available"] is True
    assert data["total"] == 3
    events = data["events"]
    assert len(events) == 3
    # Newest first: the fully-closed position event is the latest.
    assert events[0]["event_type"] == "position_fully_closed"
    assert all(ev["symbol"] == "XAUUSD.a" for ev in events)  # verbatim broker name
    # The order event carries no profit — null means "not reported", never zero.
    order_event = next(ev for ev in events if ev["event_type"] == "order_accepted")
    assert order_event["profit"] is None
    # The deal event carries the broker's real profit.
    deal_event = next(ev for ev in events if ev["event_type"] == "deal_executed")
    assert deal_event["profit"] == 0.0
    # limit slices the newest N.
    resp = http.get("/api/v1/journal/events?limit=1", headers=_auth(ctx))
    assert len(resp.json()["data"]["events"]) == 1
    assert resp.json()["data"]["events"][0]["event_type"] == "position_fully_closed"


# ----------------------------------------------------------------------
# Executor recent history (the Execution screen's lifecycle source)
# ----------------------------------------------------------------------
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DEVICE = "dev-1"
ACCOUNT = "12345678901"


def _frame(**overrides):
    issued = NOW - timedelta(minutes=1)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "same-as-command-id",
    }
    frame.update(overrides)
    if "operation" in overrides and isinstance(overrides["operation"], dict):
        frame["operation"] = overrides["operation"]
    return frame


def _validator(**overrides):
    kwargs = {
        "get_account_id": lambda: ACCOUNT,
        "entitlements": Entitlements(position_manager=True, auto_trade=True),
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return CommandValidator(DEVICE, **kwargs)


def _executor_ctx(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


def test_executor_recent_records_completed_and_rejected(tmp_path, monkeypatch):
    ctx = _executor_ctx(tmp_path)
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    executor = CommandExecutor(
        ctx, _validator(), results_queue=BoundedPendingQueue(32, "test/results"), clock=lambda: NOW
    )
    assert executor.recent_executions() == []
    execution, _ = executor.execute(_frame())
    assert execution.state is CommandState.COMPLETED
    recent = executor.recent_executions()
    assert len(recent) == 1
    assert recent[0]["operation"] == "close_position"
    assert recent[0]["state"] == "COMPLETED"
    assert recent[0]["wire_status"] == "executed"
    assert recent[0]["partial"] is False
    assert [step[0] for step in recent[0]["history"]] == [
        "RECEIVED",
        "VALIDATED",
        "AUTHORIZED",
        "DISPATCHED",
        "EXECUTING",
        "COMPLETED",
    ]
    # A rejected command (unsupported op) is recorded as FAILED, never executed.
    execution, _result = executor.execute(_frame(operation={"type": "open_order", "ticket": "1"}))
    assert execution.state is CommandState.FAILED
    recent = executor.recent_executions()
    assert len(recent) == 2
    assert recent[0]["operation"] == "open_order"
    assert recent[0]["state"] == "FAILED"
    assert recent[0]["wire_status"] == "rejected"
    assert recent[0]["error_code"] == "UnsupportedOperation"


def test_executor_recent_records_partially_completed_with_partial_facts(tmp_path, monkeypatch):
    ctx = _executor_ctx(tmp_path)
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket: {
            "status": "done",
            "ticket": ticket,
            "closed_volume": 0.4,
            "remaining_volume": 0.6,
            "partial": True,
            "reconciled": True,
        },
    )
    executor = CommandExecutor(
        ctx, _validator(), results_queue=BoundedPendingQueue(32, "test/results"), clock=lambda: NOW
    )
    execution, _result = executor.execute(_frame())
    assert execution.state is CommandState.PARTIALLY_COMPLETED
    recent = executor.recent_executions()
    assert len(recent) == 1
    assert recent[0]["state"] == "PARTIALLY_COMPLETED"
    assert recent[0]["wire_status"] == "executed"  # §6.5 has no partial status (D5)
    assert recent[0]["partial"] is True
    assert recent[0]["closed_volume"] == 0.4
    assert recent[0]["remaining_volume"] == 0.6
    assert recent[0]["reconciled"] is True


def test_executor_recent_is_bounded_and_newest_first(tmp_path, monkeypatch):
    ctx = _executor_ctx(tmp_path)
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    executor = CommandExecutor(
        ctx, _validator(), results_queue=BoundedPendingQueue(32, "test/results"), clock=lambda: NOW
    )
    ids = []
    for _ in range(55):
        frame = _frame()
        ids.append(frame["command_id"])
        executor.execute(frame)
    recent = executor.recent_executions()
    assert len(recent) == 50  # bounded at 50, oldest evicted
    assert recent[0]["command_id"] == ids[-1]  # newest first
    assert recent[-1]["command_id"] == ids[5]
    assert executor.recent_executions(limit=2)[0]["command_id"] == ids[-1]
