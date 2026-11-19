"""E2E — Category 9 (commands 73-81): risk & calculations, LIVE terminal."""

from __future__ import annotations

import pytest

from tests.conftest import auth_headers, call

pytestmark = pytest.mark.real_mt5

SYMBOL = "XAUUSD"


def test_73_calculate_position_size(client):
    data = call(client, "calculate_position_size", {"symbol": SYMBOL, "risk_percent": 1.0, "stop_points": 300})
    assert data["lots"] > 0
    assert data["lots"] <= data["volume_max"]
    assert data["stop_distance_price"] > 0
    assert data["value_per_price_unit_per_lot"] > 0
    assert data["risk_amount"] > 0


def test_73b_position_size_fixed_amount(client):
    tick = call(client, "get_market_tick", {"symbol": SYMBOL})["tick"]
    data = call(
        client,
        "calculate_position_size",
        {"symbol": SYMBOL, "risk_amount": 100.0, "stop_price": round(tick["ask"] - 10.0, 2), "side": "buy"},
    )
    # 10.0 price distance on gold, $100 risk -> lots = 100 / (10*100) = 0.1
    assert data["lots"] == pytest_approx(0.1)
    assert data["stop_distance_price"] == pytest_approx(10.0)


def pytest_approx(value):
    import pytest

    return pytest.approx(value, rel=0.35)


def test_73c_missing_risk_param_rejected(client):
    resp = client.post(
        "/api/v1/calculate_position_size",
        json={"symbol": SYMBOL, "stop_points": 100},
        headers=auth_headers(client),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_74_calculate_trade_risk(client):
    data = call(client, "calculate_trade_risk", {"symbol": SYMBOL, "side": "buy", "volume": 0.01, "stop_points": 100})
    assert data["risk_amount"] is not None and data["risk_amount"] > 0
    # 0.01 lot gold, 1.0 price stop -> ~ $1
    assert data["risk_amount"] == pytest_approx(1.0)
    assert data["source"] == "BROKER_NATIVE"


def test_75_calculate_stop_loss_price(client):
    data = call(client, "calculate_stop_loss_price", {"symbol": SYMBOL, "side": "buy", "stop_points": 500})
    assert data["price"] > 0
    assert data["distance_points"] == 500
    assert data["price"] < data["entry_price"]  # SL below entry for a buy


def test_76_calculate_take_profit_price(client):
    data = call(client, "calculate_take_profit_price", {"symbol": SYMBOL, "side": "sell", "stop_points": 500})
    assert data["price"] < data["entry_price"]  # TP below entry for a sell


def test_77_calculate_margin_required(client):
    data = call(client, "calculate_margin_required", {"symbol": SYMBOL, "side": "buy", "volume": 0.01})
    assert data["margin_required"] > 0
    assert data["source"] == "BROKER_NATIVE"


def test_78_calculate_margin_level_after_trade(client):
    data = call(client, "calculate_margin_level_after_trade", {"symbol": SYMBOL, "side": "buy", "volume": 0.01})
    assert data["projected_margin"] > 0
    assert data["projected_margin_level"] is None or data["projected_margin_level"] > 0


def test_79_calculate_pip_value(client):
    data = call(client, "calculate_pip_value", {"symbol": SYMBOL, "volume": 1.0})
    assert data["pip_size"] > 0
    assert data["pip_value"] > 0
    # gold: pip 0.1, 1 lot -> $10 per pip
    assert data["pip_value"] == pytest_approx(10.0)


def test_80_calculate_profit_loss(client):
    tick = call(client, "get_market_tick", {"symbol": SYMBOL})["tick"]
    data = call(
        client,
        "calculate_profit_loss",
        {
            "symbol": SYMBOL,
            "side": "buy",
            "volume": 0.01,
            "entry_price": tick["ask"],
            "exit_price": round(tick["ask"] + 2.0, 2),
        },
    )
    assert data["profit"] == pytest_approx(2.0)
    assert data["source"] == "BROKER_NATIVE"


def test_81_validate_trade_request_valid(client):
    data = call(client, "validate_trade_request", {"symbol": SYMBOL, "side": "buy", "volume": 0.01})
    assert data["structural"]["ok"] is True
    assert data["broker"]["available"] is True
    assert "permissions" in data


def test_81b_validate_trade_request_invalid_volume(client):
    data = call(client, "validate_trade_request", {"symbol": SYMBOL, "side": "buy", "volume": 0.007})
    assert data["valid"] is False
    assert "volume_not_multiple_of_step" in data["structural"]["reasons"]
