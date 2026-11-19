"""E2E — Category 3 (commands 19-27): symbols & market specifications, LIVE terminal."""

from __future__ import annotations

import pytest

from tests.conftest import auth_headers, call

pytestmark = pytest.mark.real_mt5

SYMBOL = "XAUUSD"


def test_19_get_trading_symbols(client):
    data = call(client, "get_trading_symbols", {"limit": 100})
    assert data["total_on_server"] > 0
    assert data["returned"] > 0
    assert len(data["symbols"]) <= 100
    assert all(s["name"] for s in data["symbols"])


def test_19b_get_trading_symbols_filter(client):
    data = call(client, "get_trading_symbols", {"filter": "XAU", "limit": 500})
    assert data["returned"] > 0
    assert all("XAU" in s["name"].upper() for s in data["symbols"])


def test_19c_unknown_symbol_not_in_list(client):
    data = call(client, "get_trading_symbols", {"filter": "ZZZ_NO_SUCH_SYMBOL_ZZZ"})
    assert data["returned"] == 0


def test_20_get_symbol_info(client):
    data = call(client, "get_symbol_info", {"symbol": SYMBOL})
    spec = data["symbol"]
    assert spec["name"] == SYMBOL
    assert spec["digits"] >= 0
    assert spec["point"] > 0
    assert spec["volume_min"] > 0
    assert spec["volume_max"] >= spec["volume_min"]
    assert spec["trade_contract_size"] > 0


def test_20b_symbol_not_found_business_error(client):
    err = call(client, "get_symbol_info", {"symbol": "NOPE_NOT_REAL"}, expect_ok=False)
    assert err["code"] == "SYMBOL_NOT_FOUND"


def test_21_get_symbol_price(client):
    data = call(client, "get_symbol_price", {"symbol": SYMBOL})
    assert data["bid"] > 0
    assert data["ask"] > 0
    assert data["ask"] >= data["bid"]
    assert data["time_utc"]


def test_22_get_symbol_tick_size(client):
    data = call(client, "get_symbol_tick_size", {"symbol": SYMBOL})
    assert data["tick_size"] > 0


def test_23_get_symbol_point(client):
    data = call(client, "get_symbol_point", {"symbol": SYMBOL})
    assert data["point"] > 0


def test_24_get_symbol_digits(client):
    data = call(client, "get_symbol_digits", {"symbol": SYMBOL})
    assert data["digits"] >= 0


def test_25_get_symbol_spread(client):
    data = call(client, "get_symbol_spread", {"symbol": SYMBOL})
    assert data["spread_price_live"] is not None and data["spread_price_live"] >= 0
    assert data["bid"] > 0 and data["ask"] > 0


def test_26_get_symbol_trading_sessions(client):
    data = call(client, "get_symbol_trading_sessions", {"symbol": SYMBOL})
    assert "session_deals" in data["session_stats"]
    assert data["note"]


def test_27_get_symbol_trade_constraints(client):
    data = call(client, "get_symbol_trade_constraints", {"symbol": SYMBOL})
    assert data["volume_min"] > 0
    assert data["volume_step"] > 0
    assert data["trade_stops_level"] >= 0
    assert data["trade_freeze_level"] >= 0
    assert data["trade_mode_label"]


def test_27b_validation_missing_symbol(client):
    resp = client.post("/api/v1/get_symbol_price", json={}, headers=auth_headers(client))
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
