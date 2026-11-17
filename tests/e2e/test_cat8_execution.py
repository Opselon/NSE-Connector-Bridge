"""E2E — Category 8 (commands 64-72): trade execution with REAL market orders.

Demo-safe: 0.01-0.02 lots on XAUUSD, every test cleans up after itself.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.conftest import call

SYMBOL = "XAUUSD"


def _cleanup(client: TestClient) -> None:
    try:
        call(client, "trade_close_all_positions", {"symbol": SYMBOL})
        data = call(client, "get_trading_pending_orders", {"symbol": SYMBOL})
        for o in data["orders"]:
            try:
                call(client, "trade_cancel_pending_order", {"ticket": o["ticket"]})
            except AssertionError:
                pass
    except AssertionError:
        pass


@pytest.mark.trade
def test_64_trade_send_market_order(client):
    try:
        result = call(
            client,
            "trade_send_market_order",
            {"symbol": SYMBOL, "side": "buy", "volume": 0.01, "comment": "E2E_CAT8"},
        )
        assert result["status"] == "done"
        assert result["retcode"] == 10009
        assert result["order_ticket"] > 0
        assert result["price"] > 0
        positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        assert positions
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_64b_market_order_unknown_symbol_rejected(client):
    err = call(client, "trade_send_market_order", {"symbol": "NOPE_NOT_REAL", "side": "buy", "volume": 0.01}, expect_ok=False)
    assert err["code"] in ("MT5_CALL_FAILED", "TRADE_REJECTED", "TRADE_UNVERIFIED")


@pytest.mark.trade
def test_65_66_buy_sell_orders(client):
    try:
        buy = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_BUY"})
        assert buy["status"] == "done" and buy["retcode"] == 10009
        buy_positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        assert any(p["type_label"] == "BUY" for p in buy_positions)

        sell = call(client, "trade_send_sell_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_SELL"})
        assert sell["status"] == "done" and sell["retcode"] == 10009
        sell_positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        assert any(p["type_label"] == "SELL" for p in sell_positions)
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_67_trade_send_pending_order_and_validation(client):
    try:
        tick = call(client, "get_market_tick", {"symbol": SYMBOL})["tick"]
        ask, bid = tick["ask"], tick["bid"]

        # valid BUY_LIMIT well below the market
        placed = call(
            client,
            "trade_send_pending_order",
            {"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.01, "price": round(ask - 2.5, 2)},
        )
        assert placed["status"] == "done" and placed["order_ticket"]
        assert placed["order"]["type_label"] == "BUY_LIMIT"

        # structurally invalid: BUY_LIMIT ABOVE the ask -> rejected BEFORE dispatch
        err = call(
            client,
            "trade_send_pending_order",
            {"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.01, "price": round(ask + 2.5, 2)},
            expect_ok=False,
        )
        assert err["code"] == "VALIDATION_ERROR"
        assert "price_not_below_ask" in (err["details"] or {}).get("reasons", [])

        # structurally invalid volume: not a multiple of volume_step
        err = call(
            client,
            "trade_send_pending_order",
            {"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.011, "price": round(ask - 2.5, 2)},
            expect_ok=False,
        )
        assert err["code"] == "VALIDATION_ERROR"
        assert "volume_not_multiple_of_step" in (err["details"] or {}).get("reasons", [])

        # no stray orders from the rejected attempts
        orders = call(client, "get_trading_pending_orders", {"symbol": SYMBOL})["orders"]
        assert len(orders) == 1  # only the valid one
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_68_trade_close_partial_position(client):
    try:
        opened = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.02, "comment": "E2E_PARTIAL"})
        assert opened["status"] == "done"
        positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        ticket = positions[-1]["ticket"]

        # validation: partial volume must be smaller than position volume
        err = call(client, "trade_close_partial_position", {"ticket": ticket, "volume": 0.02}, expect_ok=False)
        assert err["code"] == "VALIDATION_ERROR"

        result = call(client, "trade_close_partial_position", {"ticket": ticket, "volume": 0.01})
        assert result["status"] == "done" and result["retcode"] == 10009

        remaining = call(client, "get_position_volume", {"ticket": ticket})
        assert remaining["volume"] == pytest.approx(0.01)
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_69_trade_reverse_position(client):
    try:
        opened = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_REV"})
        assert opened["status"] == "done"
        positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        ticket = positions[-1]["ticket"]

        result = call(client, "trade_reverse_position", {"ticket": ticket})
        assert result["status"] == "done"
        assert result["reversed_to"] == "sell"

        # original ticket gone; a new SELL position exists
        err = call(client, "get_position_info", {"ticket": ticket}, expect_ok=False)
        assert err["code"] == "TICKET_NOT_FOUND"
        positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        assert any(p["type_label"] == "SELL" and p["volume"] == pytest.approx(0.01) for p in positions)
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_70_71_set_position_sl_tp_individually(client):
    try:
        opened = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_SLTP"})
        assert opened["status"] == "done"
        positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
        ticket = positions[-1]["ticket"]
        entry = positions[-1]["price_open"]

        # 70: set SL only (TP must stay empty)
        result = call(client, "trade_set_position_sl", {"ticket": ticket, "sl": round(entry - 3.0, 2)})
        assert result["status"] == "done"
        sltp = call(client, "get_position_sl_tp", {"ticket": ticket})
        assert sltp["sl"] == pytest.approx(entry - 3.0, abs=0.05)
        assert not sltp["tp"]

        # 71: set TP only (SL must stay)
        result = call(client, "trade_set_position_tp", {"ticket": ticket, "tp": round(entry + 3.0, 2)})
        assert result["status"] == "done"
        sltp = call(client, "get_position_sl_tp", {"ticket": ticket})
        assert sltp["tp"] == pytest.approx(entry + 3.0, abs=0.05)
        assert sltp["sl"] == pytest.approx(entry - 3.0, abs=0.05)
    finally:
        _cleanup(client)


@pytest.mark.trade
def test_72_trade_calculate_order_result(client):
    tick = call(client, "get_market_tick", {"symbol": SYMBOL})["tick"]
    entry = tick["ask"]
    data = call(
        client,
        "trade_calculate_order_result",
        {"symbol": SYMBOL, "side": "buy", "volume": 0.01, "price_open": entry, "price_close": round(entry + 1.0, 2)},
    )
    assert data["source"] == "BROKER_NATIVE"
    assert data["profit"] == pytest.approx(1.0, abs=0.2)  # 0.01 lot gold: $1 per $1 move
    assert data["currency"]
