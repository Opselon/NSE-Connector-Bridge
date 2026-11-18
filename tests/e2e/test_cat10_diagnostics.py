"""E2E — Category 10 (commands 82-90): diagnostics & monitoring, LIVE terminal."""

from __future__ import annotations

import pytest

from tests.conftest import call

pytestmark = pytest.mark.real_mt5


def test_82_get_mt5_last_error(client):
    data = call(client, "get_mt5_last_error")
    assert data["mt5_last_error"] is not None
    assert "code" in data["mt5_last_error"]


def test_83_get_bridge_logs(client):
    data = call(client, "get_bridge_logs", {"limit": 50})
    assert data["count"] > 0
    entry = data["entries"][-1]
    assert entry["ts"] and entry["level"] and entry["message"]


def test_83b_get_bridge_logs_level_filter(client):
    data = call(client, "get_bridge_logs", {"limit": 100, "level": "ERROR"})
    assert all(e["level"] == "ERROR" for e in data["entries"])


def test_84_get_bridge_health(client):
    data = call(client, "get_bridge_health")
    assert data["healthy"] is True
    assert data["checks"]["mt5_connected"] is True
    assert data["checks"]["total_mt5_calls"] > 0


def test_85_get_bridge_latency(client):
    data = call(client, "get_bridge_latency")
    assert data["overall_avg_ms"] > 0
    assert data["slowest_op"]
    assert "account_info" in data["operations"]


def test_86_get_mt5_connection_metrics(client):
    data = call(client, "get_mt5_connection_metrics")
    assert data["connection"]["state"] == "CONNECTED"
    assert len(data["recent_calls"]) > 0


def test_87_get_trade_execution_metrics(client):
    data = call(client, "get_trade_execution_metrics")
    m = data["metrics"]
    assert "orders_sent" in m and "done" in m and "rejected" in m
    assert isinstance(m["recent"], list)


def test_88_get_bridge_capabilities(client):
    data = call(client, "get_bridge_capabilities")
    assert data["commands_count"] >= 81  # registry keeps growing with trade suites
    assert data["features"]["market_orders"] is True
    names = [c["name"] for c in data["commands"]]
    assert "trade_send_market_order" in names


def test_89_get_mcp_available_tools(client):
    data = call(client, "get_mcp_available_tools")
    assert data["tools_count"] >= 81
    tool = next(t for t in data["tools"] if t["name"] == "calculate_position_size")
    assert tool["input_schema"]["type"] == "object"
    assert "properties" in tool["input_schema"]
    mut = next(t for t in data["tools"] if t["name"] == "trade_send_market_order")
    assert mut["mutates"] is True


def test_90_get_mt5_diagnostic_report(client):
    data = call(client, "get_mt5_diagnostic_report")
    assert data["bridge"]["version"]
    assert data["connection"]["state"] == "CONNECTED"
    assert data["terminal"]["connected"] is True
    assert data["account"]["login"] > 0
    assert data["metrics"]
    assert data["recent_logs"]
