"""E2E — v1.1 management upgrades: settings API, pre-flight checks, channel restart,
close-all magic safety filter."""

from __future__ import annotations

import pytest

from tests.conftest import call

pytestmark = pytest.mark.real_mt5

SYMBOL = "XAUUSD"


def test_settings_get_masked(client):
    data = client.get("/api/v1/settings").json()["data"]
    assert data["device_id"].startswith("nse-")
    assert "config_file" in data
    assert data["magic"] >= 0
    # token must never leak unmasked
    assert "server_token" not in data
    assert "server_token_masked" in data


def test_settings_put_updates_and_applies(client):
    data = client.put(
        "/api/v1/settings",
        json={"magic": 999123, "log_level": "WARNING"},
    ).json()["data"]
    assert "magic" in data["changed"] and "log_level" in data["changed"]
    # restore
    data = client.put("/api/v1/settings", json={"magic": 888101, "log_level": "INFO"}).json()["data"]
    assert "magic" in data["changed"]
    assert isinstance(data["server_channel_restarted"], bool)


def test_settings_put_partial_untouched_fields(client):
    original = client.get("/api/v1/settings").json()["data"]["broker_utc_offset_minutes"]
    new_value = (original + 30) % 721
    data = client.put("/api/v1/settings", json={"broker_utc_offset_minutes": new_value}).json()["data"]
    assert data["changed"] == ["broker_utc_offset_minutes"]
    client.put("/api/v1/settings", json={"broker_utc_offset_minutes": original})


def test_settings_put_invalid_log_level_422(client):
    resp = client.put("/api/v1/settings", json={"log_level": "LOUD"})
    assert resp.status_code == 422


def test_run_connection_checks_structure(client):
    data = call(client, "run_connection_checks")
    ids = [c["id"] for c in data["checks"]]
    assert data["overall"] in ("ok", "warn", "fail")
    for expected in (
        "mt5_attached",
        "terminal_connected",
        "autotrading_enabled",
        "account_permissions",
        "symbol_market",
        "server_channel",
    ):
        assert expected in ids
    # every check carries status + machine-readable fields
    for c in data["checks"]:
        assert c["status"] in ("ok", "fail", "warn")
        assert c["title"]


def test_run_connection_checks_autotrading_detection(client):
    """While the terminal's Algo Trading button is OFF, the check must FAIL with guidance."""
    data = call(client, "run_connection_checks")
    at = next(c for c in data["checks"] if c["id"] == "autotrading_enabled")
    assert at["status"] in ("ok", "fail")  # depends on terminal state at runtime
    if at["status"] == "fail":
        assert "Ctrl+E" in (at["hint"] or "")
        assert data["overall"] == "fail"


def test_server_channel_restart_endpoint(client):
    data = call(client, "server_channel/restart")
    assert data["server_channel"]["enabled"] in (True, False)  # disabled when URL empty


def test_close_all_with_magic_filter_never_touches_manual_positions(client):
    """The account has manual (magic=0) positions — a magic-filtered close-all
    must report zero matches and leave them alone."""
    before = call(client, "get_trading_open_positions", {})
    before_tickets = {p["ticket"] for p in before["positions"]}
    summary = call(client, "trade_close_all_positions", {"symbol": "XAUUSD", "magic": 888101})
    assert summary["requested"] == 0  # no bridge-magic positions open
    assert summary["closed"] == 0 and summary["failed"] == 0
    after = call(client, "get_trading_open_positions", {})
    assert {p["ticket"] for p in after["positions"]} == before_tickets
