"""E2E — Category 8 (commands 64-72): trade execution with REAL market orders.

Demo-safe: 0.01-0.02 lots on XAUUSD, every test cleans up after itself.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.conftest import auth_headers, call

pytestmark = pytest.mark.real_mt5

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
    err = call(
        client, "trade_send_market_order", {"symbol": "NOPE_NOT_REAL", "side": "buy", "volume": 0.01}, expect_ok=False
    )
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
        ask = tick["ask"]

        # valid BUY_LIMIT well below the market
        placed = call(
            client,
            "trade_send_pending_order",
            {"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.01, "price": round(ask - 2.5, 2)},
        )
        assert placed["status"] == "done" and placed["order_ticket"]
        assert placed["order"]["type_label"] == "BUY_LIMIT"

        # structurally invalid: BUY_LIMIT ABOVE the ask -> rejected BEFORE dispatch (422)
        resp = client.post(
            "/api/v1/trade_send_pending_order",
            json={"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.01, "price": round(ask + 2.5, 2)},
            headers=auth_headers(client),
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_ERROR"
        assert "buy_limit_price_not_below_ask" in (err["details"] or {}).get("reasons", [])

        # structurally invalid volume: not a multiple of volume_step (422)
        resp = client.post(
            "/api/v1/trade_send_pending_order",
            json={"symbol": SYMBOL, "order_type": "BUY_LIMIT", "volume": 0.011, "price": round(ask - 2.5, 2)},
            headers=auth_headers(client),
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
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

        # validation: partial volume must be smaller than position volume (422)
        resp = client.post(
            "/api/v1/trade_close_partial_position",
            json={"ticket": ticket, "volume": 0.02},
            headers=auth_headers(client),
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"

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


# ----------------------------------------------------------------------
# B3 — P3 v2 open_order (mock MT5 driver, no live terminal).
#
# These tests do NOT use the session `client` fixture (real MT5):
# they drive the server command path against the fake terminal
# shared with the unit v2 tests, so they verify the v2 open path
# end-to-end without placing live orders.
# ----------------------------------------------------------------------
def _mock_v2_ctx(tmp_path):
    """BridgeContext on the fake MT5 terminal, v2 flag on."""
    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from tests.unit.test_execution_v2_hooks import _FakeMt5

    settings = BridgeSettings(
        _env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"), enable_v2_open_order=True
    )
    driver = Mt5Driver(settings)
    fake = _FakeMt5()
    driver._mt5 = fake
    driver._connected = True
    return BridgeContext(settings=settings, driver=driver), fake


def _v2_executor(ctx):
    from nse_bridge.server.commands import CommandExecutor, CommandValidator
    from nse_bridge.server.rest import Entitlements

    validator = CommandValidator(
        "dev-1",
        get_account_id=lambda: "123456",
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        open_order_authorized=True,
    )
    return CommandExecutor(ctx, validator)


def _v2_open_frame(operation, command_id="01900000-0000-7000-8000-0000000000b1"):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {
        "type": "command",
        "command_id": command_id,
        "account_id": "123456",
        "device_id": "dev-1",
        "operation": operation,
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": command_id,
        "protocol_version": "2",
    }


@pytest.mark.unit
def test_v2_open_order_market_creates_position(tmp_path):
    """P3 v2 §14.2: a market open over the v2 wire creates a live
    position through the proven market primitive — the cat8 market
    path, driven by a server command instead of the local REST."""
    ctx, fake = _mock_v2_ctx(tmp_path)
    try:
        executor = _v2_executor(ctx)
        execution, result = executor.execute(
            _v2_open_frame(
                {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1},
                command_id="01900000-0000-7000-8000-0000000000b1",
            )
        )
        assert execution.state.value == "COMPLETED"
        assert result["status"] == "executed"
        assert result["source_tool"] == "trade_send_market_order"
        assert result["command_protocol_version"] == "2"
        assert len(fake.positions) == 1
        assert len(fake.orders) == 0
    finally:
        ctx.driver.disconnect()


@pytest.mark.unit
def test_v2_open_order_pending_creates_order_not_position(tmp_path):
    """P3 v2 §14.2 (PR #20 lesson): a limit open sent under the
    Platform wire names dispatches as PENDING — never silently as
    a market deal."""
    ctx, fake = _mock_v2_ctx(tmp_path)
    try:
        executor = _v2_executor(ctx)
        execution, result = executor.execute(
            _v2_open_frame(
                {
                    "type": "open_order",
                    "symbol": "EURUSD",
                    "side": "sell",
                    "order_type": "limit",
                    "volume": 0.1,
                    "price": 1.1050,
                },
                command_id="01900000-0000-7000-8000-0000000000b2",
            )
        )
        assert execution.state.value == "COMPLETED"
        assert result["status"] == "executed"
        assert result["source_tool"] == "trade_send_pending_order"
        assert len(fake.orders) == 1
        assert len(fake.positions) == 0  # no market deal was opened
    finally:
        ctx.driver.disconnect()


@pytest.mark.unit
def test_v2_open_order_unknown_kind_never_executes(tmp_path):
    """Fail-closed: an unknown order kind is a loud VALIDATION
    failure and nothing reaches MT5 — a malformed open is never
    guessed, and never silently executed as market."""
    ctx, fake = _mock_v2_ctx(tmp_path)
    try:
        executor = _v2_executor(ctx)
        execution, result = executor.execute(
            _v2_open_frame(
                {"type": "open_order", "symbol": "EURUSD", "side": "buy", "order_type": "banana", "volume": 0.1},
                command_id="01900000-0000-7000-8000-0000000000b3",
            )
        )
        assert execution.state.value == "FAILED"
        assert result["status"] == "mcp_failed"
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert fake.send_calls == 0
        assert len(fake.positions) == 0
        assert len(fake.orders) == 0
    finally:
        ctx.driver.disconnect()
