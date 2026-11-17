"""E2E — Category 6 (commands 46-54): positions management with REAL trades (demo, 0.01 lots).

Opens a real 0.01 BUY on XAUUSD, exercises every position command, and closes it.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.conftest import call

SYMBOL = "XAUUSD"
TEST_MAGIC = 888101


def _open_test_position(client: TestClient) -> dict:
    return call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_CAT6"})


def _close_ticket(client: TestClient, ticket: int) -> None:
    try:
        call(client, "trade_close_single_position", {"ticket": ticket})
    except AssertionError:
        pass


@pytest.mark.trade
def test_46_get_trading_open_positions(client):
    data = call(client, "get_trading_open_positions", {})
    assert data["count"] >= 0
    assert isinstance(data["positions"], list)
    for p in data["positions"]:
        assert p["ticket"] > 0 and p["symbol"] and p["volume"] > 0


def test_47_54_position_lifecycle_with_real_trade(client):
    # --- open a real position (uses command 65; dedicated tests in cat8) ---
    opened = _open_test_position(client)
    assert opened["retcode"] == 10009
    assert opened["order_ticket"] and opened["order_ticket"] > 0
    result = opened["deal_ticket"] or opened["order_ticket"]
    positions = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]
    assert positions, "test position did not appear"
    ticket = positions[-1]["ticket"]

    try:
        # 47 position info
        info = call(client, "get_position_info", {"ticket": ticket})
        pos = info["position"]
        assert pos["ticket"] == ticket
        assert pos["symbol"] == SYMBOL
        assert pos["type_label"] == "BUY"
        assert pos["volume"] == pytest.approx(0.01)
        assert pos["price_open"] > 0
        entry = pos["price_open"]

        # 48 profit
        profit = call(client, "get_position_profit", {"ticket": ticket})
        assert profit["ticket"] == ticket and isinstance(profit["profit"], float)

        # 49 volume
        vol = call(client, "get_position_volume", {"ticket": ticket})
        assert vol["volume"] == pytest.approx(0.01)

        # 50 SL/TP (initially empty)
        sltp = call(client, "get_position_sl_tp", {"ticket": ticket})
        assert sltp["sl"] in (0.0, None)

        # 52 modify SL/TP (safe distances: 300 points each side)
        modified = call(client, "trade_modify_sl_tp", {"ticket": ticket, "sl": entry - 3.0, "tp": entry + 3.0})
        assert modified["status"] == "done" and modified["retcode"] == 10009

        # 50 again — SL/TP now set
        sltp = call(client, "get_position_sl_tp", {"ticket": ticket})
        assert sltp["sl"] == pytest.approx(entry - 3.0, abs=0.05)
        assert sltp["tp"] == pytest.approx(entry + 3.0, abs=0.05)

        # 51 risk (SL set -> computable)
        risk = call(client, "get_position_risk", {"ticket": ticket})
        assert risk["risk_amount"] is not None and risk["risk_amount"] > 0
        assert risk["risk_points"] == pytest.approx(300.0, rel=0.1)

        # 53 close single
        closed = call(client, "trade_close_single_position", {"ticket": ticket})
        assert closed["status"] == "done" and closed["retcode"] == 10009

        # verify it is gone
        err = call(client, "get_position_info", {"ticket": ticket}, expect_ok=False)
        assert err["code"] == "TICKET_NOT_FOUND"
    except Exception:
        _close_ticket(client, ticket)
        raise

    # 54 close all — open two fresh positions, then close all XAUUSD
    opened = _open_test_position(client)
    assert opened["retcode"] == 10009
    opened2 = _open_test_position(client)
    assert opened2["retcode"] == 10009
    before = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["count"]
    assert before >= 2
    summary = call(client, "trade_close_all_positions", {"symbol": SYMBOL})
    assert summary["requested"] >= 2
    assert summary["failed"] == 0
    after = call(client, "get_trading_open_positions", {"symbol": SYMBOL})["count"]
    assert after == 0


@pytest.mark.trade
def test_47b_position_info_ticket_not_found(client):
    err = call(client, "get_position_info", {"ticket": 999999999999}, expect_ok=False)
    assert err["code"] == "TICKET_NOT_FOUND"
