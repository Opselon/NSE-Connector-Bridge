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


def test_health_reports_broker_down_when_ipc_is_up(client, monkeypatch):
    # Real-MT5 defect 4 (2026-11-18): mt5_connected only measures the
    # IPC link to the terminal. /health must also report the
    # terminal→broker link, so a dead broker link never reads healthy.
    # The observed failure: the terminal lost MetaQuotes-Demo for 4+
    # minutes while ticks went stale, yet /health stayed mt5_connected.
    http, ctx = client
    ctx.driver._connected = True  # simulate the IPC link up
    monkeypatch.setattr(
        ctx.driver,
        "refresh_terminal",
        lambda: {"connected": False, "name": "MetaTrader 5", "trade_allowed": False, "login": 10011755849},
    )
    body = http.get("/health", params={"probe": "full"}).json()
    assert body["mt5_connected"] is True
    assert body["broker_connected"] is False
    assert body["broker"]["login"] == 10011755849


def test_health_reports_broker_up_when_terminal_connected(client, monkeypatch):
    http, ctx = client
    ctx.driver._connected = True
    monkeypatch.setattr(
        ctx.driver,
        "refresh_terminal",
        lambda: {"connected": True, "name": "MetaTrader 5", "trade_allowed": True, "login": 10011755849},
    )
    body = http.get("/health", params={"probe": "full"}).json()
    assert body["broker_connected"] is True


def test_health_reports_broker_unknown_when_ipc_down(client):
    # IPC down: the real probe returns None — broker state is unknown,
    # never invented as healthy or unhealthy.
    http, ctx = client
    assert ctx.driver.connected is False
    body = http.get("/health", params={"probe": "full"}).json()
    assert body["mt5_connected"] is False
    assert body["broker_connected"] is None
    assert body["broker"] is None


def test_health_default_is_light_no_ipc_probe(client, monkeypatch):
    # /health is unauthenticated; probing MT5 IPC on every call let any
    # local process cheaply starve the serialized MT5 worker. The
    # default probe is LIGHT: cached driver state only, broker state
    # honestly unknown (never false-healthy) — the full IPC probe is
    # opt-in via ?probe=full.
    http, ctx = client
    probed: list[bool] = []

    def _fail_if_called():
        probed.append(True)
        raise AssertionError("light probe must not touch MT5 IPC")

    monkeypatch.setattr(ctx.driver, "refresh_terminal", _fail_if_called)
    body = http.get("/health").json()
    assert body["ok"] is True
    assert body["probe"] == "light"
    assert body["broker_connected"] is None  # unknown, not fabricated
    assert body["broker"] is None
    assert probed == []


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
