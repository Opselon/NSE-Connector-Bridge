"""Unit tests for the §17.4 `modify_pending` gate mechanism (P0-1).

The gate opens only when BOTH hold: the operator explicitly opts in
(`BridgeSettings.enable_modify_pending`, env
`NSE_BRIDGE_ENABLE_MODIFY_PENDING=1` — default OFF, fail closed) AND
this Bridge's own version is >= `MODIFY_PENDING_MIN_VERSION`
("1.4.1", the first release carrying this gate mechanism).

There is NO wire channel for the minimum version: the Platform never
sends one (the Bridge sends its own version at validate-key /
register-device; the Platform enforces its own
`MODIFY_PENDING_MIN_VERSION` before sending). The version floor is
therefore checked locally against this Bridge's own version, and the
flag is the operator's explicit opt-in on this device.

Label: unit. No MT5 terminal, no network — the MT5 call is mocked
and the "zero MT5 calls" assertions pin the fail-closed behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.commands import (
    MODIFY_PENDING_MIN_VERSION,
    CommandExecutor,
    CommandState,
    CommandValidator,
    RejectionReason,
    modify_pending_gate_open,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
ACCOUNT = "9000001"
DEVICE = "dev-modify-pending-gate"


def _frame(**overrides):
    issued = NOW - timedelta(minutes=1)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "modify_pending", "ticket": "9000002", "sl": 1.04},
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


def _gate_validator(*, enabled: bool, bridge_version: str) -> CommandValidator:
    """Validator built exactly the way `channel.py` builds it."""
    return _validator(modify_pending_proven=modify_pending_gate_open(enabled=enabled, bridge_version=bridge_version))


def _ctx(tmp_path, **settings_overrides):
    settings = BridgeSettings(
        _env_file=None,
        server_token="<redacted>",
        device_id=DEVICE,
        data_dir=tmp_path,
        log_dir=str(tmp_path / "logs"),
        **settings_overrides,
    )
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


# ----------------------------------------------------------------------
# gate function: flag AND qualifying version, nothing else opens it
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_gate_minimum_version_is_1_4_1():
    assert MODIFY_PENDING_MIN_VERSION == "1.4.1"


@pytest.mark.unit
def test_gate_open_flag_on_and_qualifying_version():
    assert modify_pending_gate_open(enabled=True, bridge_version="1.4.1") is True
    assert modify_pending_gate_open(enabled=True, bridge_version="1.4.2") is True
    assert modify_pending_gate_open(enabled=True, bridge_version="1.10.0") is True
    assert modify_pending_gate_open(enabled=True, bridge_version="2.0.0") is True


@pytest.mark.unit
def test_gate_closed_below_minimum_version_even_with_flag():
    # The flag alone never opens the gate: a 1.4.0 build carrying this
    # code (before the slice-1 version bump) still refuses.
    assert modify_pending_gate_open(enabled=True, bridge_version="1.4.0") is False
    assert modify_pending_gate_open(enabled=True, bridge_version="1.3.0") is False
    assert modify_pending_gate_open(enabled=True, bridge_version="0.9.9") is False


@pytest.mark.unit
def test_gate_closed_flag_off_even_with_qualifying_version():
    assert modify_pending_gate_open(enabled=False, bridge_version="1.4.1") is False
    assert modify_pending_gate_open(enabled=False, bridge_version="2.0.0") is False


@pytest.mark.unit
def test_gate_closed_unparseable_version_fail_closed():
    assert modify_pending_gate_open(enabled=True, bridge_version="") is False
    assert modify_pending_gate_open(enabled=True, bridge_version="1.4.x") is False
    assert modify_pending_gate_open(enabled=True, bridge_version="not-a-version") is False


# ----------------------------------------------------------------------
# settings: default OFF, explicit opt-in via env
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_modify_pending_flag_defaults_off_in_settings(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.enable_modify_pending is False


@pytest.mark.unit
def test_modify_pending_flag_opt_in_via_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NSE_BRIDGE_ENABLE_MODIFY_PENDING", "1")
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.enable_modify_pending is True


@pytest.mark.unit
def test_modify_pending_flag_opt_out_via_environment_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("NSE_BRIDGE_ENABLE_MODIFY_PENDING", "0")
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.enable_modify_pending is False


# ----------------------------------------------------------------------
# validator: gate-open accepts, gate-closed refuses (no MT5 touched)
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_gate_open_validator_accepts_modify_pending():
    v = _gate_validator(enabled=True, bridge_version="1.4.1")
    assert v.validate(_frame()).ok is True


@pytest.mark.unit
def test_gate_closed_validator_refuses_modify_pending():
    # Flag unset + qualifying version: refused.
    v = _gate_validator(enabled=False, bridge_version="1.4.1")
    assert v.validate(_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    # Flag set + below-minimum version: refused.
    v_old = _gate_validator(enabled=True, bridge_version="1.4.0")
    assert v_old.validate(_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    # Flag unset + no published version known (empty version): refused.
    v_none = _gate_validator(enabled=False, bridge_version="")
    assert v_none.validate(_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION


# ----------------------------------------------------------------------
# executor: gate-open reaches trade_modify_pending_order; gate-closed
# refuses with zero MT5 calls
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_gate_open_executor_reaches_trade_modify_pending_order(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
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
    executor = CommandExecutor(ctx, _gate_validator(enabled=True, bridge_version="1.4.1"), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.04}))
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"
    assert seen == {"ticket": 9000002, "sl": pytest.approx(1.04), "tp": 0.0}


@pytest.mark.unit
def test_gate_closed_executor_refuses_with_zero_mt5_calls(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.modify_pending_order_sltp",
        lambda ctx_, ticket, sl, tp: (
            calls.append("modify_pending_order_sltp")
            or (_ for _ in ()).throw(AssertionError("MT5 must never be reached when the gate is closed"))
        ),
    )
    executor = CommandExecutor(ctx, _gate_validator(enabled=False, bridge_version="1.4.1"), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.04}))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "UnsupportedOperation"
    assert calls == []


@pytest.mark.unit
def test_gate_closed_below_minimum_executor_refuses_with_zero_mt5_calls(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "nse_bridge.engine.trading.modify_pending_order_sltp",
        lambda ctx_, ticket, sl, tp: (
            calls.append("modify_pending_order_sltp")
            or (_ for _ in ()).throw(AssertionError("MT5 must never be reached below the minimum version"))
        ),
    )
    executor = CommandExecutor(ctx, _gate_validator(enabled=True, bridge_version="1.4.0"), clock=lambda: NOW)
    execution, result = executor.execute(_frame(operation={"type": "modify_pending", "ticket": "9000002", "sl": 1.04}))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "UnsupportedOperation"
    assert calls == []


# ----------------------------------------------------------------------
# channel wiring (behavioural, not a source grep — P2-2): a real
# ServerChannel through _ensure_components must carry the resolved gate
# into its validator.
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_channel_wires_gate_open_when_flag_on_and_version_qualifies(monkeypatch, tmp_path):
    monkeypatch.setattr("nse_bridge.realtime.channel.BRIDGE_VERSION", "1.4.1")
    ctx = _ctx(tmp_path, enable_modify_pending=True)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    assert channel._executor.validator.modify_pending_proven is True


@pytest.mark.unit
def test_channel_wires_gate_closed_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr("nse_bridge.realtime.channel.BRIDGE_VERSION", "1.4.1")
    ctx = _ctx(tmp_path, enable_modify_pending=False)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    assert channel._executor.validator.modify_pending_proven is False


@pytest.mark.unit
def test_channel_wires_gate_closed_below_minimum_version(monkeypatch, tmp_path):
    monkeypatch.setattr("nse_bridge.realtime.channel.BRIDGE_VERSION", "1.4.0")
    ctx = _ctx(tmp_path, enable_modify_pending=True)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    assert channel._executor.validator.modify_pending_proven is False
