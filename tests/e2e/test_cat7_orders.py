"""E2E — Category 7 (commands 55-63): pending orders & history with REAL data.

Self-sufficient: seeds its own closed trade + pending order lifecycle.
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from tests.conftest import call

pytestmark = pytest.mark.real_mt5

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
    # BUY_STOP above the market (validated entry side + safe distance)
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

        # 61 cancel -> the order moves to history with CANCELED state
        cancelled = call(client, "trade_cancel_pending_order", {"ticket": ticket})
        assert cancelled["status"] == "done" and cancelled["retcode"] == 10009
        time.sleep(0.3)
        hist = call(client, "get_order_status", {"ticket": ticket})
        assert hist["source"] == "history"
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


# ----------------------------------------------------------------------
# B3 — P3 v2 cancel_order (mock MT5 driver, no live terminal).
#
# These tests do NOT use the session `client` fixture (real MT5):
# they drive the server command path against the fake terminal
# shared with the unit v2 tests, so they verify the v2 wire shape
# end-to-end without placing live orders.
# ----------------------------------------------------------------------
def _mock_v2_ctx(tmp_path):
    """BridgeContext on the fake MT5 terminal, v2 flag on.

    The caller disconnects the driver (see the tests' finally
    blocks); the driver's worker thread is a daemon.
    """
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


def _v2_frame(operation, command_id="01900000-0000-7000-8000-0000000000a1"):
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
def test_v2_cancel_order_cancels_pending_order(tmp_path):
    """P3 v2 §14.2: cancel_order removes the pending order from the
    live book and reports executed — the cat7 cancel path over the
    v2 wire instead of the local REST command."""
    import time as _time
    from types import SimpleNamespace

    ctx, fake = _mock_v2_ctx(tmp_path)
    try:
        fake.orders[7000001] = SimpleNamespace(
            ticket=7000001,
            symbol="EURUSD",
            type=2,
            price_open=1.1050,
            price_current=1.1050,
            volume_current=0.1,
            volume_initial=0.1,
            sl=0.0,
            tp=0.0,
            time_setup=int(_time.time()),
            type_time=0,
            magic=0,
            comment="",
        )
        # The fake's order_send(REMOVE) returns retcode 10009 but
        # does not mutate the book — mirror the broker by removing
        # the order when the cancel is sent.
        real_send = fake.order_send

        def _send(request):
            out = real_send(request)
            if request.get("action") == fake.TRADE_ACTION_REMOVE:
                fake.orders.pop(int(request["order"]), None)
            return out

        fake.order_send = _send
        executor = _v2_executor(ctx)
        execution, result = executor.execute(_v2_frame({"type": "cancel_order", "ticket": "7000001"}))
        assert execution.state.value == "COMPLETED"
        assert result["status"] == "executed"
        assert result["source_tool"] == "trade_cancel_pending_order"
        assert result["command_protocol_version"] == "2"
        assert 7000001 not in fake.orders
    finally:
        ctx.driver.disconnect()


@pytest.mark.unit
def test_v2_cancel_order_refused_under_v1(tmp_path):
    """A cancel_order on a v1 envelope is refused — v2-only ops
    never execute under the old contract."""
    ctx, fake = _mock_v2_ctx(tmp_path)
    try:
        executor = _v2_executor(ctx)
        frame = _v2_frame({"type": "cancel_order", "ticket": "7000001"})
        del frame["protocol_version"]
        execution, result = executor.execute(frame)
        assert execution.state.value == "FAILED"
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "UnsupportedOperation"
        assert fake.send_calls == 0
    finally:
        ctx.driver.disconnect()
