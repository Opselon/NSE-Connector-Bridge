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
        lambda ctx_, ticket, volume=None: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    results = BoundedPendingQueue(32, "test/results")
    executor = CommandExecutor(ctx, _validator(), results_queue=results, clock=lambda: NOW)
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
        lambda ctx_, ticket, volume=None: calls.append(ticket) or {"status": "done"},
    )
    executor = CommandExecutor(ctx, _validator(), results_queue=None, clock=lambda: NOW)
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
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_sl_tp", "ticket": "9000001", "sl": 1.0750}))
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert seen == {"ticket": 9000001, "sl": 1.075, "tp": 1.09}  # tp preserved from position


@pytest.mark.unit
def test_executor_unknown_after_unverified_close_reconciles_to_executed(monkeypatch):
    ctx = _ctx()

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    def fetch_gone(ctx_, ticket):
        raise BridgeError(ErrorCode.TICKET_NOT_FOUND, "Position #9000001 not found")

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_gone)
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(_frame())
    # Reconciliation proved the position is gone → executed (recovered).
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["payload"]["ambiguous_fill_recovered"] is True


@pytest.mark.unit
def test_executor_unknown_when_position_still_open(monkeypatch):
    ctx = _ctx()

    def failing_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    def fetch_open(ctx_, ticket):
        return SimpleNamespace(ticket=9000001, volume=0.1)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", failing_close)
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", fetch_open)
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.UNKNOWN
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_UNVERIFIED"
    assert result["error"]["retry_safe"] is False


@pytest.mark.unit
def test_executor_trade_rejected_is_failed_not_unknown(monkeypatch):
    ctx = _ctx()

    def rejecting_close(ctx_, ticket, volume=None):
        raise BridgeError(ErrorCode.TRADE_REJECTED, "broker rejected", retcode=10016)

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", rejecting_close)
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
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


# ----------------------------------------------------------------------
# W3 — reduce-volume close via the optional `volume` field (PROPOSED
# Phase 2b wire amendment; absent volume keeps v1 full-close semantics).
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_parse_envelope_volume_optional_and_validated():
    env = parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001", "volume": 0.2}))
    assert env.volume == pytest.approx(0.2)
    env_none = parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001"}))
    assert env_none.volume is None
    env_null = parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001", "volume": None}))
    assert env_null.volume is None
    for bad in (0, -0.1, "abc"):
        with pytest.raises(ValueError):
            parse_envelope(_frame(operation={"type": "close_position", "ticket": "9000001", "volume": bad}))


@pytest.mark.unit
def test_partial_close_via_volume_reports_partial_facts(monkeypatch):
    ctx = _ctx()
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: (
            seen.update({"ticket": ticket, "volume": volume})
            or {
                "status": "done",
                "ticket": ticket,
                "retcode": 10010,
                "retcode_label": "DONE_PARTIAL - Request partially completed",
                "closed_volume": 0.2,
                "partial": True,
                "remaining_volume": 0.3,
                "reconciled": True,
            }
        ),
    )
    results = BoundedPendingQueue(32, "test/results")
    executor = CommandExecutor(ctx, _validator(), results_queue=results, clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(operation={"type": "close_position", "ticket": "9000001", "volume": 0.2})
    )
    assert seen == {"ticket": 9000001, "volume": pytest.approx(0.2)}
    # Local lifecycle: PARTIALLY_COMPLETED; wire status stays `executed`
    # (§6.5 has no partial status — dependency D5) with the partial
    # facts carried in the payload.
    assert execution.state is CommandState.PARTIALLY_COMPLETED
    assert result["status"] == "executed"
    assert result["payload"]["partial"] is True
    assert result["payload"]["closed_volume"] == pytest.approx(0.2)
    assert result["payload"]["remaining_volume"] == pytest.approx(0.3)
    assert result["payload"]["reconciled"] is True
    assert result["payload"]["retcode"] == 10010
    assert len(results) == 1


@pytest.mark.unit
def test_close_without_volume_keeps_full_close(monkeypatch):
    ctx = _ctx()
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: seen.update({"volume": volume}) or {"status": "done", "ticket": ticket},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(_frame())
    assert "volume" not in seen or seen["volume"] is None  # v1 semantics
    assert execution.state is CommandState.COMPLETED
    assert result["payload"].get("partial") is not True


