"""E2E — Category 2 (commands 10-18): trading account, against the LIVE terminal."""

from __future__ import annotations

from tests.conftest import call


def test_10_get_trading_account_info(client):
    data = call(client, "get_trading_account_info")
    acc = data["account"]
    assert acc["login"] > 0
    assert acc["balance"] > 0
    assert acc["equity"] > 0
    assert acc["currency"]
    assert acc["server"]
    assert acc["leverage"] > 0


def test_11_get_account_balance(client):
    data = call(client, "get_account_balance")
    assert data["balance"] > 0
    assert data["currency"]
    assert data["login"] > 0


def test_12_get_account_equity(client):
    data = call(client, "get_account_equity")
    assert data["equity"] > 0
    assert data["currency"]


def test_13_get_account_margin(client):
    data = call(client, "get_account_margin")
    assert data["margin"] >= 0
    assert data["currency"]


def test_14_get_account_free_margin(client):
    data = call(client, "get_account_free_margin")
    assert data["margin_free"] > 0


def test_15_get_account_margin_level(client):
    data = call(client, "get_account_margin_level")
    assert data["margin_level"] is None or data["margin_level"] > 0


def test_16_get_account_profit(client):
    data = call(client, "get_account_profit")
    assert isinstance(data["profit"], float)


def test_17_get_account_currency(client):
    data = call(client, "get_account_currency")
    assert data["currency"]
    assert data["login"] > 0


def test_18_get_trading_permissions(client):
    data = call(client, "get_trading_permissions")
    assert isinstance(data["trade_allowed"], bool)
    assert data["trade_mode"] >= 0
    assert data["trade_mode_label"]
    assert data["account_login"] > 0


def test_19_error_not_connected_envelope(client):
    """Disconnect, expect structured business error, reconnect for later suites."""
    call(client, "disconnect_mt5")
    err = call(client, "get_account_balance", expect_ok=False)
    assert err["code"] == "MT5_NOT_CONNECTED"
    assert err["message"]
    call(client, "reconnect_mt5")
