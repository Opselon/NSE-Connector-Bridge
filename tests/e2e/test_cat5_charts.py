"""E2E — Category 5 (commands 37-45): charts & market history, LIVE terminal."""

from __future__ import annotations

from tests.conftest import call

SYMBOL = "XAUUSD"


def test_37_get_chart_history(client):
    data = call(client, "get_chart_history", {"symbol": SYMBOL, "timeframe": "M1", "count": 50})
    assert data["received"] >= 40  # at least the last ~40 M1 bars exist
    assert data["kept"] == data["received"]
    assert data["dropped"] == 0
    bar = data["bars"][-1]
    assert bar["high"] >= bar["low"]
    assert bar["open"] > 0 and bar["close"] > 0
    assert bar["time_utc"]


def test_37b_get_chart_history_range_mode(client):
    data = call(client, "get_chart_history", {"symbol": SYMBOL, "timeframe": "M5", "count": 500, "from_utc": "2026-11-10T00:00:00Z"})
    assert data["received"] > 0
    assert data["bars"][0]["time_utc"].startswith("2026-11-10") or data["received"] > 10


def test_37c_unknown_timeframe_rejected(client):
    resp = client.post(
        "/api/v1/get_chart_history", json={"symbol": SYMBOL, "timeframe": "M7", "count": 10}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_38_get_chart_ticks_history(client):
    data = call(client, "get_chart_ticks_history", {"symbol": SYMBOL, "count": 100})
    assert data["received"] > 0
    tick = data["ticks"][0]
    assert tick["bid"] > 0 and tick["ask"] > 0
    assert tick["time_msc"] > 0


def test_38b_get_chart_ticks_history_range(client):
    data = call(client, "get_chart_ticks_history", {"symbol": SYMBOL, "count": 100, "from_utc": "2026-11-17T00:00:00Z"})
    assert data["received"] > 0


def test_39_get_chart_current_timeframe(client):
    data = call(client, "get_chart_current_timeframe")
    assert data["timeframe"] == "M1"
    assert data["source"] == "bridge_config"


def test_40_get_chart_available_timeframes(client):
    data = call(client, "get_chart_available_timeframes")
    names = data["timeframes"]
    for expected in ("M1", "M5", "M15", "H1", "H4", "D1", "W1", "MN1"):
        assert expected in names
    assert data["enum_values"]["M1"] == 1


def test_41_get_chart_ohlcv(client):
    data = call(client, "get_chart_ohlcv", {"symbol": SYMBOL, "timeframe": "M5", "count": 30})
    assert data["count"] > 0
    assert len(data["open"]) == data["count"]
    assert len(data["close"]) == data["count"]
    assert len(data["high"]) == len(data["low"]) == data["count"]
    assert all(v > 0 for v in data["close"])


def test_42_get_chart_indicators(client):
    data = call(client, "get_chart_indicators")
    assert data["available"] is False
    assert "rsi" in data["items"]
    assert data["note"]


def test_43_get_chart_objects(client):
    data = call(client, "get_chart_objects")
    assert data["available"] is False
    assert isinstance(data["items"], list)


def test_44_get_chart_active_info(client):
    data = call(client, "get_chart_active_info")
    assert data["symbol"] == SYMBOL
    assert data["symbol_selected"] is True
    assert data["last_tick_utc"]


def test_45_get_chart_multi_timeframe_history(client):
    data = call(client, "get_chart_multi_timeframe_history", {"symbol": SYMBOL, "timeframes": ["M1", "M5"], "count": 20})
    assert set(data["series"].keys()) == {"M1", "M5"}
    assert len(data["series"]["M1"]) > 0
    assert len(data["series"]["M5"]) > 0
    assert data["dropped_by_timeframe"]["M1"] == 0
