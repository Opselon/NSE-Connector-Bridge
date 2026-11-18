"""Unit tests for env config / production defaults (Phase 1 — security).

Production default is bridge.nexusverse.space; dev/staging/prod are
selected via configuration only, with no hardcoded dev endpoint.
Label: unit.
"""

from __future__ import annotations

import os

import pytest

from nse_bridge.config import (
    BridgeSettings,
    validate_server_base_url,
    validate_server_url,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)


def _settings(**overrides):
    return BridgeSettings(_env_file=None, **overrides)


def test_production_defaults():
    s = _settings()
    assert s.environment == "production"
    assert s.server_base_url == "https://bridge.nexusverse.space"
    assert s.resolved_server_ws_url == "wss://bridge.nexusverse.space/api/v1/connector/ws"
    assert s.allow_non_loopback is False
    assert s.host == "127.0.0.1"


def test_placeholder_server_url_is_gone():
    s = _settings()
    assert "nse-platform.io" not in s.server_base_url
    assert "nse-platform.io" not in s.resolved_server_ws_url
    assert "nse-platform.io" not in s.server_url


def test_env_overrides_server_base_and_environment(monkeypatch):
    monkeypatch.setenv("NSE_BRIDGE_SERVER_BASE_URL", "https://staging.example.invalid")
    monkeypatch.setenv("NSE_BRIDGE_ENVIRONMENT", "staging")
    s = BridgeSettings(_env_file=None)
    assert s.server_base_url == "https://staging.example.invalid"
    assert s.environment == "staging"
    assert s.resolved_server_ws_url == "wss://staging.example.invalid/api/v1/connector/ws"


def test_explicit_server_url_wins_over_derived(monkeypatch):
    s = _settings(server_url="wss://custom.example.invalid/ws")
    assert s.resolved_server_ws_url == "wss://custom.example.invalid/ws"


def test_validate_server_url():
    validate_server_url("")  # disabled
    validate_server_url("wss://bridge.nexusverse.space/api/v1/connector/ws")
    validate_server_url("ws://127.0.0.1:8765/ws")  # loopback test endpoint
    validate_server_url("ws://example.invalid/ws", allow_insecure=True)  # explicit test config
    with pytest.raises(ValueError, match="wss://"):
        validate_server_url("ws://example.invalid/ws")
    with pytest.raises(ValueError, match="wss://"):
        validate_server_url("http://bridge.nexusverse.space/ws")
    with pytest.raises(ValueError, match="wss://"):
        validate_server_url("https://bridge.nexusverse.space/ws")


def test_validate_server_base_url():
    validate_server_base_url("https://bridge.nexusverse.space")
    with pytest.raises(ValueError, match="https://"):
        validate_server_base_url("http://bridge.nexusverse.space")
    # http is only tolerated in an explicitly-marked dev environment.
    validate_server_base_url("http://127.0.0.1:8000", environment="dev")
    with pytest.raises(ValueError, match="https://"):
        validate_server_base_url("http://127.0.0.1:8000", environment="production")


def test_removed_secret_fields_do_not_exist():
    s = _settings()
    for field in (
        "mt5_password",
        "agent_llm_base_url",
        "agent_llm_api_key",
        "agent_llm_model",
        "server_rest_base",
    ):
        assert not hasattr(s, field), field
