"""Phase 2b W2/W4 tests — mandatory pre-send order_check + uncertain-result reconciliation.

Label: unit. No MT5 — a fake MT5 module is injected into the driver.

W2 (CONTRACTS.md §7, DESIGN.md §3.8): every financial send in the
gateway execution path runs the official ``mt5.order_check`` with the
EXACT request first. A failed check must prevent ``order_send``; the
raw check result travels with the outcome; a passing check is never a
guarantee of execution.

W4 (spec §7 + CONTRACTS.md §8): a ``None`` response or retcode 10031
(CONNECTION) does NOT prove non-execution — the send loop must stop,
reconcile against live broker state, and never re-send. Confirmed
executions are reported as executed; unresolved outcomes stay
UNKNOWN; a duplicate ``order_send`` never happens.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.engine.execution import PendingOrderRequest, trade_send_pending_order
from nse_bridge.engine.orders import ModifyPendingRequest, trade_modify_pending_order
from nse_bridge.engine.trading import close_position, market_order, modify_position_sltp
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.retcodes import TRADE_RETCODE_CONNECTION
from nse_bridge.server.commands import CommandExecutor, CommandState, CommandValidator
from nse_bridge.server.queue import BoundedPendingQueue
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

DEVICE = "dev-1"
ACCOUNT = "12345678901"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


class _FakeMt5:
    """Minimal MT5 module with hookable order_check / order_send.

    ``send_behaviour`` is a callable(request) -> SimpleNamespace | None
    consulted BEFORE the default dispatch, so tests can simulate lost
    responses (None), retcode 10031, and side effects like a landed
    close whose response never arrived.
    """

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

    def __init__(self, *, positions=None, orders=None, check_retcode=0, check_none=False, send_behaviour=None):
        self.positions = {p.ticket: p for p in (positions or [])}
        self.orders = {o.ticket: o for o in (orders or [])}
        self.check_retcode = check_retcode
        self.check_none = check_none
        self.send_behaviour = send_behaviour
        self.check_requests: list[dict] = []
        self.sent_requests: list[dict] = []
        self.send_calls = 0
        self.check_calls = 0

    # -- reads ----------------------------------------------------------
    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(int(ticket))
            return [p] if p is not None else []
        rows = list(self.positions.values())
        return [p for p in rows if p.symbol == symbol] if symbol else rows

    def orders_get(self, ticket=None, symbol=None):
        if ticket is not None:
            o = self.orders.get(int(ticket))
            return [o] if o is not None else []
        return list(self.orders.values())

    def symbol_info(self, name):
        return SimpleNamespace(
            name=name,
            visible=True,
            trade_mode=4,
            digits=5,
            point=0.00001,
            trade_tick_size=0.00001,
            trade_stops_level=10,
            trade_freeze_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=3,
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(time.time()), bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)

    def account_info(self):
        return SimpleNamespace(login=123456, trade_mode=0, margin_mode=2, balance=10000.0, equity=10000.0)

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    # -- trade ops --------------------------------------------------------
    def order_check(self, request):
        self.check_calls += 1
        self.check_requests.append(dict(request))
        if self.check_none:
            return None
        return SimpleNamespace(
            retcode=self.check_retcode,
            balance=10000.0,
            equity=10000.0,
            profit=0.0,
            margin=32.97,
            margin_free=9967.03,
            margin_level=30331.0,
            comment="Done" if self.check_retcode == 0 else "CheckFailed",
            request=dict(request),
        )

    def order_send(self, request):
        self.sent_requests.append(dict(request))
        self.send_calls += 1
        if self.send_behaviour is not None:
            return self.send_behaviour(request)
        return SimpleNamespace(
            retcode=10009, order=0, deal=8000001, price=1.1, volume=request.get("volume"), comment=""
        )


def _position(ticket=9000001, symbol="EURUSD", type_=0, volume=0.5):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type_,
        volume=volume,
        price_open=1.1000,
        price_current=1.1002,
        sl=0.0,
        tp=0.0,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=888101,
        comment="",
        time=int(time.time()) - 30,
    )


def _ctx(fake, tmp_path) -> BridgeContext:
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"), magic=888101)
    driver = Mt5Driver(settings)
    driver._mt5 = fake
    driver._connected = True
    return BridgeContext(settings=settings, driver=driver)


# ----------------------------------------------------------------------
# W2: order_check gates every send
# ----------------------------------------------------------------------
def test_failed_check_prevents_close_send(tmp_path):
    fake = _FakeMt5(positions=[_position()], check_retcode=10019)  # NO_MONEY
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        close_position(ctx, 9000001)
    assert excinfo.value.code is ErrorCode.ORDER_CHECK_FAILED
    assert excinfo.value.retcode == 10019
    assert excinfo.value.details["order_check"]["comment"] == "CheckFailed"
    assert fake.send_calls == 0  # order_send never reached
    assert fake.check_calls == 1


def test_failed_check_prevents_market_open_send(tmp_path):
    fake = _FakeMt5(check_retcode=10014)  # INVALID_VOLUME
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        market_order(ctx, symbol="EURUSD", side="buy", volume=0.01)
    assert excinfo.value.code is ErrorCode.ORDER_CHECK_FAILED
    assert fake.send_calls == 0


def test_check_none_fails_closed(tmp_path):
    fake = _FakeMt5(positions=[_position()], check_none=True)
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        close_position(ctx, 9000001)
    assert excinfo.value.code is ErrorCode.ORDER_CHECK_FAILED
    assert "unverified" in excinfo.value.message
    assert fake.send_calls == 0


def test_check_request_is_the_exact_send_request(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    close_position(ctx, 9000001)
    assert fake.check_calls == 1
    assert fake.send_calls == 1
    # The exact request that was checked is the request that was sent.
    assert fake.check_requests[0] == fake.sent_requests[0]


def test_passing_check_recorded_in_close_payload(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    ctx = _ctx(fake, tmp_path)
    payload = close_position(ctx, 9000001)
    assert payload["status"] == "done"
    assert payload["order_check"]["retcode"] == 0
    assert payload["order_check"]["margin_free"] == 9967.03  # raw result preserved


def test_modify_failed_check_prevents_send(tmp_path):
    fake = _FakeMt5(positions=[_position()], check_retcode=10016)  # INVALID_STOPS
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        modify_position_sltp(ctx, 9000001, 1.0500, 1.1200)
    assert excinfo.value.code is ErrorCode.ORDER_CHECK_FAILED
    assert fake.send_calls == 0


def test_modify_no_changes_check_passes(tmp_path):
    # SLTP with unchanged values: order_check reports 10025 (NO_CHANGES),
    # which is a PASS for the check — the send then reports NO_CHANGES
    # as an idempotent success (real-MT5 defect 1 semantics).
    fake = _FakeMt5(positions=[_position()], check_retcode=10025)
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=10025, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    payload = modify_position_sltp(ctx, 9000001, 0.0, 0.0)
    assert payload["no_changes"] is True
    assert payload["order_check"]["retcode"] == 10025
    assert fake.send_calls == 1


# ----------------------------------------------------------------------
# W4: uncertain outcomes reconcile, never re-send
# ----------------------------------------------------------------------
def test_close_none_response_reconciled_position_gone(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])

    def behaviour(request):
        # The close lands on the broker, but the response frame is lost.
        del fake.positions[9000001]

    fake.send_behaviour = behaviour
    ctx = _ctx(fake, tmp_path)
    payload = close_position(ctx, 9000001)
    assert payload["status"] == "done"
    assert payload["closed_volume"] == 0.5
    assert payload["remaining_volume"] == 0.0
    assert payload["reconciled"] is True
    assert payload["ambiguous_fill_recovered"] is True
    assert payload["reconciliation"] == "position_gone"
    assert payload["order_check"]["retcode"] == 0
    assert fake.send_calls == 1  # reconciled — never re-sent


def test_close_none_response_reconciled_partial_fill(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])

    def behaviour(request):
        # A partial close lands (0.2 closed) but the response is lost.
        fake.positions[9000001].volume = 0.3

    fake.send_behaviour = behaviour
    ctx = _ctx(fake, tmp_path)
    payload = close_position(ctx, 9000001, volume=0.2)
    assert payload["status"] == "done"
    assert payload["partial"] is True
    assert payload["closed_volume"] == pytest.approx(0.2)
    assert payload["remaining_volume"] == pytest.approx(0.3)
    assert payload["reconciled"] is True
    assert fake.send_calls == 1  # no duplicate close of the remaining volume


def test_close_none_response_unchanged_position_stays_unverified(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])
    fake.send_behaviour = lambda request: None  # response lost, nothing landed
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        close_position(ctx, 9000001)
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1  # never re-sent while unknown


def test_close_retcode_10031_reconciled_position_gone(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])

    def behaviour(request):
        del fake.positions[9000001]
        return SimpleNamespace(retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment="")

    fake.send_behaviour = behaviour
    ctx = _ctx(fake, tmp_path)
    payload = close_position(ctx, 9000001)
    assert payload["status"] == "done"
    assert payload["retcode"] == TRADE_RETCODE_CONNECTION
    assert payload["reconciliation"] == "position_gone"
    assert fake.send_calls == 1


def test_close_retcode_10031_position_intact_stays_unverified(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        close_position(ctx, 9000001)
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert excinfo.value.details["retcode"] == TRADE_RETCODE_CONNECTION
    assert fake.send_calls == 1


def test_modify_none_response_raises_unverified_no_resend(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    fake.send_behaviour = lambda request: None
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        modify_position_sltp(ctx, 9000001, 1.05, 1.12)
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1  # one attempt, then reconcile — no re-send


def test_modify_10031_raises_unverified_no_resend(tmp_path):
    fake = _FakeMt5(positions=[_position()])
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        modify_position_sltp(ctx, 9000001, 1.05, 1.12)
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1


def test_pending_modify_10031_raises_unverified(tmp_path):
    order = SimpleNamespace(
        ticket=7000001,
        symbol="EURUSD",
        type=2,
        price_open=1.0500,
        volume_initial=0.01,
        volume_current=0.01,
        sl=0.0,
        tp=0.0,
        time_setup=int(time.time()),
        type_time=0,
        magic=888101,
        comment="",
    )
    fake = _FakeMt5(orders=[order])
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        trade_modify_pending_order(ctx, ModifyPendingRequest(ticket=7000001, sl=1.0400))
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1


def test_pending_send_placed_10008_is_success(tmp_path):
    fake = _FakeMt5()

    def behaviour(request):
        return SimpleNamespace(
            retcode=10008, order=7100001, deal=0, price=request["price"], volume=request["volume"], comment=""
        )

    fake.send_behaviour = behaviour
    ctx = _ctx(fake, tmp_path)
    payload = trade_send_pending_order(
        ctx, PendingOrderRequest(symbol="EURUSD", order_type="BUY_LIMIT", volume=0.01, price=1.0000)
    )
    assert payload["status"] == "done"
    assert payload["retcode"] == 10008
    assert payload["retcode_label"].startswith("PLACED")
    assert payload["order_check"]["retcode"] == 0


def test_pending_send_none_response_unverified(tmp_path):
    fake = _FakeMt5()
    fake.send_behaviour = lambda request: None
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        trade_send_pending_order(
            ctx, PendingOrderRequest(symbol="EURUSD", order_type="BUY_LIMIT", volume=0.01, price=1.0000)
        )
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1


def test_market_order_10031_recovered_by_fingerprint(tmp_path):
    # A recently-opened position with the bridge magic and the requested
    # side exists although the send response was lost → reconciled.
    opened = SimpleNamespace(
        ticket=8001234,
        symbol="EURUSD",
        type=0,  # buy
        volume=0.01,
        price_open=1.1002,
        sl=0.0,
        tp=0.0,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=888101,
        comment="",
        time=int(time.time()) - 5,
    )
    fake = _FakeMt5(positions=[opened])
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    payload = market_order(ctx, symbol="EURUSD", side="buy", volume=0.01)
    assert payload["ambiguous_fill_recovered"] is True
    assert payload["order_check"]["retcode"] == 0
    assert fake.send_calls == 1


def test_market_order_10031_no_fingerprint_stays_unverified(tmp_path):
    fake = _FakeMt5()
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        market_order(ctx, symbol="EURUSD", side="buy", volume=0.01)
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED
    assert fake.send_calls == 1


# ----------------------------------------------------------------------
# Command layer: UNKNOWN outcome reconciliation end-to-end
# ----------------------------------------------------------------------
def _frame(**operation):
    issued = NOW - timedelta(minutes=1)
    return {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "close_position", "ticket": "9000001", **operation},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "same-as-command-id",
    }


def _command_ctx(fake, tmp_path):
    ctx = _ctx(fake, tmp_path)
    validator = CommandValidator(
        DEVICE,
        get_account_id=lambda: ACCOUNT,
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: NOW,
    )
    return ctx, CommandExecutor(
        ctx, validator, results_queue=BoundedPendingQueue(32, "test/results"), clock=lambda: NOW
    )


def test_command_layer_10031_close_reconciles_to_executed(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])

    def behaviour(request):
        del fake.positions[9000001]
        return SimpleNamespace(retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment="")

    fake.send_behaviour = behaviour
    _ctx_result, executor = _command_ctx(fake, tmp_path)
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["payload"]["reconciled"] is True
    assert result["payload"]["closed_volume"] == 0.5
    # W9: the raw order_check result survives into the command outcome.
    assert result["payload"]["order_check"]["retcode"] == 0


def test_command_layer_10031_close_unresolved_stays_unknown(tmp_path):
    fake = _FakeMt5(positions=[_position(volume=0.5)])
    fake.send_behaviour = lambda request: SimpleNamespace(
        retcode=TRADE_RETCODE_CONNECTION, order=0, deal=0, price=0.0, volume=0.0, comment=""
    )
    _ctx_result, executor = _command_ctx(fake, tmp_path)
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.UNKNOWN
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_UNVERIFIED"
    assert result["error"]["retry_safe"] is False
    assert fake.send_calls == 1  # never re-sent


# ----------------------------------------------------------------------
# W9 — end-to-end raw-fact preservation in action.result
# ----------------------------------------------------------------------
def test_close_command_result_preserves_all_broker_facts(tmp_path):
    """Every fact the Platform's execution trace needs survives into the
    command outcome: command identity, retcode + label, the raw
    order_check result, the raw broker response (fill price/volume),
    timing, and the reconciliation status (W9)."""
    fake = _FakeMt5(positions=[_position(volume=0.5)])
    ctx = _ctx(fake, tmp_path)
    validator = CommandValidator(
        DEVICE,
        get_account_id=lambda: ACCOUNT,
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: NOW,
    )
    results = BoundedPendingQueue(32, "test/results")
    executor = CommandExecutor(ctx, validator, results_queue=results, clock=lambda: NOW)
    frame = _frame()
    _execution, result = executor.execute(frame)

    # Command identity: correlation_id == command_id (§6.5).
    assert result["correlation_id"] == frame["command_id"]
    assert result["event_type"] == "action.result"
    assert result["status"] == "executed"
    # Timing (client-measured, §6.5).
    assert result["started_at"] and result["completed_at"]
    assert result["duration_ms"] >= 0
    # Raw broker facts: retcode + label + fill price/volume, verbatim.
    payload = result["payload"]
    assert payload["retcode"] == 10009
    assert payload["retcode_label"].startswith("DONE")
    assert payload["price"] == pytest.approx(1.1)  # the broker's fill price
    assert payload["volume"] == pytest.approx(0.5)
    assert payload["ticket"] == 9000001
    assert payload["closed_volume"] == pytest.approx(0.5)
    # The complete pre-send order_check result is preserved.
    assert payload["order_check"]["retcode"] == 0
    assert payload["order_check"]["margin_free"] == 9967.03
    # actual_mcp_result mirrors the payload (raw MT5 result preserved).
    assert result["actual_mcp_result"] == payload
    assert result["data_quality"]["raw_available"] is True
    # The result was persisted for reconnect delivery (§6.5).
    assert len(results) == 1
    assert results.snapshot()[0]["correlation_id"] == frame["command_id"]
