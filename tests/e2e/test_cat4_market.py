"""E2E — Category 4 (commands 28-36): real-time market data, LIVE terminal."""

from __future__ import annotations

import pytest

from tests.conftest import call

pytestmark = pytest.mark.real_mt5

SYMBOL = "XAUUSD"


def test_28_get_market_tick(client):
    data = call(client, "get_market_tick", {"symbol": SYMBOL})
    tick = data["tick"]
    assert tick["bid"] > 0 and tick["ask"] > 0
    assert tick["freshness_ms"] is not None and tick["freshness_ms"] < 60_000
    assert tick["stale"] is False


def test_29_get_market_bid(client):
    data = call(client, "get_market_bid", {"symbol": SYMBOL})
    assert data["price"] > 0
    assert data["time_utc"]


def test_30_get_market_ask(client):
    data = call(client, "get_market_ask", {"symbol": SYMBOL})
    assert data["price"] > 0


def test_31_get_market_last_price(client):
    data = call(client, "get_market_last_price", {"symbol": SYMBOL})
    assert data["last_price"] > 0
    assert data["source"] in ("tick", "m1_bar_close", "unavailable")


def test_32_get_market_depth(client):
    data = call(client, "get_market_depth", {"symbol": SYMBOL})
    assert data["depth_levels"] >= 0
    assert isinstance(data["entries"], list)
    # note may explain no-DOM brokers; not an error


def test_33_get_market_spread_history(client):
    data = call(client, "get_market_spread_history", {"symbol": SYMBOL, "minutes": 30, "limit": 100})
    assert data["points"] > 0
    assert data["avg_spread"] is not None and data["avg_spread"] > 0
    assert len(data["series"]) <= 100
    point = data["series"][0]
    assert point["bid"] > 0 and point["ask"] > 0 and point["spread"] >= 0


def test_34_get_market_tick_volume(client):
    data = call(client, "get_market_tick_volume", {"symbol": SYMBOL})
    assert data["tick_volume"] is not None
    assert data["m1_bar_tick_volume"] is None or data["m1_bar_tick_volume"] >= 0


def test_35_get_market_watchlist(client):
    data = call(client, "get_market_watchlist")
    assert data["count"] > 0
    assert SYMBOL in data["symbols"]


def test_36_subscribe_market_ticks(client):
    data = call(client, "subscribe_market_ticks", {"symbol": SYMBOL, "interval_ms": 500})
    assert data["subscription"]["symbol"] == SYMBOL
    assert data["subscription"]["interval_ms"] == 500
    assert data["current_tick"]["bid"] > 0
    # reflected in bridge status
    status = call(client, "get_bridge_status")
    assert status["subscriptions_active"] >= 1


def test_36b_subscribe_unknown_symbol_fails_cleanly(client):
    err = call(client, "subscribe_market_ticks", {"symbol": "NOPE_NOT_REAL"}, expect_ok=False)
    assert err["code"] == "MT5_CALL_FAILED"
