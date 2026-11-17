"""E2E — Category 7 (commands 55-63): pending orders & history with REAL data.

Self-sufficient: seeds its own closed trade + pending order lifecycle.
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from tests.conftest import call

SYMBOL = "XAUUSD"


def _cancel_ticket(client: TestClient, ticket: int) -> None:
    try:
        call(client, "trade_cancel_pending_order", {"ticket": ticket})
    except AssertionError:
        pass


@pytest.mark.trade
def test_55_get_trading_pending_orders(client):
    data = call(client, "get_trading_pending_orders", {})
    assert data["count"] >= 0
    for o in data["orders"]:
        assert o["ticket"] > 0 and o["type_label"]


@pytest.mark.trade
def test_56_57_61_62_pending_order_lifecycle(client):
    tick = call(client, "get_market_tick", {"symbol": SYMBOL})["tick"]
    ask = tick["ask"]
    # BUY_STOP well above the market (validated entry side + safe distance)
    placed = call(
        client,
        "trade_send_pending_order",
        {"symbol": SYMBOL, "order_type": "BUY_STOP", "volume": 0.01, "price": round(ask + 2.0, 2)},
    )
    assert placed["status"] == "done" and placed["order_ticket"]
    ticket = placed["order_ticket"]

    try:
        # 55 filtered
        data = call(client, "get_trading_pending_orders", {"symbol": SYMBOL})
        assert any(o["ticket"] == ticket for o in data["orders"])

        # 56 info (active source)
        info = call(client, "get_order_info", {"ticket": ticket})
        assert info["source"] == "active"
        assert info["order"]["ticket"] == ticket
        assert info["order"]["type_label"] == "BUY_STOP"

        # 57 status
        status = call(client, "get_order_status", {"ticket": ticket})
        assert status["order"]["state_label"] == "PLACED"

        # 62 modify price +0.10
        modified = call(client, "trade_modify_pending_order", {"ticket": ticket, "price": round(ask + 2.1, 2)})
        assert modified["status"] == "done" and modified["retcode"] == 10009
        info = call(client, "get_order_info", {"ticket": ticket})
        assert info["order"]["price_open"] == pytest.approx(round(ask + 2.1, 2), abs=0.02)

        # 61 cancel
        cancelled = call(client, "trade_cancel_pending_order", {"ticket": ticket})
        assert cancelled["status"] == "done" and cancelled["retcode"] == 10009
        time.sleep(0.2)
        err = call(client, "get_order_info", {"ticket": ticket}, expect_ok=False)
        # cancelled order leaves the active book; may still exist in history
        assert err["code"] == "TICKET_NOT_FOUND" or info is not None
        if err["code"] == "TICKET_NOT_FOUND":
            pass  # fully gone
        else:
            hist = call(client, "get_order_status", {"ticket": ticket})
            assert hist["order"]["state_label"] == "CANCELED"
    except Exception:
        _cancel_ticket(client, ticket)
        raise


@pytest.mark.trade
def test_58_59_60_63_history_with_real_closed_trade(client):
    # seed: open + immediately close 0.01 (creates history order + deals)
    opened = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_CAT7"})
    assert opened["retcode"] == 10009
    positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
    ticket = positions[-1]["ticket"]
    call(client, "trade_close_single_position", {"ticket": ticket})

    # 58 history orders (last 24h default)
    data = call(client, "get_trading_history_orders", {})
    assert data["count"] > 0
    assert any(o["symbol"] == SYMBOL for o in data["orders"])

    # 59 history positions reconstructed from deals
    positions_hist = call(client, "get_trading_history_positions", {})
    assert positions_hist["count"] > 0
    ours = [p for p in positions_hist["positions"] if p["symbol"] == SYMBOL]
    assert ours, "closed test position missing from history positions"
    summary = ours[0]
    assert summary["volume_in"] == pytest.approx(0.01)
    assert summary["volume_out"] == pytest.approx(0.01)
    assert summary["open_time_utc"] and summary["close_time_utc"]
    assert summary["direction"] == "buy"

    # 60 deals history
    deals = call(client, "get_trading_deals_history", {"symbol": SYMBOL})
    assert deals["count"] >= 2  # IN + OUT of the seeded trade
    entries = [d["entry_label"] for d in deals["deals"]]
    assert "IN" in entries and "OUT" in entries

    # 63 execution details for the seeded market order
    details = call(client, "get_order_execution_details", {"ticket": opened["order_ticket"]})
    assert details["order"] is not None
    assert len(details["deals"]) >= 1
    assert details["deals"][0]["price"] > 0