@pytest.mark.unit
def test_volume_oversize_is_mcp_failed_never_retried(monkeypatch):
    ctx = _ctx()

    def oversize(ctx_, ticket, volume=None):
        raise BridgeError(
            ErrorCode.VALIDATION,
            "Close volume 5.0 exceeds position volume 0.5.",
            http_status=422,
            details={"ticket": ticket, "position_volume": 0.5},
        )

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", oversize)
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(operation={"type": "close_position", "ticket": "9000001", "volume": 5.0})
    )
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["error"]["retry_safe"] is False  # server decides; never a blind retry


# ----------------------------------------------------------------------
# W5 — open_order technical path behind the INACTIVE §17.5 gate.
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_open_order_rejected_by_default_gate_closed(monkeypatch):
    ctx = _ctx()
    calls: list[str] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.market_order",
        lambda ctx_, **kw: calls.append("sent") or {"status": "done"},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.01})
    )
    # §17.5 mandatory default: rejected as UnsupportedOperation until
    # ALL FOUR activation conditions are met — never executed.
    assert execution.state is CommandState.FAILED
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "UnsupportedOperation"
    assert calls == []


@pytest.mark.unit
def test_open_order_gate_open_market_dispatches(monkeypatch):
    ctx = _ctx()
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.trading.market_order",
        lambda ctx_, **kw: seen.update(kw) or {"status": "done", "ticket": 8001234, "retcode": 10009},
    )
    validator = _validator(open_order_authorized=True)
    executor = CommandExecutor(ctx, validator, clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(
            operation={
                "type": "open_order",
                "symbol": "EURUSD",
                "side": "buy",
                "volume": 0.01,
                "sl": 1.08,
                "tp": 1.10,
            }
        )
    )
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["source_tool"] == "trade_send_market_order"
    assert seen["symbol"] == "EURUSD"
    assert seen["side"] == "buy"
    assert seen["volume"] == pytest.approx(0.01)


@pytest.mark.unit
def test_open_order_gate_open_pending_dispatches(monkeypatch):
    ctx = _ctx()
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.execution.trade_send_pending_order",
        lambda ctx_, params: (
            seen.update(
                {
                    "symbol": params.symbol,
                    "order_type": params.order_type,
                    "price": params.price,
                    "volume": params.volume,
                }
            )
            or {"status": "done", "order_ticket": 7100001, "retcode": 10009}
        ),
    )
    validator = _validator(open_order_authorized=True)
    executor = CommandExecutor(ctx, validator, clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(
            operation={
                "type": "open_order",
                "order_kind": "limit",
                "symbol": "EURUSD",
                "side": "buy",
                "volume": 0.01,
                "price": 1.05,
            }
        )
    )
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["source_tool"] == "trade_send_pending_order"
    assert seen["order_type"] == "BUY_LIMIT"
    assert seen["price"] == pytest.approx(1.05)


@pytest.mark.unit
def test_open_order_structural_requirements_enforced():
    # A market open parses structurally; a pending open carries price
    # (+ stoplimit for stop_limit). An open has no broker ticket yet.
    env = parse_envelope(_frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.01}))
    assert env.symbol == "EURUSD"
    assert env.side == "buy"
    assert env.order_kind == "market"  # CONTRACTS §7 default
    assert env.ticket == ""  # an open has no broker ticket yet
    pending_env = parse_envelope(
        _frame(
            operation={
                "type": "open_order",
                "order_kind": "stop_limit",
                "symbol": "EURUSD",
                "side": "sell",
                "volume": 0.01,
                "price": 1.15,
                "stoplimit": 1.12,
            }
        )
    )
    assert pending_env.order_kind == "stop_limit"
    assert pending_env.stoplimit == pytest.approx(1.12)


