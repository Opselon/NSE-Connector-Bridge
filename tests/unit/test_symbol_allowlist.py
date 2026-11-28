"""Unit tests for the config-driven symbol allowlist (audit P1-5).

The Bridge is the last line of defence: before this slice the only
symbol check was existence (``require_symbol_info`` raises
``SYMBOL_NOT_FOUND`` iff the broker does not list the symbol — any
broker-listed symbol was tradable). The allowlist is enforced in the
command validator (step 5c) BEFORE dispatch, so a rejected symbol
never reaches MT5.

Rules pinned here:

* default = current behaviour (no allowlist → every broker symbol
  tradable) so nothing breaks until configured;
* configured = fail-closed (unknown symbol → ``SymbolNotAllowed``,
  never a substitution — the resolver is untouched);
* matching is exact after normalization (``normalize_broker_name``):
  ``EUR/USD`` ≡ ``EURUSD``; a broker suffix is a DIFFERENT symbol
  (``EURUSDm`` must be listed explicitly) — the same spelling rule as
  the policy-envelope ``bounds.allowed_symbols`` bound;
* only commands that name a symbol in the frame (``open_order``) are
  gated — ticket-based ops carry no symbol in the frame;
* a rejected command never consumes its replay id (step 7 runs after
  step 5c), so the id stays reusable.

Label: unit. No MT5 terminal, no network — the "zero MT5 calls"
assertions pin the fail-closed behaviour.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nse_bridge.config import BridgeSettings, load_settings, parse_allowed_symbols
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.commands import (
    CommandExecutor,
    CommandState,
    CommandValidator,
    RejectionReason,
    parse_envelope,
)
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
ACCOUNT = "123456"
DEVICE = "dev-symbol-allowlist"


def _frame(symbol: str = "EURUSD", **overrides):
    issued = NOW - timedelta(minutes=1)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": {"type": "open_order", "symbol": symbol, "side": "buy", "volume": 0.1},
        "protocol_version": "2",
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
        "open_order_authorized": True,
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
# parse helper: normalization, dedup, empty handling
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_parse_allowed_symbols_comma_string():
    assert parse_allowed_symbols("EURUSD,XAUUSD") == frozenset({"EURUSD", "XAUUSD"})
    assert parse_allowed_symbols("EURUSD, XAUUSD , EURUSD") == frozenset({"EURUSD", "XAUUSD"})


@pytest.mark.unit
def test_parse_allowed_symbols_normalizes_spelling():
    # Canonical "EUR/USD" and lowercase are the same entry as "EURUSD".
    assert parse_allowed_symbols("EUR/USD,eurusd") == frozenset({"EURUSD"})
    assert parse_allowed_symbols("XAUUSD.a") == frozenset({"XAUUSDA"})


@pytest.mark.unit
def test_parse_allowed_symbols_suffix_is_a_different_entry():
    # Broker suffixes are NOT folded: EURUSDm is not EURUSD.
    assert parse_allowed_symbols("EURUSDm") == frozenset({"EURUSDM"})
    assert parse_allowed_symbols("EURUSD") != frozenset({"EURUSDM"})


@pytest.mark.unit
def test_parse_allowed_symbols_empty_and_list_inputs():
    assert parse_allowed_symbols("") == frozenset()
    assert parse_allowed_symbols(" , ,") == frozenset()
    assert parse_allowed_symbols(None) == frozenset()
    # Defensive: a config.json array is tolerated, not fatal.
    assert parse_allowed_symbols(["EURUSD", "XAUUSD"]) == frozenset({"EURUSD", "XAUUSD"})


# ----------------------------------------------------------------------
# settings: default unconfigured, env + config.json opt-in
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_allowlist_defaults_to_unconfigured(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.allowed_symbols == ""
    assert settings.allowed_symbols_set is None


@pytest.mark.unit
def test_allowlist_configured_via_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NSE_BRIDGE_ALLOWED_SYMBOLS", "EURUSD, XAUUSD")
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.allowed_symbols_set == frozenset({"EURUSD", "XAUUSD"})


@pytest.mark.unit
def test_allowlist_configured_via_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("NSE_BRIDGE_ALLOWED_SYMBOLS", raising=False)
    monkeypatch.setenv("NSE_BRIDGE_DATA_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"allowed_symbols": "EURUSD,XAUUSD"}), encoding="utf-8")
    settings = load_settings()
    assert settings.allowed_symbols_set == frozenset({"EURUSD", "XAUUSD"})


@pytest.mark.unit
def test_allowlist_empty_value_means_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("NSE_BRIDGE_ALLOWED_SYMBOLS", " , ")
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.allowed_symbols_set is None


# ----------------------------------------------------------------------
# validator: configured allowlist rejects before MT5; unconfigured unchanged
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_unconfigured_validator_accepts_any_symbol():
    v = _validator()
    assert v.validate(_frame("EURUSD")).ok is True
    assert v.validate(_frame("XAUUSD")).ok is True
    assert v.validate(_frame("SOMEOBSCUREPAIR")).ok is True


@pytest.mark.unit
def test_configured_validator_rejects_out_of_list_symbol():
    v = _validator(allowed_symbols=["EURUSD", "XAUUSD"])
    assert v.validate(_frame("GBPUSD")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED
    assert v.validate(_frame("EURUSD")).ok is True
    assert v.validate(_frame("XAUUSD")).ok is True


@pytest.mark.unit
def test_configured_validator_accepts_canonical_spelling_of_listed_symbol():
    v = _validator(allowed_symbols=["EURUSD"])
    assert v.validate(_frame("EUR/USD")).ok is True
    assert v.validate(_frame("eurusd")).ok is True


@pytest.mark.unit
def test_configured_validator_treats_broker_suffix_as_different_symbol():
    v = _validator(allowed_symbols=["EURUSD"])
    assert v.validate(_frame("EURUSDm")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED
    v_suffix = _validator(allowed_symbols=["EURUSDm"])
    assert v_suffix.validate(_frame("EURUSDm")).ok is True
    assert v_suffix.validate(_frame("EURUSD")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED


@pytest.mark.unit
def test_validator_accepts_comma_string_allowlist():
    v = _validator(allowed_symbols="EURUSD, XAUUSD")
    assert v.validate(_frame("XAUUSD")).ok is True
    assert v.validate(_frame("GBPUSD")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED


@pytest.mark.unit
def test_validator_empty_allowlist_value_means_unconfigured():
    assert _validator(allowed_symbols=[]).validate(_frame("GBPUSD")).ok is True
    assert _validator(allowed_symbols="").validate(_frame("GBPUSD")).ok is True
    assert _validator(allowed_symbols=" , ").validate(_frame("GBPUSD")).ok is True


@pytest.mark.unit
def test_ticket_based_ops_carry_no_symbol_and_are_unaffected():
    # close_position names no symbol in the frame — the allowlist has
    # nothing to check, so the op passes step 5c unchanged (its symbol
    # was fixed when the position was opened, under this same gate).
    v = _validator(allowed_symbols=["EURUSD"])
    frame = _frame(operation={"type": "close_position", "ticket": "9000001"})
    frame.pop("protocol_version", None)
    assert v.validate(frame).ok is True


# ----------------------------------------------------------------------
# §6.3 ordering: the symbol check sits at step 5c — after the op
# allowlist / account checks, before entitlement and replay
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_symbol_rejection_before_entitlement():
    v = _validator(
        allowed_symbols=["EURUSD"],
        entitlements=Entitlements(position_manager=False, auto_trade=False),
    )
    assert v.validate(_frame("GBPUSD")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED


@pytest.mark.unit
def test_symbol_rejection_after_wrong_account():
    v = _validator(allowed_symbols=["EURUSD"])
    assert v.validate(_frame("GBPUSD", account_id="999")).rejection is RejectionReason.WRONG_ACCOUNT


@pytest.mark.unit
def test_symbol_rejection_does_not_consume_replay_id():
    v = _validator(allowed_symbols=["EURUSD"])
    frame = _frame("GBPUSD")
    assert v.validate(frame).rejection is RejectionReason.SYMBOL_NOT_ALLOWED
    # Same command_id again: still the symbol rejection, never Replayed.
    assert v.validate(frame).rejection is RejectionReason.SYMBOL_NOT_ALLOWED


@pytest.mark.unit
def test_validator_never_rewrites_the_frame_symbol():
    # Validation normalizes only for comparison: the envelope keeps the
    # spelling the Platform sent, so the resolver still sees it.
    envelope = parse_envelope(_frame("EUR/USD"))
    assert envelope.symbol == "EUR/USD"
    v = _validator(allowed_symbols=["EURUSD"])
    assert v.validate(_frame("EUR/USD")).ok is True


# ----------------------------------------------------------------------
# executor: rejected symbol never reaches MT5 (zero driver calls)
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_executor_rejects_out_of_list_symbol_with_zero_mt5_calls(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    def _no_mt5(*args, **kwargs):
        raise AssertionError("MT5 must never be reached for a symbol outside the allowlist")

    monkeypatch.setattr(Mt5Driver, "run", _no_mt5)
    executor = CommandExecutor(ctx, _validator(allowed_symbols=["EURUSD"]), clock=lambda: NOW)
    execution, result = executor.execute(_frame("GBPUSD"))
    assert execution.state is CommandState.FAILED
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "SymbolNotAllowed"


@pytest.mark.unit
def test_executor_dispatches_in_list_symbol_to_v2_open(monkeypatch, tmp_path):
    # The validator passes an in-list symbol through to dispatch: the
    # v2 seam is reached (patched here so no MT5 call happens), proving
    # the allowlist is not the thing refusing it. Dispatch itself is
    # covered by the v2-hook tests.
    ctx = _ctx(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "nse_bridge.engine.v2_open_order.execute_v2_open_order",
        lambda ctx_, request, **kwargs: (
            seen.update({"symbol": request.symbol}) or {"status": "done", "ticket": 9000001, "retcode": 10009}
        ),
    )
    executor = CommandExecutor(ctx, _validator(allowed_symbols=["EURUSD"]), clock=lambda: NOW)
    execution, result = executor.execute(_frame("EURUSD"))
    assert seen == {"symbol": "EURUSD"}
    assert execution.state is CommandState.COMPLETED
    assert result["status"] == "executed"


# ----------------------------------------------------------------------
# channel wiring (behavioural, not a source grep — P2-2): a real
# ServerChannel through _ensure_components must carry the configured
# allowlist into its validator.
# ----------------------------------------------------------------------
@pytest.mark.unit
def test_channel_wires_configured_allowlist_into_validator(monkeypatch, tmp_path):
    # A real ServerChannel through _ensure_components must carry the
    # configured allowlist into its validator. The test channel has no
    # bound account and no entitlements, so the account is bound here
    # and the assertions pin step 5c exactly: an out-of-list symbol is
    # refused as SymbolNotAllowed, an in-list symbol passes 5c and is
    # refused only later, at step 6 (entitlements).
    monkeypatch.setattr(ServerChannel, "_bound_account_id", lambda self: ACCOUNT)
    ctx = _ctx(tmp_path, allowed_symbols="EURUSD, XAUUSD")
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    validator = channel._executor.validator
    assert validator.validate(_frame("GBPUSD")).rejection is RejectionReason.SYMBOL_NOT_ALLOWED
    assert validator.validate(_frame("EURUSD")).rejection is RejectionReason.AUTO_TRADE_NOT_ENABLED


@pytest.mark.unit
def test_channel_wires_unconfigured_allowlist_as_no_restriction(monkeypatch, tmp_path):
    monkeypatch.setattr(ServerChannel, "_bound_account_id", lambda self: ACCOUNT)
    ctx = _ctx(tmp_path)
    channel = ServerChannel(ctx, EventBus())
    channel._ensure_components()
    assert channel._executor is not None
    # No allowlist configured: the symbol passes step 5c and is refused
    # only later, at step 6 (entitlements) — never SymbolNotAllowed.
    assert channel._executor.validator.validate(_frame("GBPUSD")).rejection is RejectionReason.AUTO_TRADE_NOT_ENABLED
