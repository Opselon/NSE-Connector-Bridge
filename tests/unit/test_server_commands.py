"""Unit tests for the command path (CONTRACT v1 §6).

Label: unit. No MT5 — the driver calls are monkeypatched; the
validator, lifecycle, replay cache and result envelopes are pure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.commands import (
    CommandExecution,
    CommandExecutor,
    CommandState,
    CommandValidator,
    RejectionReason,
    ReplayCache,
    build_action_result,
    parse_envelope,
    rejection_result,
)
from nse_bridge.server.queue import BoundedPendingQueue
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

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


def _ctx():
    settings = BridgeSettings(_env_file=None, server_token="x", device_id=DEVICE)
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


@pytest.mark.unit
def test_validation_order_malformed_first():
    v = _validator()
    assert v.validate({"type": "command"}).rejection is RejectionReason.MALFORMED
    assert v.validate({"type": "command", "command_id": "x"}).rejection is RejectionReason.MALFORMED
    bad_ticket = _frame(operation={"type": "close_position", "ticket": "not-a-ticket"})
    assert v.validate(bad_ticket).rejection is RejectionReason.MALFORMED
    bad_expiry = _frame(expires_at="not-a-date")
    assert v.validate(bad_expiry).rejection is RejectionReason.MALFORMED


@pytest.mark.unit
def test_validation_order_expired_before_device():
    v = _validator()
    expired = _frame(
        device_id="someone-else",
        expires_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    )
    assert v.validate(expired).rejection is RejectionReason.EXPIRED


@pytest.mark.unit
def test_validation_order_wrong_device_before_account():
    v = _validator()
    frame = _frame(device_id="someone-else", account_id="999")
    assert v.validate(frame).rejection is RejectionReason.WRONG_DEVICE


@pytest.mark.unit
def test_validation_order_wrong_account():
    v = _validator()
    assert v.validate(_frame(account_id="999")).rejection is RejectionReason.WRONG_ACCOUNT
    v_no_account = _validator(get_account_id=lambda: None)
    assert v_no_account.validate(_frame()).rejection is RejectionReason.WRONG_ACCOUNT


@pytest.mark.unit
def test_validation_order_allowlist_rejects_open_and_cancel():
    v = _validator()
    assert v.validate(_frame(operation={"type": "open_order", "ticket": "1"})).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )
    assert v.validate(_frame(operation={"type": "cancel_order", "ticket": "1"})).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )
    assert v.validate(_frame(operation={"type": "trade_send_market_order", "ticket": "1"})).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )


@pytest.mark.unit
def test_validation_modify_pending_gated_until_proven():
    v = _validator()
    frame = _frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.08})
    assert v.validate(frame).rejection is RejectionReason.UNSUPPORTED_OPERATION
    v_proven = _validator(modify_pending_proven=True)
    assert v_proven.validate(frame).ok is True


@pytest.mark.unit
def test_validation_order_entitlement_before_replay():
    v = _validator(entitlements=Entitlements(position_manager=True, auto_trade=False))
    frame = _frame()
    assert v.validate(frame).rejection is RejectionReason.AUTO_TRADE_NOT_ENABLED
    v2 = _validator(entitlements=Entitlements(position_manager=False, auto_trade=True))
    assert v2.validate(_frame()).rejection is RejectionReason.AUTO_TRADE_NOT_ENABLED


@pytest.mark.unit
def test_validation_replay_second_delivery_rejected():
    v = _validator()
    frame = _frame()
    assert v.validate(frame).ok is True
    assert v.validate(frame).rejection is RejectionReason.REPLAYED
    assert v.validate(_frame()).ok is True  # a different command_id is fine


@pytest.mark.unit
def test_failed_validation_does_not_consume_replay_id():
    """§6.3: only a SUCCESSFUL validation consumes the replay id."""
    v = _validator()
    command_id = new_uuid7_str()
    expired = _frame(command_id=command_id, expires_at=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
    assert v.validate(expired).rejection is RejectionReason.EXPIRED
    fresh = _frame(command_id=command_id)
    assert v.validate(fresh).ok is True  # same id, fresh expiry: NOT a replay


@pytest.mark.unit
def test_replay_cache_bounded_at_10000():
    cache = ReplayCache(max_ids=3)
    for i in range(5):
        cache.consume(f"id-{i}")
    assert len(cache) == 3
    assert "id-0" not in cache and "id-1" not in cache
    assert "id-4" in cache


@pytest.mark.unit
def test_lifecycle_legal_chain_and_illegal_transition():
    ex = CommandExecution(command_id="c1")
    assert ex.state is CommandState.RECEIVED
    ex.transition(CommandState.VALIDATED)
    ex.transition(CommandState.AUTHORIZED)
    ex.transition(CommandState.DISPATCHED)
    ex.transition(CommandState.EXECUTING)
    ex.transition(CommandState.COMPLETED)
    assert [s for s, _ in ex.history] == [
        "RECEIVED",
        "VALIDATED",
        "AUTHORIZED",
        "DISPATCHED",
        "EXECUTING",
        "COMPLETED",
    ]
    with pytest.raises(ValueError):
        ex.transition(CommandState.EXECUTING)  # terminal: no outgoing edges


@pytest.mark.unit
def test_action_result_envelope_shape():
    started = NOW
    completed = NOW + timedelta(milliseconds=42)
    result = build_action_result(
        device_id=DEVICE,
        command_id="cmd-1",
        account_id=ACCOUNT,
        status="executed",
        source_tool="trade_close_single_position",
        started_at=started,
        completed_at=completed,
        payload={"status": "done", "ticket": 9000001},
    )
    assert result["protocol_version"] == "1.0"
    assert result["schema_version"] == "2026-11-17"
    assert result["event_type"] == "action.result"
    assert result["correlation_id"] == "cmd-1"
    assert result["status"] == "executed"
    assert result["duration_ms"] == pytest.approx(42.0)
    assert result["payload"]["ticket"] == 9000001
    assert result["actual_mcp_result"]["ticket"] == 9000001
    assert "error" not in result  # only on failure (§6.5)
    assert "user_id" not in result  # omitted when unknown (§6.5)


@pytest.mark.unit
def test_rejection_result_shape():
    result = rejection_result(
        device_id=DEVICE,
        command_id="cmd-2",
        account_id=ACCOUNT,
        reason=RejectionReason.EXPIRED,
        operation_type="close_position",
    )
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "Expired"
    assert result["error"]["retry_safe"] is False
    assert result["validation_errors"] == ["Expired"]


@pytest.mark.unit
def test_executor_close_position_success(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    results = BoundedPendingQueue(32, "test/results")
    executor = CommandExecutor(ctx, _validator(), results_queue=results)
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["source_tool"] == "trade_close_single_position"
    assert result["payload"]["ticket"] == 9000001
    assert len(results) == 1  # persisted for reconnect delivery (§6.5)


@pytest.mark.unit
def test_executor_rejected_command_never_executes(monkeypatch):
    ctx = _ctx()
    calls: list[int] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket: calls.append(ticket) or {"status": "done"},
    )
    executor = CommandExecutor(ctx, _validator(), results_queue=None)
    execution, result = executor.execute(_frame(operation={"type": "open_order", "ticket": "1"}))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "UnsupportedOperation"
    assert calls == []


@pytest.mark.unit
def test_executor_modify_sltp_fills_missing_values_from_position(monkeypatch):
    ctx = _ctx()
    seen: dict[str, object] = {}

    def fake_fetch_position(ctx_, ticket):
        return SimpleNamespace(sl=1.0800, tp=1.0900, symbol="EURUSD")

    def fake_modify(ctx_, ticket, sl, tp):
        seen.update({"ticket": ticket, "sl": sl, "tp": tp})
        return {"status": "done", "ticket": ticket, "sl": sl, "tp": tp}

    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fake_fetch_position)
    monkeypatch.setattr("nse_bridge.engine.trading.modify_position_sltp", fake_modify)
    executor = CommandExecutor(ctx, _validator())
    execution, result = executor.execute(_frame(operation={"type": "modify_sl_tp", "ticket": "9000001", "sl": 1.0750}))
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert seen == {"ticket": 9000001, "sl": 1.075, "tp": 1.09}  # tp preserved from position


@pytest.mark.unit
def test_executor_unknown_after_unverified_close_reconciles_to_executed(monkeypatch):
    ctx = _ctx()

    def failing_close(ctx_, ticket):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    def fetch_gone(ctx_, ticket):
        raise BridgeError(ErrorCode.TICKET_NOT_FOUND, "Position #9000001 not found")

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_gone)
    executor = CommandExecutor(ctx, _validator())
    execution, result = executor.execute(_frame())
    # Reconciliation proved the position is gone → executed (recovered).
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["payload"]["ambiguous_fill_recovered"] is True


@pytest.mark.unit
def test_executor_unknown_when_position_still_open(monkeypatch):
    ctx = _ctx()

    def failing_close(ctx_, ticket):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    def fetch_open(ctx_, ticket):
        return SimpleNamespace(ticket=9000001, volume=0.1)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_open)
    executor = CommandExecutor(ctx, _validator())
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.UNKNOWN
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_UNVERIFIED"
    assert result["error"]["retry_safe"] is False


@pytest.mark.unit
def test_executor_trade_rejected_is_failed_not_unknown(monkeypatch):
    ctx = _ctx()

    def rejecting_close(ctx_, ticket):
        raise BridgeError(ErrorCode.TRADE_REJECTED, "broker rejected", retcode=10016)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", rejecting_close)
    executor = CommandExecutor(ctx, _validator())
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_REJECTED"


@pytest.mark.unit
def test_parse_envelope_ticket_string_and_int():
    env = parse_envelope(_frame(operation={"type": "close_position", "ticket": 9000001}))
    assert env.ticket == "9000001" and env.ticket_int == 9000001
    env2 = parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001"}))
    assert env2.ticket == "9000001"


@pytest.mark.unit
def test_parse_envelope_rejects_non_integral_ticket():
    # Codex P2: a float ticket must be rejected, never truncated to a
    # different ticket (9000001.5 silently became "9000001").
    with pytest.raises(ValueError, match="not an integer"):
        parse_envelope(_frame(operation={"type": "close_position", "ticket": 9000001.5}))
    with pytest.raises(TypeError, match="not an integer"):
        parse_envelope(_frame(operation={"type": "close_position", "ticket": True}))
    with pytest.raises(ValueError, match="not numeric"):
        parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001.5"}))


@pytest.mark.unit
def test_parse_envelope_accepts_integral_float_ticket():
    env = parse_envelope(_frame(operation={"type": "close_position", "ticket": 9000001.0}))
    assert env.ticket == "9000001" and env.ticket_int == 9000001
