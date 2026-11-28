"""Unit tests for audit P2-5: broker retcode in ``action.result`` errors.

Label: unit. No MT5 — the terminal is a fake whose ``order_send``
returns a chosen broker retcode, or the engine function is
monkeypatched to raise the ``BridgeError`` the engine would raise.

The audit claim under test: ``_finish_error`` built the
``action.result`` error via ``_error_object(code, category,
cause=message, ...)`` and dropped ``exc.retcode`` (and the
``TRADE_UNVERIFIED`` raises carried the retcode only inside
``details``), so the server saw "failed" with no retcode and could
not distinguish 10027 (algo trading disabled) from 10030 (invalid
fill) from 10004 (requote). After the fix the error object carries
``retcode`` + ``retcode_label`` whenever the failure carries a broker
retcode, and carries neither key when it does not.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.commands import (
    CommandExecutor,
    CommandState,
    CommandValidator,
    _error_object,
    parse_envelope,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DEVICE = "dev-1"
ACCOUNT = "123456"


class _RejectingMt5:
    """Minimal fake terminal: order_check passes, order_send answers
    with one fixed broker retcode. Everything else is just enough
    broker state for a v2 market open to reach the send."""

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

    def __init__(self, retcode: int):
        self.retcode = retcode
        self.send_calls = 0

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(time.time()), bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)

    def symbol_info(self, name):
        return SimpleNamespace(
            name=name,
            point=0.00001,
            digits=5,
            trade_stops_level=10,
            trade_freeze_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=3,
            trade_exemode=2,
            spread=20,
        )

    def symbols_get(self):
        return [self.symbol_info("EURUSD")]

    def symbol_select(self, name, enable=True):
        return True

    def positions_get(self, ticket=None, symbol=None):
        return []

    def orders_get(self, ticket=None, symbol=None):
        return []

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def order_check(self, request):
        return SimpleNamespace(
            retcode=0,
            balance=10000.0,
            equity=10000.0,
            profit=0.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            comment="Done",
            request=dict(request),
        )

    def order_send(self, request):
        self.send_calls += 1
        return SimpleNamespace(
            retcode=self.retcode,
            order=0,
            deal=0,
            price=request.get("price", 0.0),
            volume=request.get("volume", 0.0),
            comment="",
        )


def _ctx_with_fake_mt5(tmp_path, retcode: int):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _RejectingMt5(retcode)
    driver._mt5 = fake
    driver._connected = True
    ctx = BridgeContext(settings=settings, driver=driver)
    try:
        yield ctx, fake
    finally:
        driver.disconnect()


def _validator(**overrides):
    kwargs = {
        "get_account_id": lambda: ACCOUNT,
        "entitlements": Entitlements(position_manager=True, auto_trade=True),
        "open_order_authorized": True,
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return CommandValidator(DEVICE, **kwargs)


def _open_frame(**op_overrides):
    issued = NOW - timedelta(minutes=1)
    operation = {"type": "open_order", "symbol": "EURUSD", "side": "buy", "order_kind": "market", "volume": 0.1}
    operation.update(op_overrides)
    return {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": operation,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": new_uuid7_str(),
        "protocol_version": "2",
    }


def _close_frame():
    issued = NOW - timedelta(minutes=1)
    return {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": new_uuid7_str(),
    }


def _executor(ctx):
    return CommandExecutor(ctx, _validator(), clock=lambda: NOW)


# ----------------------------------------------------------------------
# Acceptance: a failed MT5 call surfaces its broker retcode in the
# action.result error — driven through the real v2 market-open path
# (order_check passes, order_send is rejected by the broker).
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("retcode", "label_fragment"),
    [
        (10030, "INVALID_FILL"),
        (10027, "CLIENT_DISABLES_AT"),
        (10004, "REQUOTE"),
    ],
)
def test_failed_market_open_surfaces_broker_retcode(tmp_path, retcode, label_fragment):
    for ctx, fake in _ctx_with_fake_mt5(tmp_path, retcode):
        executor = _executor(ctx)
        execution, result = executor.execute(_open_frame())
        assert fake.send_calls == 1  # the send really happened; this is a broker rejection, not a local refuse
        assert execution.state is CommandState.FAILED
        assert result["status"] == "mcp_failed"
        assert result["error"]["code"] == "TRADE_REJECTED"
        assert result["error"]["retcode"] == retcode
        assert label_fragment in result["error"]["retcode_label"]


def test_failed_market_open_with_retcode_10027_is_distinguishable_from_10030(tmp_path):
    """The two retcodes the audit names must not collapse into the
    same "failed" error: 10027 vs 10030 differ in the error object."""
    errors = {}
    for retcode in (10027, 10030):
        for ctx, _fake in _ctx_with_fake_mt5(tmp_path, retcode):
            _, result = _executor(ctx).execute(_open_frame())
            errors[retcode] = result["error"]
    assert errors[10027]["retcode"] == 10027
    assert errors[10030]["retcode"] == 10030
    assert errors[10027]["retcode_label"] != errors[10030]["retcode_label"]


def test_order_check_refusal_surfaces_its_retcode(monkeypatch, tmp_path):
    """An order_check refusal (e.g. 10030 INVALID_FILL) carries the
    retcode the check itself returned — the send is never reached."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(
            ErrorCode.ORDER_CHECK_FAILED,
            "order_check refused the close_position request: INVALID_FILL - Invalid order filling type",
            retcode=10030,
            details={"op": "close_position", "symbol": "EURUSD"},
        )

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    _, result = _executor(ctx).execute(_close_frame())
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "ORDER_CHECK_FAILED"
    assert result["error"]["retcode"] == 10030
    assert "INVALID_FILL" in result["error"]["retcode_label"]


