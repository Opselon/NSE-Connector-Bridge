"""Unit tests for mandatory local-API auth (Phase 1 — security).

Missing/wrong X-API-Key -> 401; correct key -> 200. The app is built
with the FastAPI TestClient WITHOUT entering its lifespan (no MT5, no
streaming, no server channel). Label: unit.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx
    driver.disconnect()


def test_api_key_is_auto_generated_on_app_creation(client):
    _, ctx = client
    assert ctx.settings.local_api_key
    assert len(ctx.settings.local_api_key) >= 32


def test_missing_api_key_is_401(client):
    http, _ = client
    resp = http.get("/api/v1/commands")
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


def test_wrong_api_key_is_401(client):
    http, _ = client
    resp = http.get("/api/v1/commands", headers={"X-API-Key": "wrong-key-12345678"})
    assert resp.status_code == 401


def test_correct_api_key_is_200(client):
    http, ctx = client
    resp = http.get("/api/v1/commands", headers={"X-API-Key": ctx.settings.local_api_key})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_health_probe_needs_no_key(client):
    http, _ = client
    resp = http.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_put_settings_routes_secrets_to_store_not_config_json(client, tmp_path):
    http, ctx = client
    key = ctx.settings.local_api_key
    new_token = "tok-put-settings-1234567890"
    resp = http.put(
        "/api/v1/settings",
        json={"server_token": new_token},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    # Runtime value updated...
    assert ctx.settings.server_token == new_token
    # ...but the token NEVER lands in plaintext config.json.
    config_file = tmp_path / "config.json"
    assert config_file.is_file()
    assert new_token not in config_file.read_text(encoding="utf-8")
    # ...and it IS in the secret store.
    from nse_bridge.secrets import get_secret_store

    assert get_secret_store(tmp_path).get_secret("server_token") == new_token
    # GET /settings still masks secrets.
    resp = http.get("/api/v1/settings", headers={"X-API-Key": key})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["server_token_set"] is True
    assert new_token not in json.dumps(data)
    assert data["server_token_masked"].endswith(new_token[-4:])


def test_put_settings_rejects_non_wss_server_url(client):
    http, ctx = client
    resp = http.put(
        "/api/v1/settings",
        json={"server_url": "http://example.invalid/ws"},
        headers={"X-API-Key": ctx.settings.local_api_key},
    )
    assert resp.status_code == 422
    assert resp.json()["ok"] is False
