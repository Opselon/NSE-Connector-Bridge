"""Policy-move reporting (audit P1-8).

A synthetic envelope drives a move and the record is persisted in
the durable store AND retrievable via the local API; no envelope →
no move, no record. No MT5 — a fake MT5 module is injected into the
driver (same harness as ``test_policy_envelope.py``).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nse_bridge.api.management import router as management_router
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.reliability.policy_move_store import PolicyMoveStore
from nse_bridge.server.policy import PolicyService

pytestmark = pytest.mark.unit

ACCOUNT = "123456"


class _FakeMt5:
    TRADE_ACTION_DEAL = 0
    TRADE_ACTION_PENDING = 1
    TRADE_ACTION_SLTP = 2
    TRADE_ACTION_MODIFY = 3
    TRADE_ACTION_REMOVE = 4
    TRADE_ACTION_CLOSE_BY = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 1

    def __init__(self, positions=None):
        self.positions = {p.ticket: p for p in (positions or [])}
        self.sent_requests: list[dict] = []
        self.send_calls = 0

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(int(ticket))
            return [p] if p is not None else []
        return list(self.positions.values())

    def orders_get(self, ticket=None, symbol=None):
        return []

    def symbol_info(self, name):
        return SimpleNamespace(name=name, point=0.00001, digits=5, trade_tick_size=0.00001, filling_mode=3)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(time.time()), bid=1.1050, ask=1.1052, last=1.1051, volume=10, flags=0)

    def account_info(self):
        return SimpleNamespace(login=123456, trade_mode=0, margin_mode=2)

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    def order_check(self, request):
        return SimpleNamespace(
            retcode=0,
            balance=1.0,
            equity=1.0,
            profit=0.0,
            margin=0.0,
            margin_free=1.0,
            margin_level=0.0,
            comment="Done",
            request=dict(request),
        )

    def order_send(self, request):
        self.sent_requests.append(dict(request))
        self.send_calls += 1
        if request.get("action") == self.TRADE_ACTION_SLTP:
            self.positions[request["position"]].sl = float(request["sl"])
            self.positions[request["position"]].tp = float(request["tp"])
        return SimpleNamespace(retcode=10009, order=0, deal=0, price=0.0, volume=0.0, comment="")


def _position(ticket=9000001, symbol="EURUSD", type_=0, volume=0.1, sl=0.0, tp=0.0):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type_,
        volume=volume,
        price_open=1.1000,
        price_current=1.1050,
        sl=sl,
        tp=tp,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=888101,
        comment="",
        time=int(time.time()) - 60,
    )


def _ctx(fake, tmp_path) -> BridgeContext:
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"), magic=888101)
    driver = Mt5Driver(settings)
    driver._mt5 = fake
    driver._connected = True
    driver._login = 123456
    ctx = BridgeContext(settings=settings, driver=driver)
    ctx.server_channel_state["connected"] = True
    return ctx


def _envelope_frame(*, version=7, minutes=4, rules=None):
    issued = datetime.now(UTC) - timedelta(minutes=1)
    return {
        "account_id": ACCOUNT,
        "version": version,
        "bounds": {},
        "rules": rules
        if rules is not None
        else [{"rule_id": "trail-1", "params": {"kind": "trailing", "distance_points": 150, "step_points": 10}}],
        "risk_free_policy": {},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
    }


def asyncio_run(coro):
    return asyncio.run(coro)


class _CapturingTelemetry:
    """Fake telemetry: records warn/info calls (never queues)."""

    def __init__(self):
        self.warned: list[tuple[str, dict]] = []
        self.informed: list[tuple[str, dict]] = []

    def warn(self, event_type, **kwargs):
        self.warned.append((event_type, kwargs))

    def info(self, event_type, **kwargs):
        self.informed.append((event_type, kwargs))


# ----------------------------------------------------------------------
# The durable store itself
# ----------------------------------------------------------------------
def test_store_persists_and_lists_newest_first(tmp_path):
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    assert not (tmp_path / "policy_moves.jsonl").exists()  # lazy: no file at construction
    record = {
        "decision_id": "01d1",
        "rule_id": "trail-1",
        "envelope_version": 7,
        "kind": "trailing",
        "ticket": 9000001,
        "symbol": "EURUSD",
        "sl_from": 1.09,
        "sl_to": 1.1035,
        "tp_from": None,
        "tp_to": None,
        "status": "executed",
        "started_at": "2026-11-28T00:00:00Z",
        "completed_at": "2026-11-28T00:00:01Z",
    }
    assert store.append(record) is True
    assert (tmp_path / "policy_moves.jsonl").is_file()
    assert store.list_moves() == [record]
    # A new instance on the same path reloads the record (restart-proof).
    assert PolicyMoveStore(tmp_path / "policy_moves.jsonl").list_moves() == [record]


def test_store_filters_and_bounds(tmp_path):
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl", max_items=3)
    for i, kind in enumerate(("trailing", "breakeven", "trailing_tp", "trailing")):
        store.append(
            {
                "decision_id": f"01d{i}",
                "rule_id": f"r-{i}",
                "envelope_version": 7,
                "kind": kind,
                "ticket": 9000001 + i,
                "symbol": "EURUSD",
                "status": "executed",
            }
        )
    # Bound evicts the oldest (decision 01d0) with a warning, never silently.
    assert [r["decision_id"] for r in store.list_moves(limit=10)] == ["01d3", "01d2", "01d1"]
    assert [r["decision_id"] for r in store.list_moves(kind="trailing_tp")] == ["01d2"]
    assert [r["decision_id"] for r in store.list_moves(ticket=9000002)] == ["01d1"]
    assert store.list_moves(status="failed") == []
    assert len(store.list_moves(limit=1)) == 1


def test_store_skips_corrupt_lines_and_refuses_invalid(tmp_path):
    path = tmp_path / "policy_moves.jsonl"
    path.write_text(
        '{"decision_id": "01ok", "rule_id": "r", "ticket": 1, "status": "executed"}\n'
        "not json at all\n"
        '{"decision_id": "", "rule_id": "r", "ticket": 1}\n',
        encoding="utf-8",
    )
    store = PolicyMoveStore(path)
    assert [r["decision_id"] for r in store.list_moves()] == ["01ok"]
    assert store.append({"rule_id": "r"}) is False  # no decision_id → refused
    assert store.append("not a dict") is False


# ----------------------------------------------------------------------
# Executor → store (mock MT5)
# ----------------------------------------------------------------------
def test_executed_trailing_move_is_persisted(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, move_store=store)
    service.handle_envelope_frame(_envelope_frame())
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(records) == 1 and records[0].status == "executed"
    stored = store.list_moves()
    assert len(stored) == 1
    move = stored[0]
    assert move["rule_id"] == "trail-1"
    assert move["decision_id"] == records[0].decision_id
    assert move["envelope_version"] == 7
    assert move["kind"] == "trailing"
    assert move["ticket"] == 9000001
    assert move["symbol"] == "EURUSD"
    assert move["sl_from"] == pytest.approx(1.0900)
    assert move["sl_to"] == pytest.approx(1.1035)
    assert move["tp_from"] is None and move["tp_to"] is None
    assert move["status"] == "executed"
    assert move["started_at"] and move["completed_at"]


def test_executed_trailing_tp_move_carries_tp_not_sl(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900, tp=1.0900)])
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, move_store=store)
    service.handle_envelope_frame(
        _envelope_frame(
            rules=[{"rule_id": "tp-1", "params": {"kind": "trailing_tp", "distance_points": 150, "step_points": 10}}]
        )
    )
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(records) == 1 and records[0].status == "executed"
    move = store.list_moves()[0]
    assert move["kind"] == "trailing_tp"
    assert move["tp_from"] == pytest.approx(1.0900)
    assert move["tp_to"] == pytest.approx(1.1065)
    assert move["sl_from"] is None and move["sl_to"] is None


def test_failed_move_is_persisted(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    fake.symbol_info = lambda name: SimpleNamespace(
        name=name, point=0.0, digits=5, trade_tick_size=0.00001, filling_mode=3
    )  # unknown point → the move fails before MT5
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, move_store=store)
    service.handle_envelope_frame(_envelope_frame())
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(records) == 1 and records[0].status == "failed"
    assert fake.send_calls == 0
    move = store.list_moves()[0]
    assert move["status"] == "failed"
    assert move["error_code"] == "POLICY_POINT_UNKNOWN"
    assert move["ticket"] == 9000001


def test_no_envelope_means_no_move_no_record(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, move_store=store)
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []
    assert fake.send_calls == 0
    assert store.list_moves() == []
    assert not (tmp_path / "policy_moves.jsonl").exists()  # nothing persisted, no file


def test_refused_noop_is_not_persisted(tmp_path):
    # SL already tighter than the trailing target: a mechanical no-op.
    # It stays in the in-memory history (traceability) but must NOT
    # fill the durable ledger — it is the steady state of the poll loop.
    fake = _FakeMt5(positions=[_position(sl=1.1049)])
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, move_store=store)
    service.handle_envelope_frame(_envelope_frame())
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []  # no-op returns no record to the caller
    assert fake.send_calls == 0
    assert store.list_moves() == []
    assert len(service.executor.history) == 1  # …but the history still shows it
    assert service.executor.history[0]["denial_reason"] == "no_tighter_stop"


# ----------------------------------------------------------------------
# Telemetry delivery (CONTRACT §5.3 move-report event)
# ----------------------------------------------------------------------
def test_executed_move_is_emitted_as_warn_with_decision_id(tmp_path):
    # INFO events are never queued for delivery (telemetry.py binding
    # rule) — an executed move MUST go out as WARN, with the
    # decision_id as correlation_id so the queue dedup key is unique
    # per move and two moves can never collapse into one entry.
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    telemetry = _CapturingTelemetry()
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    service = PolicyService(ctx, telemetry=telemetry, move_store=store)
    service.handle_envelope_frame(_envelope_frame())
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records[0].status == "executed"
    assert telemetry.informed == []  # nothing about a move goes out as INFO
    assert len(telemetry.warned) == 1
    event_type, kwargs = telemetry.warned[0]
    assert event_type == "policy.move.executed"
    assert kwargs["correlation_id"] == records[0].decision_id
    assert kwargs["details"]["sl_to"] == pytest.approx(1.1035)
    assert kwargs["details"]["decision_id"] == records[0].decision_id
    assert kwargs["details"]["envelope_version"] == 7


def test_failed_move_is_emitted_as_warn(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    fake.symbol_info = lambda name: SimpleNamespace(
        name=name, point=0.0, digits=5, trade_tick_size=0.00001, filling_mode=3
    )
    ctx = _ctx(fake, tmp_path)
    telemetry = _CapturingTelemetry()
    service = PolicyService(ctx, telemetry=telemetry)
    service.handle_envelope_frame(_envelope_frame())
    asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(telemetry.warned) == 1
    event_type, kwargs = telemetry.warned[0]
    assert event_type == "policy.move.failed"
    assert kwargs["error_code"] == "POLICY_POINT_UNKNOWN"


# ----------------------------------------------------------------------
# Local API — GET /api/v1/policy/moves
# ----------------------------------------------------------------------
def _api_client(ctx):
    # The production app registers a BridgeError handler (create_app);
    # a bare router-only app needs the same one or a raised BridgeError
    # surfaces as a bare 500 instead of its real status.
    from nse_bridge.api.app import _error_response
    from nse_bridge.errors import BridgeError

    app = FastAPI()
    app.state.ctx = ctx

    @app.exception_handler(BridgeError)
    async def _bridge_error_handler(request, exc):
        return _error_response(exc, "")

    app.include_router(management_router)
    return TestClient(app, raise_server_exceptions=False)


def test_local_api_serves_persisted_moves(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    ctx.policy_move_store = store
    service = PolicyService(ctx, move_store=store)
    service.handle_envelope_frame(_envelope_frame())
    asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(store.list_moves()) == 1

    resp = _api_client(ctx).get("/api/v1/policy/moves")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["data"]["moves"]) == 1
    move = body["data"]["moves"][0]
    assert move["rule_id"] == "trail-1"
    assert move["ticket"] == 9000001
    assert move["sl_to"] == pytest.approx(1.1035)
    # Filters: a non-matching ticket finds nothing.
    assert _api_client(ctx).get("/api/v1/policy/moves?ticket=1").json()["data"]["moves"] == []
    assert _api_client(ctx).get("/api/v1/policy/moves?kind=trailing_tp").json()["data"]["moves"] == []
    assert len(_api_client(ctx).get("/api/v1/policy/moves?kind=trailing").json()["data"]["moves"]) == 1


def test_local_api_empty_when_no_move(tmp_path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    ctx.policy_move_store = PolicyMoveStore(tmp_path / "policy_moves.jsonl")
    resp = _api_client(ctx).get("/api/v1/policy/moves")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"moves": [], "total": 0}


def test_local_api_503_while_store_not_initialised(tmp_path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)  # bare context: no policy_move_store wired
    assert ctx.policy_move_store is None
    resp = _api_client(ctx).get("/api/v1/policy/moves")
    assert resp.status_code == 503