def test_unverified_close_with_retcode_10031_surfaces_when_unreconciled(monkeypatch, tmp_path):
    """TRADE_UNVERIFIED used to carry the retcode only inside
    ``details``; with the outcome unprovable the error object itself
    must now carry 10031."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned retcode 10031", retcode=10031)

    def fetch_open(ctx_, ticket):
        return SimpleNamespace(ticket=9000001, volume=0.1)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_open)
    execution, result = _executor(ctx).execute(_close_frame())
    assert execution.state is CommandState.UNKNOWN
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_UNVERIFIED"
    assert result["error"]["retcode"] == 10031
    assert "CONNECTION" in result["error"]["retcode_label"]


# ----------------------------------------------------------------------
# Shape compatibility: no retcode anywhere unless a broker retcode
# actually applies; success carries no error at all.
# ----------------------------------------------------------------------
def test_error_without_retcode_has_no_retcode_keys(monkeypatch, tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.VALIDATION, "Close volume exceeds position volume.")

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    _, result = _executor(ctx).execute(_close_frame())
    assert result["status"] == "mcp_failed"
    assert "retcode" not in result["error"]
    assert "retcode_label" not in result["error"]
    # The pre-existing keys are untouched.
    assert set(result["error"]) == {
        "code",
        "category",
        "source_action",
        "raw",
        "cause",
        "recovery",
        "retry_safe",
        "server_intervention_required",
    }


def test_unverified_without_retcode_has_no_retcode_key(monkeypatch, tmp_path):
    """No-result outcome (raw is None): there is no broker retcode to
    report, so no field is invented."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    def fetch_open(ctx_, ticket):
        return SimpleNamespace(ticket=9000001, volume=0.1)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_open)
    _, result = _executor(ctx).execute(_close_frame())
    assert result["error"]["code"] == "TRADE_UNVERIFIED"
    assert "retcode" not in result["error"]
    assert "retcode_label" not in result["error"]


def test_success_result_has_no_error_and_payload_retcode_untouched(monkeypatch, tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    execution, result = _executor(ctx).execute(_close_frame())
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert "error" not in result
    assert result["payload"]["retcode"] == 10009  # success payload shape unchanged


def test_error_object_carries_retcode_only_when_given():
    with_retcode = _error_object(
        code="TRADE_REJECTED",
        category="Execution",
        source_action="trade_close_single_position",
        cause="broker rejected",
        retcode=10030,
    )
    assert with_retcode["retcode"] == 10030
    assert "INVALID_FILL" in with_retcode["retcode_label"]

    without = _error_object(
        code="VALIDATION_ERROR",
        category="Validation",
        source_action="trade_close_single_position",
        cause="bad input",
    )
    assert "retcode" not in without
    assert "retcode_label" not in without


def test_parse_envelope_still_accepts_close_frame():
    envelope = parse_envelope(_close_frame())
    assert envelope.operation_type == "close_position"
    assert envelope.ticket_int == 9000001
