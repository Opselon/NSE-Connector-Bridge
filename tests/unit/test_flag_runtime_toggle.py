"""Unit tests for the P2-1 runtime flag toggle.

The §17.5 (`enable_v2_open_order`) and §17.4 (`enable_modify_pending`)
gate flags used to be process-start snapshots: the validator was built
once in ``ServerChannel._ensure_components`` and ``PUT /settings``
could not change either flag, so only a full Bridge restart (or a
hand-edited config.json) picked a toggle up.

Both flags are now live:

* ``CommandValidator`` accepts a ``bool | Callable[[], bool]`` for
  each gate and re-reads a callable source on every access, so a
  toggle takes effect on the NEXT command with no channel rebuild
  and no process restart. Plain bools keep working (existing
  callers/tests are unchanged).
* ``ServerChannel._ensure_components`` passes live-reading callables
  over ``ctx.settings`` for both gates.
* ``PUT /settings`` accepts both flags, persists them to config.json
  (``persist_settings``), and reports them in ``GET /settings`` —
  no channel restart is triggered for a flag change because none is
  needed any more.

Startup behaviour is unchanged: the v2 flag still defaults ON (env
opt-out both ways), the modify_pending flag still defaults OFF with
the same version floor (>= 1.4.1), and the v2 negotiation gate is
still per-frame and fail-closed.

Label: unit. No MT5 terminal, no network — the MT5 call is mocked
and the "zero MT5 calls" assertions pin the fail-closed behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nse_bridge import BRIDGE_VERSION
from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings, _apply_json_overrides, persist_settings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.commands import (
    CommandValidator,
    RejectionReason,
    modify_pending_gate_open,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
ACCOUNT = "9000001"
DEVICE = "dev-flag-runtime-toggle"


def _frame(operation, **overrides):
    issued = NOW - timedelta(minutes=1)
    command_id = new_uuid7_str()
    frame = {
        "type": "command",
        "command_id": command_id,
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": operation,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": command_id,
    }
    frame.update(overrides)
    return frame


def _open_order_frame(**overrides):
    kwargs = {"protocol_version": "2"}
    kwargs.update(overrides)
    return _frame(
        {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1},
        **kwargs,
    )


def _modify_pending_frame(**overrides):
    return _frame({"type": "modify_pending", "ticket": "9000002", "sl": 1.04}, **overrides)


def _validator(**overrides):
    kwargs = {
        "get_account_id": lambda: ACCOUNT,
        "entitlements": Entitlements(position_manager=True, auto_trade=True),
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return CommandValidator(DEVICE, **kwargs)


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
# validator: a callable gate source is re-read on every command
# ----------------------------------------------------------------------
def test_validator_open_order_follows_live_callable_without_rebuild():
    """Toggling the v2 flag flips validation on the SAME validator."""
    flag = {"v2": True}
    v = _validator(open_order_authorized=lambda: flag["v2"])
    assert v.validate(_open_order_frame()).ok is True
    flag["v2"] = False
    assert v.validate(_open_order_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    flag["v2"] = True
    assert v.validate(_open_order_frame()).ok is True


def test_validator_modify_pending_follows_live_callable_without_rebuild():
    """Toggling the §17.4 flag flips validation on the SAME validator."""
    flag = {"enabled": True}
    v = _validator(
        modify_pending_proven=lambda: modify_pending_gate_open(enabled=flag["enabled"], bridge_version=BRIDGE_VERSION)
    )
    assert v.validate(_modify_pending_frame()).ok is True
    flag["enabled"] = False
    assert v.validate(_modify_pending_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    flag["enabled"] = True
    assert v.validate(_modify_pending_frame()).ok is True


def test_validator_plain_bool_gate_still_works():
    """Bool gate inputs (all existing callers/tests) behave as before."""
    v_on = _validator(open_order_authorized=True, modify_pending_proven=True)
    assert v_on.validate(_open_order_frame()).ok is True
    assert v_on.validate(_modify_pending_frame()).ok is True
    v_off = _validator(open_order_authorized=False, modify_pending_proven=False)
    assert v_off.validate(_open_order_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    assert v_off.validate(_modify_pending_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION


def test_validator_gate_properties_return_plain_bools():
    """Properties resolve callables to plain bools (``is True`` still holds)."""
    v = _validator(open_order_authorized=lambda: True, modify_pending_proven=lambda: False)
    assert v.open_order_authorized is True
    assert v.modify_pending_proven is False


# ----------------------------------------------------------------------
# channel: a real ServerChannel through _ensure_components re-reads
# both flags live — no rebuild, no channel restart, no process restart
# ----------------------------------------------------------------------
def _channel_with_entitlements(tmp_path, monkeypatch, **settings_overrides):
    monkeypatch.setattr("nse_bridge.realtime.channel.BRIDGE_VERSION", "1.4.1")
    ctx = _ctx(tmp_path, **settings_overrides)
    ctx.driver._login = int(ACCOUNT)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    # Entitlements are server-resolved; the test grants them so the
    # validator reaches ok/flag-gate instead of AUTO_TRADE_NOT_ENABLED.
    channel._rest.entitlements = Entitlements(position_manager=True, auto_trade=True)
    return ctx, channel


def test_channel_v2_flag_toggle_applies_without_rebuild(monkeypatch, tmp_path):
    ctx, channel = _channel_with_entitlements(tmp_path, monkeypatch, enable_v2_open_order=True)
    validator = channel._executor.validator
    assert validator.validate(_open_order_frame()).ok is True
    ctx.settings.enable_v2_open_order = False
    assert validator.validate(_open_order_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    ctx.settings.enable_v2_open_order = True
    assert validator.validate(_open_order_frame()).ok is True


def test_channel_modify_pending_flag_toggle_applies_without_rebuild(monkeypatch, tmp_path):
    ctx, channel = _channel_with_entitlements(tmp_path, monkeypatch, enable_modify_pending=True)
    validator = channel._executor.validator
    assert validator.validate(_modify_pending_frame()).ok is True
    ctx.settings.enable_modify_pending = False
    assert validator.validate(_modify_pending_frame()).rejection is RejectionReason.UNSUPPORTED_OPERATION
    ctx.settings.enable_modify_pending = True
    assert validator.validate(_modify_pending_frame()).ok is True


def test_channel_retained_executor_picks_up_flag_on_re_ensure(monkeypatch, tmp_path):
    """The P2-1 bug, pinned: a channel restart keeps the SAME executor
    (``_stop_locked`` never clears it), so a snapshot-built validator
    would keep the OLD flag forever. With live-reading gates the
    retained executor sees the new value after _ensure_components
    runs again on restart."""
    ctx, channel = _channel_with_entitlements(tmp_path, monkeypatch, enable_v2_open_order=True)
    executor_before = channel._executor
    ctx.settings.enable_v2_open_order = False
    channel._ensure_components()
    assert channel._executor is executor_before
    assert channel._executor.validator.validate(_open_order_frame()).rejection is (
        RejectionReason.UNSUPPORTED_OPERATION
    )


def test_channel_v2_negotiation_gate_still_fail_closed(monkeypatch, tmp_path):
    """Startup behaviour unchanged: flag ON + v1 envelope still refuses."""
    _, channel = _channel_with_entitlements(tmp_path, monkeypatch, enable_v2_open_order=True)
    frame = _open_order_frame(protocol_version="1")
    assert channel._executor.validator.validate(frame).rejection is RejectionReason.UNSUPPORTED_OPERATION


# ----------------------------------------------------------------------
# config: both flags persist to config.json and reload
# ----------------------------------------------------------------------
def test_both_flags_persist_to_config_json_and_reload(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    settings.enable_v2_open_order = False
    settings.enable_modify_pending = True
    persist_settings(settings)
    raw = json.loads(settings.config_file.read_text(encoding="utf-8"))
    assert raw["enable_v2_open_order"] is False
    assert raw["enable_modify_pending"] is True
    # Reload exactly the way production startup does: construct, then
    # apply the config.json overrides (load_settings does both).
    reloaded = _apply_json_overrides(BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs")))
    assert reloaded.enable_v2_open_order is False
    assert reloaded.enable_modify_pending is True


# ----------------------------------------------------------------------
# API: PUT /settings toggles both flags, persists them, and the LIVE
# channel validator sees the change with no channel restart
# ----------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx, app
    driver.disconnect()


def _auth(ctx):
    return {"X-API-Key": ctx.settings.local_api_key}


def test_put_settings_toggles_v2_flag_and_persists(client):
    http, ctx, _ = client
    resp = http.put("/api/v1/settings", json={"enable_v2_open_order": False}, headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "enable_v2_open_order" in data["changed"]
    assert data["server_channel_restarted"] is False
    assert data["restart_required"] is False
    assert ctx.settings.enable_v2_open_order is False
    assert json.loads(ctx.settings.config_file.read_text(encoding="utf-8"))["enable_v2_open_order"] is False
    got = http.get("/api/v1/settings", headers=_auth(ctx)).json()["data"]
    assert got["enable_v2_open_order"] is False


def test_put_settings_toggles_modify_pending_flag_and_persists(client):
    http, ctx, _ = client
    resp = http.put("/api/v1/settings", json={"enable_modify_pending": True}, headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "enable_modify_pending" in data["changed"]
    assert data["server_channel_restarted"] is False
    assert ctx.settings.enable_modify_pending is True
    assert json.loads(ctx.settings.config_file.read_text(encoding="utf-8"))["enable_modify_pending"] is True
    got = http.get("/api/v1/settings", headers=_auth(ctx)).json()["data"]
    assert got["enable_modify_pending"] is True


def test_put_settings_flag_toggle_reaches_live_channel_validator(monkeypatch, client):
    """End to end: PUT /settings -> the live channel validator flips
    on the next command, with no channel restart in between."""
    http, ctx, app = client
    monkeypatch.setattr("nse_bridge.realtime.channel.BRIDGE_VERSION", "1.4.1")
    ctx.driver._login = int(ACCOUNT)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    channel._rest.entitlements = Entitlements(position_manager=True, auto_trade=True)
    app.state.channel = channel
    validator = channel._executor.validator
    # The API fixture's device id is auto-generated (not DEVICE), so
    # the frames must carry the channel's real device id.
    dev = channel._device_id()
    assert validator.validate(_open_order_frame(device_id=dev)).ok is True
    assert validator.validate(_modify_pending_frame(device_id=dev)).rejection is RejectionReason.UNSUPPORTED_OPERATION
    resp = http.put(
        "/api/v1/settings",
        json={"enable_v2_open_order": False, "enable_modify_pending": True},
        headers=_auth(ctx),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["server_channel_restarted"] is False
    assert validator.validate(_open_order_frame(device_id=dev)).rejection is RejectionReason.UNSUPPORTED_OPERATION
    assert validator.validate(_modify_pending_frame(device_id=dev)).ok is True
