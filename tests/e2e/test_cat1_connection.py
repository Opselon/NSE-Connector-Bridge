"""E2E — Category 1 (commands 1-9): connection & terminal, against the LIVE terminal."""

from __future__ import annotations

import pytest

from tests.conftest import auth_headers, call

pytestmark = pytest.mark.real_mt5


def test_01_connect_mt5(client):
    data = call(client, "connect_mt5")
    assert data["ok"] is True
    assert data["state"] == "CONNECTED"
    assert data["terminal_version"]  # terminal name stamp
    assert isinstance(data["login"], int) and data["login"] > 0


def test_02_get_bridge_status(client):
    data = call(client, "get_bridge_status")
    assert data["bridge_version"]
    assert data["protocol_version"] >= 1
    assert data["mt5_connected"] is True
    assert data["device_id"].startswith("nse-")
    assert data["commands_registered"] >= 9
    assert data["pid"] > 0


def test_03_get_mt5_connection_status(client):
    data = call(client, "get_mt5_connection_status")
    assert data["terminal"]["connected"] is True
    assert data["state"] == "CONNECTED"


def test_04_get_mt5_terminal_info(client):
    data = call(client, "get_mt5_terminal_info")
    term = data["terminal"]
    assert "MetaTrader" in (term["name"] or "")
    assert term["connected"] is True
    assert term["data_path"]  # real data path present
    assert isinstance(term["maxbars"], int) and term["maxbars"] > 0


def test_05_get_mt5_terminal_version(client):
    data = call(client, "get_mt5_terminal_version")
    assert data["api_version"] == 500
    assert isinstance(data["build"], int) and data["build"] > 1000
    assert data["build_date"]


def test_06_get_mt5_server_info(client):
    data = call(client, "get_mt5_server_info")
    assert data["server"]  # e.g. MetaQuotes-Demo
    assert data["currency"]
    assert data["login"] > 0
    # The latency probe runs only when a bot_symbol is configured (there is
    # no default symbol to probe in a fresh test env); tolerate the explicit skip.
    assert data["tick_latency_ms"] is None or 0 < data["tick_latency_ms"] < 5000


def test_07_get_mcp_server_info(client):
    data = call(client, "get_mcp_server_info")
    assert data["tools_count"] >= 9
    assert "rest" in data["transport"]
    assert "/docs" in data["docs_url"]


def test_08_disconnect_mt5(client):
    data = call(client, "disconnect_mt5")
    assert data["ok"] is True
    assert data["state"] == "DISCONNECTED"


def test_09_reconnect_mt5(client):
    data = call(client, "reconnect_mt5")
    assert data["ok"] is True
    assert data["state"] == "CONNECTED"


def test_10_command_after_reconnect_still_works(client):
    data = call(client, "get_account_currency")
    assert data["currency"]


def test_11_swagger_docs_served(client):
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    schema = openapi.json()
    assert schema["info"]["title"] == "NSE MT5 Bridge API"
    assert len(schema["paths"]) >= 12  # 9 cmds + commands + health + docs


def test_12_commands_listing(client):
    data = client.get("/api/v1/commands", headers=auth_headers(client)).json()["data"]
    numbers = [c["number"] for c in data]
    assert numbers[:9] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert all(c["mutates"] is False for c in data if c["category"] == "connection")
