"""Phase 2b W6 tests — policy envelope (D-5, CONTRACTS.md §4.10).

Label: unit. No MT5 — a fake MT5 module is injected into the driver.

Fail-closed semantics: an autonomous trailing/breakeven move happens
ONLY with a fresh (≤ 5 min), valid, account-matched envelope AND a
healthy platform WebSocket. Expired/invalid/missing envelopes and a
dead socket block every new move; an in-flight move completes under
the envelope it was authorized with; reconnect never revives an
expired envelope. Every move carries rule_id + decision_id + envelope
version (§17.3 traceability data).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.policy import (
    ENVELOPE_MAX_AGE_S,
    PolicyDenied,
    PolicyEnvelope,
    PolicyEnvelopeModel,
    PolicyGate,
    PolicyService,
    _tp_target_for_move,
)

pytestmark = pytest.mark.unit

DEVICE = "dev-1"
ACCOUNT = "12345678901"


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
    driver._login = 123456  # the bound MT5 login (string on the wire)
    ctx = BridgeContext(settings=settings, driver=driver)
    ctx.server_channel_state["connected"] = True  # WS healthy by default in these tests
    return ctx


def _envelope_frame(
    *,
    version=7,
    minutes=4,
    account_id="123456",  # the bound MT5 login as a string (§7.1)
    rules=None,
    allowed_symbols=None,
    clock_now=None,
):
    issued = (clock_now or datetime.now(UTC)) - timedelta(minutes=1)
    return {
        "account_id": account_id,
        "version": version,
        "bounds": {"allowed_symbols": allowed_symbols} if allowed_symbols is not None else {},
        "rules": rules
        if rules is not None
        else [{"rule_id": "trail-1", "params": {"kind": "trailing", "distance_points": 150, "step_points": 10}}],
        "risk_free_policy": {},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
    }


# ----------------------------------------------------------------------
# Envelope validation
# ----------------------------------------------------------------------
def test_envelope_window_longer_than_five_minutes_rejected():
    frame = _envelope_frame(minutes=6)
    with pytest.raises(PolicyDenied) as excinfo:
        PolicyEnvelope(PolicyEnvelopeModel.model_validate(frame))
    assert excinfo.value.reason == "window_too_long"
    assert ENVELOPE_MAX_AGE_S == 300.0  # §4.10 contract rule


def test_envelope_window_not_after_issued_rejected():
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    frame = _envelope_frame(minutes=4, clock_now=now)
    frame["expires_at"] = frame["issued_at"]  # expired == issued
    with pytest.raises(PolicyDenied) as excinfo:
        PolicyEnvelope(PolicyEnvelopeModel.model_validate(frame))
    assert excinfo.value.reason == "invalid_window"


def test_envelope_schema_violation_rejected():
    frame = _envelope_frame()
    frame["version"] = 0  # version must be >= 1
    gate = PolicyGate()
    with pytest.raises(PolicyDenied) as excinfo:
        gate.apply_frame(frame, bound_account_id=ACCOUNT)
    assert excinfo.value.reason == "invalid_rule"
    assert gate.envelope is None  # nothing stored — fail closed


def test_envelope_duplicate_rule_id_rejected():
    frame = _envelope_frame(rules=[{"rule_id": "r", "params": {"kind": "trailing", "distance_points": 100}}] * 2)
    with pytest.raises(PolicyDenied) as excinfo:
        PolicyEnvelope(PolicyEnvelopeModel.model_validate(frame))
    assert excinfo.value.reason == "invalid_rule"


def test_envelope_for_other_account_never_applied(tmp_path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    # An envelope for another account is rejected and never stored.
    report = service.handle_envelope_frame(_envelope_frame(account_id="99999999"))
    assert report["accepted"] is False
    assert report["reason"] == "account_mismatch"
    assert service.gate.envelope is None


def test_envelope_accepted_and_stored(tmp_path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    report = service.handle_envelope_frame(_envelope_frame(version=11))
    assert report["accepted"] is True
    assert service.gate.envelope is not None
    assert service.gate.envelope.version == 11


# ----------------------------------------------------------------------
# Authorization (fail-closed matrix)
# ----------------------------------------------------------------------
def test_valid_fresh_envelope_permits_authorized_move(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())
    # A long with SL 1.0900 and price 1.1050: trailing at 150 points
    # (0.0015) → target 1.1035 > 1.0900 — the stop tightens.
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(records) == 1
    record = records[0]
    assert record.status == "executed"
    # Traceability: §17.3 data on every move (D-7).
    assert record.rule_id == "trail-1"
    assert record.decision_id
    assert record.envelope_version == 7
    assert record.sl_from == pytest.approx(1.0900)
    assert record.sl_to == pytest.approx(1.1035)
    assert fake.send_calls == 1
    assert fake.sent_requests[0]["sl"] == pytest.approx(1.1035)
    # The move history carries the same traceability data.
    history = service.executor.history
    assert history[0]["rule_id"] == "trail-1"
    assert history[0]["decision_id"] == record.decision_id
    assert history[0]["envelope_version"] == 7


def test_trailing_never_widens_a_stop(tmp_path):
    # SL already tighter than the trailing target → mechanical no-op.
    fake = _FakeMt5(positions=[_position(sl=1.1049)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert all(r.status == "refused" and r.denial_reason == "no_tighter_stop" for r in records)
    assert fake.send_calls == 0


def test_expired_envelope_blocks_execution(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    gate = PolicyGate(clock=lambda: datetime.now(UTC) + timedelta(minutes=10))  # 10 min later
    service = PolicyService(ctx)
    service.gate = gate
    service.executor.gate = gate
    service.handle_envelope_frame(_envelope_frame(minutes=4))  # stored, but the clock moved past it
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []  # nothing executed
    assert fake.send_calls == 0
    with pytest.raises(PolicyDenied) as excinfo:
        gate.authorize(rule_id="trail-1", kind="trailing", symbol="EURUSD", ws_connected=True)
    assert excinfo.value.reason == "expired"


def test_invalid_envelope_blocks_execution(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    report = service.handle_envelope_frame({"not": "an envelope"})
    assert report["accepted"] is False
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []
    assert fake.send_calls == 0


def test_missing_envelope_blocks_execution(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []
    assert fake.send_calls == 0


def test_symbol_outside_bounds_blocks_execution(tmp_path):
    fake = _FakeMt5(positions=[_position(symbol="GBPUSD")])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame(allowed_symbols=["EURUSD"]))
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []  # blanket denials are not recorded per-position
    assert fake.send_calls == 0
    with pytest.raises(PolicyDenied) as excinfo:
        service.gate.authorize(rule_id="trail-1", kind="trailing", symbol="GBPUSD", ws_connected=True)
    assert excinfo.value.reason == "symbol_not_allowed"


def test_unknown_rule_blocks_execution(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())
    with pytest.raises(PolicyDenied) as excinfo:
        service.gate.authorize(rule_id="no-such-rule", kind="trailing", symbol="EURUSD", ws_connected=True)
    assert excinfo.value.reason == "unknown_rule"


def test_websocket_loss_blocks_new_moves(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())
    # In-flight move completes (authorized first)…
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records[0].status == "executed"
    # …then the WS drops: new evaluations move nothing.
    ctx.server_channel_state["connected"] = False
    fake.positions[9000001].sl = 1.0800  # a wider stop again — a new move WOULD be tempting
    records2 = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records2 == []
    assert fake.send_calls == 1  # no second send


def test_reconnect_does_not_revive_expired_envelope(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    clock = {"now": now}
    gate = PolicyGate(clock=lambda: clock["now"])
    service = PolicyService(ctx)
    service.gate = gate
    service.executor.gate = gate
    service.handle_envelope_frame(_envelope_frame(minutes=4, clock_now=now))
    clock["now"] = now + timedelta(minutes=5)  # envelope expired
    # …the WS reconnects (healthy again)…
    ctx.server_channel_state["connected"] = True
    # …but the expired envelope authorizes nothing.
    with pytest.raises(PolicyDenied) as excinfo:
        gate.authorize(rule_id="trail-1", kind="trailing", symbol="EURUSD", ws_connected=True)
    assert excinfo.value.reason == "expired"
    assert fake.send_calls == 0


def test_in_flight_move_completes_under_last_valid_envelope(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())

    original = service.executor._execute_move_sync

    def mid_flight_expiry(record, params):
        # The envelope expires while the move is executing.
        service.gate._envelope = None
        return original(record, params)

    service.executor._execute_move_sync = mid_flight_expiry  # type: ignore[method-assign]
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    # The move was authorized BEFORE it started — it completes.
    assert records[0].status == "executed"
    assert fake.send_calls == 1


def test_breakeven_move_executes_when_in_profit(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=0.0)])  # no SL yet
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(
        _envelope_frame(
            rules=[{"rule_id": "be-1", "params": {"kind": "breakeven", "trigger_points": 200, "offset_points": 20}}]
        )
    )
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records[0].status == "executed"
    assert records[0].rule_id == "be-1"
    # Entry 1.1000 + 20 points (0.0002) → SL 1.1002; price 1.1050 is
    # 500 points in profit ≥ trigger 200.
    assert fake.sent_requests[0]["sl"] == pytest.approx(1.1002)


def test_breakeven_holds_while_not_in_profit(tmp_path):
    fake = _FakeMt5(positions=[_position(sl=0.0)])
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(
        time=int(time.time()), bid=1.1005, ask=1.1007, last=1.1006, volume=10, flags=0
    )  # only 50 points in profit < trigger 200
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(
        _envelope_frame(
            rules=[{"rule_id": "be-1", "params": {"kind": "breakeven", "trigger_points": 200, "offset_points": 20}}]
        )
    )
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert all(r.status == "refused" and r.denial_reason == "no_tighter_stop" for r in records)
    assert fake.send_calls == 0


def test_no_local_strategy_only_mechanical_moves(tmp_path):
    # The envelope NEVER authorizes an open/close/volume change: the
    # executor only ever sends TRADE_ACTION_SLTP requests.
    fake = _FakeMt5(positions=[_position(sl=1.0900)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame())
    asyncio_run(service.poll_once(list(fake.positions.values())))
    for request in fake.sent_requests:
        assert request["action"] == fake.TRADE_ACTION_SLTP
        assert "volume" not in request


# ----------------------------------------------------------------------
# Trailing TP — the TP-ratchet math (audit P0-3, Part B)
# ----------------------------------------------------------------------
# The fake tick is bid 1.1050 / ask 1.1052 and the symbol point is
# 0.00001, so distance_points=150 is 0.0015 and step_points=10 is
# 0.0001. The math mirrors the Platform server loop
# (connector::trailing_tp::ratchet_tp), which proves it first.

TP_PARAMS = {"kind": "trailing_tp", "distance_points": 150, "step_points": 10}
POINT = 0.00001


def _tp_pos(*, type_=0, entry=1.1000, tp=0.0):
    return SimpleNamespace(ticket=9000001, symbol="EURUSD", type=type_, price_open=entry, tp=tp, sl=0.0)


def test_tp_ratchet_long_moves_tp_ahead_of_price():
    # Entry 1.1000, TP 1.0900, bid 1.1050: candidate 1.1065 beats the
    # TP by 165 points — the TP ratchets away from entry.
    got = _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.0900), tick_bid=1.1050, tick_ask=1.1052, point=POINT)
    assert got == pytest.approx(1.1065)


def test_tp_ratchet_short_moves_tp_ahead_of_price():
    # Short, entry 1.1100, TP 1.1050, ask 1.1052: candidate 1.1037
    # beats the TP by 13 points — the TP ratchets away from entry.
    got = _tp_target_for_move(
        TP_PARAMS, pos=_tp_pos(type_=1, entry=1.1100, tp=1.1050), tick_bid=1.1050, tick_ask=1.1052, point=POINT
    )
    assert got == pytest.approx(1.1037)


def test_tp_ratchet_step_gate_blocks_a_small_improvement():
    # Candidate 1.1065 improves on a TP of 1.1065 by 0 — under the
    # 10-point step; a TP of 1.1064 improves by exactly the step and fires.
    assert _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.1065), tick_bid=1.1050, tick_ask=1.1052, point=POINT) is None
    assert (
        _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.10645), tick_bid=1.1050, tick_ask=1.1052, point=POINT) is None
    )
    got = _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.1064), tick_bid=1.1050, tick_ask=1.1052, point=POINT)
    assert got == pytest.approx(1.1065)


def test_tp_ratchet_never_moves_tp_back():
    # Price fell back after the TP was set: the candidate trails the
    # lower price, so it sits below the current TP — no move.
    assert (
        _tp_target_for_move(
            {"kind": "trailing_tp", "distance_points": 150, "step_points": 10},
            pos=_tp_pos(tp=1.1065),
            tick_bid=1.1010,
            tick_ask=1.1012,
            point=POINT,
        )
        is None
    )


def test_tp_ratchet_never_toward_entry():
    # Long losing (entry 1.1100 above the bid 1.1050): the candidate
    # 1.1065 never clears the entry price — no move, even with no TP yet.
    assert (
        _tp_target_for_move(
            TP_PARAMS, pos=_tp_pos(entry=1.1100, tp=1.0900), tick_bid=1.1050, tick_ask=1.1052, point=POINT
        )
        is None
    )
    assert (
        _tp_target_for_move(TP_PARAMS, pos=_tp_pos(entry=1.1100), tick_bid=1.1050, tick_ask=1.1052, point=POINT) is None
    )
    # Short losing mirror: entry 1.1000 below the ask 1.1052.
    assert (
        _tp_target_for_move(
            TP_PARAMS, pos=_tp_pos(type_=1, entry=1.1000, tp=1.1100), tick_bid=1.1050, tick_ask=1.1052, point=POINT
        )
        is None
    )


def test_tp_ratchet_no_tp_yet_baselines_on_entry():
    # No TP yet: the first trailing move must clear the step measured
    # from the ENTRY price. With a 1000-point step the candidate 1.1065
    # is only 650 points above entry — blocked; with the 10-point step
    # it fires.
    assert (
        _tp_target_for_move(
            {"kind": "trailing_tp", "distance_points": 150, "step_points": 1000},
            pos=_tp_pos(),
            tick_bid=1.1050,
            tick_ask=1.1052,
            point=POINT,
        )
        is None
    )
    got = _tp_target_for_move(TP_PARAMS, pos=_tp_pos(), tick_bid=1.1050, tick_ask=1.1052, point=POINT)
    assert got == pytest.approx(1.1065)


def test_tp_ratchet_stops_level_distance_is_respected():
    # A zero distance proposes a TP ON the tick — refused by the math,
    # never left for order_check to catch. A 1-point distance sits
    # exactly one point from the tick and is allowed.
    assert (
        _tp_target_for_move(
            {"kind": "trailing_tp", "distance_points": 0, "step_points": 10},
            pos=_tp_pos(tp=1.0900),
            tick_bid=1.1050,
            tick_ask=1.1052,
            point=POINT,
        )
        is None
    )
    got = _tp_target_for_move(
        {"kind": "trailing_tp", "distance_points": 1, "step_points": 10},
        pos=_tp_pos(tp=1.0900),
        tick_bid=1.1050,
        tick_ask=1.1052,
        point=POINT,
    )
    assert got == pytest.approx(1.10501)


def test_tp_ratchet_invalid_inputs_never_move():
    assert (
        _tp_target_for_move(TP_PARAMS, pos=_tp_pos(entry=0.0, tp=1.0900), tick_bid=1.1050, tick_ask=1.1052, point=POINT)
        is None
    )
    assert _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.0900), tick_bid=0.0, tick_ask=1.1052, point=POINT) is None
    assert _tp_target_for_move(TP_PARAMS, pos=_tp_pos(tp=1.0900), tick_bid=1.1050, tick_ask=1.1052, point=0.0) is None
    # A negative step would re-issue on every tick — refused.
    assert (
        _tp_target_for_move(
            {"kind": "trailing_tp", "distance_points": 150, "step_points": -5},
            pos=_tp_pos(tp=1.0900),
            tick_bid=1.1050,
            tick_ask=1.1052,
            point=POINT,
        )
        is None
    )


# ----------------------------------------------------------------------
# Trailing TP — envelope-driven moves (mock MT5)
# ----------------------------------------------------------------------


def test_trailing_tp_rule_authorizes_its_own_kind(tmp_path):
    gate = PolicyGate()
    gate.apply_frame(
        _envelope_frame(rules=[{"rule_id": "tp-1", "params": dict(TP_PARAMS)}]),
        bound_account_id="123456",  # the frame's account (§7.1 login string)
    )
    envelope, _rule = gate.authorize(rule_id="tp-1", kind="trailing_tp", symbol="EURUSD", ws_connected=True)
    assert envelope.version == 7
    # …but the same rule does NOT authorize an SL-trailing move.
    with pytest.raises(PolicyDenied) as excinfo:
        gate.authorize(rule_id="tp-1", kind="trailing", symbol="EURUSD", ws_connected=True)
    assert excinfo.value.reason == "rule_kind_mismatch"


def test_trailing_tp_envelope_move_executes(tmp_path):
    # Long, SL 1.0900 / TP 1.0900, bid 1.1050: the TP ratchets to
    # 1.1065 (150 points ahead of the bid) and the SL is untouched.
    fake = _FakeMt5(positions=[_position(sl=1.0900, tp=1.0900)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame(rules=[{"rule_id": "tp-1", "params": dict(TP_PARAMS)}]))
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert len(records) == 1
    record = records[0]
    assert record.status == "executed"
    assert record.kind == "trailing_tp"
    assert record.rule_id == "tp-1"
    assert record.decision_id
    assert record.envelope_version == 7
    assert record.tp_from == pytest.approx(1.0900)
    assert record.tp_to == pytest.approx(1.1065)
    assert record.sl_from is None and record.sl_to is None
    assert fake.send_calls == 1
    sent = fake.sent_requests[0]
    assert sent["action"] == fake.TRADE_ACTION_SLTP
    assert sent["tp"] == pytest.approx(1.1065)
    assert sent["sl"] == pytest.approx(1.0900)  # stop untouched
    assert "volume" not in sent
    history = service.executor.history
    assert history[0]["tp_from"] == pytest.approx(1.0900)
    assert history[0]["tp_to"] == pytest.approx(1.1065)


def test_trailing_tp_step_gate_holds_the_move(tmp_path):
    # TP already 1.1065: the candidate improves it by 0 — a no-op,
    # recorded in history as no_better_tp, nothing sent.
    fake = _FakeMt5(positions=[_position(sl=1.0900, tp=1.1065)])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame(rules=[{"rule_id": "tp-1", "params": dict(TP_PARAMS)}]))
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []
    assert fake.send_calls == 0
    history = service.executor.history
    assert len(history) == 1
    assert history[0]["status"] == "refused"
    assert history[0]["denial_reason"] == "no_better_tp"
    assert history[0]["kind"] == "trailing_tp"


def test_trailing_tp_never_moves_toward_entry(tmp_path):
    # Entry 1.1100 is above the bid 1.1050 (losing long): the TP
    # never moves, nothing is sent.
    pos = _position(sl=1.0900, tp=1.0900)
    pos.price_open = 1.1100
    fake = _FakeMt5(positions=[pos])
    ctx = _ctx(fake, tmp_path)
    service = PolicyService(ctx)
    service.handle_envelope_frame(_envelope_frame(rules=[{"rule_id": "tp-1", "params": dict(TP_PARAMS)}]))
    records = asyncio_run(service.poll_once(list(fake.positions.values())))
    assert records == []
    assert fake.send_calls == 0


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
