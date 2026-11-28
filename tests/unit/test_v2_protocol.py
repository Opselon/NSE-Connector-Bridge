"""Unit tests for the P3 v2 protocol (Bridge v1.4.0 B3).

Label: unit. No MT5 — the driver calls are monkeypatched; the
fence, idempotency store, lifecycle record and validator are pure.

Covers, against the Platform v2 contract
(``connector::contract`` on Platform origin/main, MASTER §14):
- versioned envelopes: "2" negotiates v2, absent/unknown is v1
  fail-closed; v2-only ops are refused under v1.
- ``open_order`` needs BOTH gates (operator flag + negotiated v2).
- ``cancel_order`` / ``partial_close`` dispatch under v2;
  ``trailing_stop`` / ``break_even`` stay refused (no verified
  primitive on this path).
- idempotency: a redelivery returns the stored terminal result
  and never touches MT5 twice — same command_id or same key.
- per-device fencing: one active command per (device, token), the
  token per-ticket (P1-3) so independent tickets run concurrently;
  a superseded policy_revision is Fenced, never executed.
- server lifecycle: pending → delivered → acknowledged →
  confirmed/failed, with timestamps, on the result envelope.
- execution timeout: an overrunning dispatch is reconciled
  against MT5 truth before any outcome is reported.
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
    CommandFence,
    CommandState,
    CommandValidator,
    IdempotencyStore,
    RejectionReason,
    ServerLifecycle,
    default_priority,
    parse_envelope,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DEVICE = "dev-1"
ACCOUNT = "12345678901"


def _frame(**overrides):
    issued = NOW - timedelta(minutes=1)
    command_id = new_uuid7_str()
    frame = {
        "type": "command",
        "command_id": command_id,
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        # Production wire: idempotency_key == command_id (v1, and
        # the Platform's replay sets it so) — a fresh frame is a
        # fresh submission, never an accidental dedupe collision.
        "idempotency_key": command_id,
    }
    frame.update(overrides)
    if "operation" in overrides and isinstance(overrides["operation"], dict):
        frame["operation"] = overrides["operation"]
    return frame


def _open_frame(**op_fields):
    op = {"type": "open_order", "ticket": "0", "symbol": "EURUSD", "side": "buy", "volume": 0.1}
    op.update(op_fields)
    return _frame(protocol_version="2", operation=op)


def _validator(**overrides):
    kwargs = {
        "get_account_id": lambda: ACCOUNT,
        "entitlements": Entitlements(position_manager=True, auto_trade=True),
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return CommandValidator(DEVICE, **kwargs)


def _ctx():
    settings = BridgeSettings(_env_file=None, server_token="<redacted>", device_id=DEVICE)
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


def _position(ticket=9000001, volume=0.5):
    return SimpleNamespace(
        ticket=ticket,
        symbol="EURUSD",
        type=0,
        volume=volume,
        price_open=1.098,
        price_current=1.099,
        sl=0.0,
        tp=0.0,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=0,
        comment="",
        time=int(time.time()),
    )


# ----------------------------------------------------------------------
# IdempotencyStore
# ----------------------------------------------------------------------
def test_idempotency_store_round_trips_and_stamps_deduplicated():
    store = IdempotencyStore()
    result = {"status": "executed", "correlation_id": "cmd-1"}
    store.put("key-1", result)
    assert "key-1" in store
    got = store.get("key-1")
    assert got is not None
    assert got["status"] == "executed"
    assert got["deduplicated"] is True
    assert got is not result  # a copy — callers cannot mutate the store
    assert store.get("missing") is None


def test_idempotency_store_bounded_evicts_oldest():
    store = IdempotencyStore(max_keys=3)
    for i in range(5):
        store.put(f"k{i}", {"status": "executed"})
    assert len(store) == 3
    assert "k0" not in store and "k1" not in store
    assert "k4" in store


# ----------------------------------------------------------------------
# CommandFence
# ----------------------------------------------------------------------
def test_fence_single_active_command_per_device_token():
    fence = CommandFence()
    assert fence.acquire("dev-1", "default", "cmd-1", timeout_s=1.0) is True
    assert fence.acquire("dev-1", "default", "cmd-2", timeout_s=0.0) is False
    # A different device, or a different fence token, is unaffected.
    assert fence.acquire("dev-2", "default", "cmd-3", timeout_s=0.0) is True
    assert fence.acquire("dev-1", "7", "cmd-4", timeout_s=0.0) is True
    fence.release("dev-1", "default", "cmd-1")
    assert fence.acquire("dev-1", "default", "cmd-2", timeout_s=0.0) is True


def test_fence_release_is_scoped_to_holder():
    fence = CommandFence()
    fence.acquire("dev-1", "default", "cmd-1", timeout_s=1.0)
    fence.release("dev-1", "default", "cmd-2")  # not the holder — no-op
    assert fence.active_command("dev-1", "default") == "cmd-1"
    fence.release("dev-1", "default", "cmd-1")
    assert fence.active_command("dev-1", "default") is None


def test_fence_token_for_is_per_ticket():
    # P1-3: the token is per-ticket when the op acts on an existing
    # ticket — a policy_revision on the same frame does NOT widen
    # the token (the revision gate is the executor's explicit check).
    env = parse_envelope(_frame(operation={"type": "close_position", "ticket": "1", "policy_revision": 7}))
    assert CommandFence.token_for(env) == "ticket:1"
    env2 = parse_envelope(_frame(operation={"type": "close_position", "ticket": "1"}))
    assert CommandFence.token_for(env2) == "ticket:1"
    env3 = parse_envelope(_frame(operation={"type": "close_position", "ticket": "2"}))
    assert CommandFence.token_for(env3) == "ticket:2"
    # An open has no broker ticket yet: it falls back to the policy
    # revision (or "default") — a stray ticket field must not fence
    # opens together.
    env4 = parse_envelope(
        _frame(
            operation={
                "type": "open_order",
                "ticket": "0",
                "symbol": "EURUSD",
                "side": "buy",
                "volume": 0.1,
                "policy_revision": 7,
            }
        )
    )
    assert CommandFence.token_for(env4) == "7"
    env5 = parse_envelope(_frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1}))
    assert CommandFence.token_for(env5) == "default"


# ----------------------------------------------------------------------
# ServerLifecycle
# ----------------------------------------------------------------------
def test_server_lifecycle_records_states_in_order():
    lc = ServerLifecycle(command_id="cmd-1")
    assert lc.current == "pending"
    lc.record("delivered")
    lc.record("acknowledged")
    lc.record("confirmed")
    assert [s for s, _ in lc.states] == ["pending", "delivered", "acknowledged", "confirmed"]
    assert lc.to_dict()["current"] == "confirmed"
    assert lc.to_dict()["command_id"] == "cmd-1"


def test_server_lifecycle_rejects_unknown_state():
    lc = ServerLifecycle(command_id="cmd-1")
    with pytest.raises(ValueError):
        lc.record("teleported")


# ----------------------------------------------------------------------
# validator: v2 negotiation matrix
# ----------------------------------------------------------------------
def test_v2_open_order_needs_flag_and_negotiation():
    v_flag_off = _validator(open_order_authorized=False)
    assert v_flag_off.validate(_open_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    v_flag_on = _validator(open_order_authorized=True)
    assert v_flag_on.validate(_open_frame()).ok is True
    # Same open, v1 envelope (no protocol_version): refused even
    # with the flag on — the flag alone never opens an open.
    v1_open = _frame(operation={"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1})
    assert v_flag_on.validate(v1_open).rejection is RejectionReason.UNSUPPORTED_OPERATION


def test_v2_cancel_and_partial_close_allowed_under_v2_only():
    v = _validator()
    assert v.validate(_frame(protocol_version="2", operation={"type": "cancel_order", "ticket": "7000001"})).ok is True
    assert (
        v.validate(
            _frame(protocol_version="2", operation={"type": "partial_close", "ticket": "9000001", "volume": 0.2})
        ).ok
        is True
    )
    # ... and refused under v1 (no protocol_version).
    assert v.validate(_frame(operation={"type": "cancel_order", "ticket": "7000001"})).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )
    assert v.validate(_frame(operation={"type": "partial_close", "ticket": "9000001", "volume": 0.2})).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )


def test_v2_trailing_and_breakeven_stay_refused():
    """No verified command-path primitive exists for these ops in
    this slice — they are refused, never guessed, even under v2."""
    v = _validator()
    assert (
        v.validate(
            _frame(
                protocol_version="2",
                operation={"type": "trailing_stop", "ticket": "9000001", "trailing_distance": 0.001},
            )
        ).rejection
        is RejectionReason.UNSUPPORTED_OPERATION
    )
    assert (
        v.validate(
            _frame(
                protocol_version="2", operation={"type": "break_even", "ticket": "9000001", "break_even_offset": 0.0}
            )
        ).rejection
        is RejectionReason.UNSUPPORTED_OPERATION
    )


def test_v1_ops_unchanged_under_v2():
    v = _validator()
    assert (
        v.validate(_frame(protocol_version="2", operation={"type": "close_position", "ticket": "9000001"})).ok is True
    )
    assert (
        v.validate(_frame(protocol_version="2", operation={"type": "modify_sl_tp", "ticket": "9000001", "sl": 1.08})).ok
        is True
    )


def test_default_priority_close_and_cancel():
    assert default_priority("close_position") == 9
    assert default_priority("partial_close") == 9
    assert default_priority("cancel_order") == 8
    assert default_priority("open_order") == 0
    assert default_priority("modify_sl_tp") == 0


# ----------------------------------------------------------------------
# executor: cancel_order / partial_close dispatch (mock MT5)
# ----------------------------------------------------------------------
def test_executor_cancel_order_dispatches_under_v2(monkeypatch):
    ctx = _ctx()
    calls: list[int] = []
    monkeypatch.setattr(
        "nse_bridge.engine.orders.cancel_order_by_ticket",
        lambda ctx_, ticket: calls.append(ticket) or {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(protocol_version="2", operation={"type": "cancel_order", "ticket": "7000001"})
    )
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert result["source_tool"] == "trade_cancel_pending_order"
    assert result["command_protocol_version"] == "2"
    assert calls == [7000001]


def test_executor_partial_close_dispatches_under_v2(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", lambda ctx_, ticket: _position(volume=0.5))
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: {
            "status": "done",
            "ticket": ticket,
            "partial": True,
            "closed_volume": volume,
            "remaining_volume": 0.5 - (volume or 0.0),
        },
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(protocol_version="2", operation={"type": "partial_close", "ticket": "9000001", "volume": 0.2})
    )
    assert execution.state is CommandState.PARTIALLY_COMPLETED
    assert result["status"] == "executed"  # wire has no partial status (D5)
    assert result["payload"]["partial"] is True
    assert result["payload"]["closed_volume"] == pytest.approx(0.2)
    assert result["payload"]["remaining_volume"] == pytest.approx(0.3)


def test_executor_partial_close_full_volume_is_validation_error(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr("nse_bridge.engine.trading.fetch_position", lambda ctx_, ticket: _position(volume=0.5))
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(
        _frame(protocol_version="2", operation={"type": "partial_close", "ticket": "9000001", "volume": 0.5})
    )
    assert execution.state is CommandState.FAILED
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "VALIDATION_ERROR"


# ----------------------------------------------------------------------
# executor: idempotency dedupe on replay
# ----------------------------------------------------------------------
def test_executor_replay_returns_stored_result_without_second_execution(monkeypatch):
    ctx = _ctx()
    calls: list[int] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: (
            calls.append(ticket) or {"status": "done", "ticket": ticket, "retcode": 10009}
        ),
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    frame = _frame(idempotency_key="key-1")
    first_execution, _first = executor.execute(frame)
    assert first_execution.state is CommandState.COMPLETED
    assert len(calls) == 1
    # Server re-delivers the same command after a reconnect.
    second_execution, second = executor.execute(frame)
    assert second_execution.state is CommandState.COMPLETED
    assert second["status"] == "executed"
    assert second["deduplicated"] is True
    assert len(calls) == 1  # MT5 was never touched twice
    # Same idempotency key under a NEW command_id: same submission.
    third_frame = _frame(idempotency_key="key-1")
    third_execution, third = executor.execute(third_frame)
    assert third_execution.state is CommandState.COMPLETED
    assert third["deduplicated"] is True
    assert third["correlation_id"] == third_frame["command_id"]
    assert len(calls) == 1


# ----------------------------------------------------------------------
# executor: fencing
# ----------------------------------------------------------------------
def test_executor_fenced_when_another_command_holds_the_fence(monkeypatch):
    ctx = _ctx()
    calls: list[int] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: calls.append(ticket) or {"status": "done", "ticket": ticket},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW, fence_timeout_s=0.0)
    # P1-3: the fence token is per-ticket — the held token is the
    # close's own ticket (9000001), not "default".
    assert executor.fence.acquire(DEVICE, "ticket:9000001", "other-command", timeout_s=1.0) is True
    try:
        execution, result = executor.execute(_frame())
        assert execution.state is CommandState.FAILED
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "Fenced"
        assert calls == []  # nothing reached MT5
        # A command on a DIFFERENT ticket is not fenced by this token.
        execution, result = executor.execute(_frame(operation={"type": "close_position", "ticket": "9000002"}))
        assert execution.state is CommandState.COMPLETED
        assert len(calls) == 1
    finally:
        executor.fence.release(DEVICE, "ticket:9000001", "other-command")
    # Fence free: the same command shape executes.
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.COMPLETED
    assert len(calls) == 2


def test_executor_superseded_policy_revision_is_fenced(monkeypatch):
    ctx = _ctx()
    calls: list[int] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: calls.append(ticket) or {"status": "done", "ticket": ticket},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW, expected_policy_revision=7)
    stale = _frame(operation={"type": "close_position", "ticket": "9000001", "policy_revision": 6})
    execution, result = executor.execute(stale)
    assert execution.state is CommandState.FAILED
    assert result["error"]["code"] == "Fenced"
    assert calls == []
    current = _frame(operation={"type": "close_position", "ticket": "9000001", "policy_revision": 7})
    execution, result = executor.execute(current)
    assert execution.state is CommandState.COMPLETED
    assert len(calls) == 1


def test_executor_fence_released_when_dispatch_crashes(monkeypatch):
    """A crash mid-dispatch must not wedge the device's fence —
    the next command still executes."""
    ctx = _ctx()
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: (_ for _ in ()).throw(BridgeError(ErrorCode.INTERNAL, "boom")),
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, _result = executor.execute(_frame())
    assert execution.state is CommandState.FAILED
    assert executor.fence.active_command(DEVICE, "default") is None


# ----------------------------------------------------------------------
# executor: server lifecycle + execution timeout
# ----------------------------------------------------------------------
def test_executor_result_carries_server_lifecycle(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(
        "nse_bridge.engine.trading.close_position",
        lambda ctx_, ticket, volume=None: {"status": "done", "ticket": ticket, "retcode": 10009},
    )
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW)
    execution, result = executor.execute(_frame())
    assert execution.state is CommandState.COMPLETED
    lifecycle = result["server_lifecycle"]
    assert lifecycle["current"] == "confirmed"
    assert [s for s, _ in lifecycle["states"]] == ["pending", "delivered", "acknowledged", "confirmed"]
    assert result["command_protocol_version"] == "1"
    recent = executor.recent_executions(limit=1)[0]
    assert recent["server_lifecycle"] == "confirmed"
    assert recent["command_protocol_version"] == "1"
    assert recent["deduplicated"] is False


def test_executor_overrun_dispatch_is_unknown_when_unprovable(monkeypatch):
    """A dispatch that overruns execution_timeout_s is reconciled
    against MT5 truth; when the book cannot prove the landing, the
    outcome is UNKNOWN — never a blind success."""
    ctx = _ctx()

    def slow_close(ctx_, ticket, volume=None):
        time.sleep(0.05)
        return {"status": "done", "ticket": ticket, "retcode": 10009}

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", slow_close)
    executor = CommandExecutor(ctx, _validator(), clock=lambda: NOW, execution_timeout_s=0.0)
    execution, result = executor.execute(_frame())
    # Reconcile reads the live book via the real (unconnected) driver
    # → NOT_CONNECTED → unprovable → UNKNOWN.
    assert execution.state is CommandState.UNKNOWN
    assert result["status"] == "mcp_failed"
    assert result["error"]["code"] == "TIMEOUT"
    assert result["server_lifecycle"]["current"] == "unknown"
