"""Unit tests for the secret store (Phase 1 — security).

Linux-runnable, no MT5. The dev fallback is exercised with temp
dirs only — the real user config is never touched. Label: unit.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.secrets import (
    DevFallbackSecretStore,
    SecretStoreError,
    ensure_local_api_key,
    get_secret_store,
    initialize_secrets,
    load_secrets_into_settings,
    migrate_plaintext_secrets,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)


def _settings(tmp_path, **overrides):
    return BridgeSettings(_env_file=None, data_dir=tmp_path, **overrides)


def test_dev_fallback_round_trip(tmp_path):
    store = DevFallbackSecretStore(tmp_path)
    assert store.get_secret("server_token") is None
    store.set_secret("server_token", "tok-abc-123")
    store.set_secret("local_api_key", "key-xyz-789")
    assert store.get_secret("server_token") == "tok-abc-123"
    assert store.get_secret("local_api_key") == "key-xyz-789"
    store.delete_secret("server_token")
    assert store.get_secret("server_token") is None
    assert store.get_secret("local_api_key") == "key-xyz-789"


def test_dev_fallback_file_is_clearly_marked(tmp_path):
    store = DevFallbackSecretStore(tmp_path)
    store.set_secret("server_token", "tok-abc-123")
    raw = json.loads((tmp_path / "dev-secrets.json").read_text(encoding="utf-8"))
    assert "DEV-ONLY" in raw["_warning"]
    # The fallback file is NOT config.json.
    assert not (tmp_path / "config.json").exists()


def test_factory_degrades_to_dev_fallback_on_linux_without_keyring(tmp_path):
    # This VM has keyring installed but no Secret Service backend, so
    # the factory must degrade gracefully instead of failing.
    from nse_bridge.secrets import _keyring_usable

    if _keyring_usable():  # a real desktop backend — round-trip still works
        pytest.skip("a real keyring backend is present on this machine")
    store = get_secret_store(tmp_path)
    assert isinstance(store, DevFallbackSecretStore)
    store.set_secret("server_token", "tok-1")
    assert store.get_secret("server_token") == "tok-1"


def test_dev_fallback_refuses_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SecretStoreError, match="refused on Windows"):
        DevFallbackSecretStore(tmp_path)
    # The factory must also refuse (no Credential Manager here) rather
    # than silently downgrading to plaintext on Windows.
    with pytest.raises(SecretStoreError, match="no usable Windows secret store"):
        get_secret_store(tmp_path)


def test_migrate_plaintext_config_json(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "device_id": "nse-test-device",
                "server_url": "wss://bridge.nexusverse.space/api/v1/connector/ws",
                "server_token": "tok-plaintext-123",
                "local_api_key": "key-plaintext-456",
                "mt5_password": "hunter2",
                "agent_llm_api_key": "sk-legacy",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    store = DevFallbackSecretStore(tmp_path)
    migrated = migrate_plaintext_secrets(config_file, store)
    assert sorted(migrated) == ["local_api_key", "server_token"]
    assert store.get_secret("server_token") == "tok-plaintext-123"
    assert store.get_secret("local_api_key") == "key-plaintext-456"
    # Legacy credentials are dropped, never re-stored.
    assert store.get_secret("mt5_password") is None  # type: ignore[arg-type]
    remaining = json.loads(config_file.read_text(encoding="utf-8"))
    assert "server_token" not in remaining
    assert "local_api_key" not in remaining
    assert "mt5_password" not in remaining
    assert "agent_llm_api_key" not in remaining
    # Non-secret keys are preserved.
    assert remaining["device_id"] == "nse-test-device"
    assert remaining["server_url"].startswith("wss://bridge.nexusverse.space")


def test_migrate_missing_or_invalid_config_is_noop(tmp_path):
    store = DevFallbackSecretStore(tmp_path)
    assert migrate_plaintext_secrets(tmp_path / "config.json", store) == []
    bad = tmp_path / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    assert migrate_plaintext_secrets(bad, store) == []


def test_load_secrets_into_settings_prefers_existing_values(tmp_path):
    store = DevFallbackSecretStore(tmp_path)
    store.set_secret("server_token", "tok-from-store")
    settings = _settings(tmp_path, server_token="tok-from-env")
    loaded = load_secrets_into_settings(settings, store)
    assert loaded == []  # existing value wins
    assert settings.server_token == "tok-from-env"
    settings2 = _settings(tmp_path)
    loaded2 = load_secrets_into_settings(settings2, store)
    assert loaded2 == ["server_token"]
    assert settings2.server_token == "tok-from-store"


def test_ensure_local_api_key_generates_stores_and_reuses(tmp_path):
    settings = _settings(tmp_path)
    key = ensure_local_api_key(settings)
    assert len(key) >= 32
    assert settings.local_api_key == key
    store = DevFallbackSecretStore(tmp_path)
    assert store.get_secret("local_api_key") == key
    # Second call (fresh settings object, same data dir) reuses it.
    settings2 = _settings(tmp_path)
    assert ensure_local_api_key(settings2) == key


def test_ensure_local_api_key_keeps_preset_key(tmp_path):
    settings = _settings(tmp_path, local_api_key="preset-key-12345678")
    assert ensure_local_api_key(settings) == "preset-key-12345678"
    store = DevFallbackSecretStore(tmp_path)
    assert store.get_secret("local_api_key") is None  # not persisted behind our back


def test_initialize_secrets_end_to_end(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"server_token": "tok-migrated", "log_level": "INFO"}),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    migrated = initialize_secrets(settings, strict=True)
    assert migrated == ["server_token"]
    assert settings.server_token == "tok-migrated"
    assert settings.local_api_key  # auto-generated
    remaining = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "server_token" not in remaining