@pytest.mark.unit
def test_open_order_gate_open_but_malformed_open_is_validation_error(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr("nse_bridge.engine.trading.market_order", lambda ctx_, **kw: {"status": "done"})
    validator = _validator(open_order_authorized=True)
    executor = CommandExecutor(ctx, validator, clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "sideways", "volume": 0.01})
    )
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.unit
def test_open_order_uncertain_send_reconciles_via_fingerprint(monkeypatch):
    ctx = _ctx()

    def unverified_open(ctx_, **kw):
        raise BridgeError(ErrorCode.TRADE_UNVERIFIED, "order_send returned no result")

    monkeypatch.setattr("nse_bridge.engine.trading.market_order", unverified_open)
    # A just-opened matching position exists (side + magic + recency).
    pos = SimpleNamespace(
        ticket=8001234,
        symbol="EURUSD",
        type=0,
        volume=0.01,
        price_open=1.098,
        sl=0.0,
        tp=0.0,
        magic=888101,
        time=datetime.now(UTC).timestamp() - 5,
        comment="",
        profit=0.0,
        swap=0.0,
        commission=0.0,
    )
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_positions", lambda ctx_, symbol=None: [pos])
    validator = _validator(open_order_authorized=True)
    executor = CommandExecutor(ctx, validator, clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.01})
    )
    # W4: reconciliation proves the open landed → executed, never re-sent.
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["payload"]["ticket"] == 8001234
    assert result["payload"]["ambiguous_fill_recovered"] is True


# ----------------------------------------------------------------------
# W1 — modify_pending close-out (§17.4): SL/TP only, order tickets only,
# gate closed by default; the execution semantics are pinned here.
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_modify_pending_non_numeric_ticket_is_malformed():
    v = _validator(modify_pending_proven=True)
    assert v.validate(_frame(operation={"type": "modify_pending", "ticket": "abc", "sl": 1.08})).rejection is (
        RejectionReason.MALFORMED
    )


@pytest.mark.unit
def test_modify_pending_with_position_ticket_is_rejected_not_executed(monkeypatch):
    """§17.4 pinned semantics: `modify_pending` names a PENDING ORDER.
    A ticket that only exists as an open position is TICKET_NOT_FOUND —
    never silently converted into a position modification."""
    ctx = _ctx()
    monkeypatch.setattr("nse_bridge.engine.orders._fetch_order", lambda ctx_, ticket: None)
    monkeypatch.setattr(
        "nse_bridge.engine.trading.fetch_position",
        lambda ctx_, ticket: SimpleNamespace(ticket=ticket, symbol="EURUSD", sl=0.0, tp=0.0),
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.modify_pending_order_sltp",
        lambda ctx_, ticket, sl, tp: dispatched.append("dispatched") or {"status": "done"},
    )
    executor = CommandExecutor(ctx, _validator(modify_pending_proven=True), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000001", "sl": 1.05}))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TICKET_NOT_FOUND"
    assert dispatched == []  # the position-modify path was never taken


@pytest.mark.unit
def test_modify_pending_success_with_gate_open(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(
        "nse_bridge.engine.orders._fetch_order",
        lambda ctx_, ticket: SimpleNamespace(ticket=ticket, symbol="EURUSD", sl=0.0, tp=0.0),
    )
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.trading.modify_pending_order_sltp",
        lambda ctx_, ticket, sl, tp: (
            seen.update({"ticket": ticket, "sl": sl, "tp": tp})
            or {"status": "done", "ticket": ticket, "retcode": 10009}
        ),
    )
    executor = CommandExecutor(ctx, _validator(modify_pending_proven=True), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.04}))
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["source_tool"] == "trade_modify_sl_tp"  # §17.4: same MT5 action, order ticket
    assert seen == {"ticket": 9000002, "sl": pytest.approx(1.04), "tp": 0.0}


@pytest.mark.unit
def test_modify_pending_broker_rejection_is_failed_never_retried(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(
        "nse_bridge.engine.orders._fetch_order",
        lambda ctx_, ticket: SimpleNamespace(ticket=ticket, symbol="EURUSD", sl=0.0, tp=0.0),
    )
    monkeypatch.setattr(
        "nse_bridge.engine.trading.modify_pending_order_sltp",
        lambda ctx_, ticket, sl, tp: (_ for _ in ()).throw(
            BridgeError(ErrorCode.TRADE_REJECTED, "modify rejected by broker", retcode=10016)
        ),
    )
    executor = CommandExecutor(ctx, _validator(modify_pending_proven=True), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.04}))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TRADE_REJECTED"
    assert result["error"]["retry_safe"] is False
