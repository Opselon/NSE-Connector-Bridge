"""Unit tests for the S4 (v1.3.0) v2 ``open_order`` hooks.

Label: unit. No MT5 — a fake MT5 module is injected into the driver.
Covers: the hook refuses loudly when the flag is off, refuses loudly
when protocol v2 has not been negotiated, never touches the driver
on refusal, dispatches through the proven primitives only when BOTH
gates are open, and is NOT wired into the live server command path
(the §17.5 gate still rejects server-sent ``open_order`` frames).

The Bridge alone does NOT enable new-position trading — these tests
pin the refusal behavior, not a live trading surface.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.engine.v2_open_order import (
    V2Negotiation,
    V2OpenOrderRequest,
    check_v2_open_order_available,
    execute_v2_open_order,
)
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver

pytestmark = pytest.mark.unit


class _FakeMt5:
    """Minimal fake: enough broker state for a market/pending open."""

    TRADE_ACTION_DEAL = 0
    TRADE_ACTION_PENDING = 1
    TRADE_ACTION_SLTP = 2
    TRADE_ACTION_MODIFY = 3
    TRADE_ACTION_REMOVE = 4
    TRADE_ACTION_CLOSE_BY = 5
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 1

    def __init__(self):
        self.positions: dict[int, SimpleNamespace] = {}
        self.orders: dict[int, SimpleNamespace] = {}
        self.send_calls = 0
        self.sent_requests: list[dict] = []
        self._next_order_ticket = 7000001
        self._next_position_ticket = 9000001

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(time.time()), bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)

    def symbol_info(self, name):
        return SimpleNamespace(
            name=name,
            description=f"{name} desc",
            path=f"Test\\{name}",
            visible=True,
            selected=True,
            trade_mode=4,
            digits=5,
            point=0.00001,
            spread=20,
            spread_float=2.0,
            trade_tick_size=0.00001,
            trade_tick_value=1.0,
            trade_tick_value_loss=1.0,
            trade_tick_value_profit=1.0,
            trade_contract_size=100000.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            volume_limit=0.0,
            trade_stops_level=10,
            trade_freeze_level=0,
            trade_exemode=2,
            trade_calc_mode=0,
            filling_mode=3,
            swap_mode=1,
            swap_long=0.5,
            swap_short=-0.7,
            swap_rollover3days=3,
            currency_base=None,
            currency_profit=None,
            currency_margin=None,
            margin_initial=1000.0,
            margin_maintenance=500.0,
            margin_hedged=250.0,
            order_gtc_mode=0,
            start_time=0,
            expiration_time=0,
            session_deals=12,
            session_turnover=345.0,
            session_open=1.10,
            session_close=1.11,
            session_volume=7.5,
        )

    def symbols_get(self):
        return [self.symbol_info("EURUSD")]

    def symbol_select(self, name, enable=True):
        return True

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(int(ticket))
            return [p] if p is not None else []
        rows = list(self.positions.values())
        if symbol:
            rows = [p for p in rows if p.symbol == symbol]
        return rows

    def orders_get(self, ticket=None, symbol=None):
        if ticket is not None:
            o = self.orders.get(int(ticket))
            return [o] if o is not None else []
        return list(self.orders.values())

    def account_info(self):
        return SimpleNamespace(
            login=123456,
            name="Test",
            server="Test-Server",
            currency="USD",
            trade_mode=0,
            leverage=100,
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            profit=0.0,
            swap=0.0,
            commission=0.0,
            credit=0.0,
            margin_so_mode=0,
            margin_so_call=50.0,
            margin_so_so=20.0,
            margin_mode=2,
            trade_allowed=True,
            trade_expert=True,
        )

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    def order_check(self, request):
        return SimpleNamespace(
            retcode=0,
            balance=10000.0,
            equity=10000.0,
            profit=0.0,
            margin=0.0,
            margin_free=10000.0,
            margin_level=0.0,
            comment="Done",
            request=dict(request),
        )

    def order_send(self, request):
        self.sent_requests.append(dict(request))
        self.send_calls += 1
        action = request.get("action")
        if action == self.TRADE_ACTION_DEAL and "position" not in request:
            ticket = self._next_position_ticket
            self._next_position_ticket += 1
            self.positions[ticket] = SimpleNamespace(
                ticket=ticket,
                symbol=request.get("symbol"),
                type=request.get("type"),
                volume=request.get("volume"),
                price_open=request.get("price"),
                price_current=request.get("price"),
                sl=request.get("sl", 0.0),
                tp=request.get("tp", 0.0),
                profit=0.0,
                swap=0.0,
                commission=0.0,
                magic=request.get("magic", 0),
                comment=request.get("comment", ""),
                time=int(time.time()),
            )
            return SimpleNamespace(
                retcode=10009,
                order=ticket,
                deal=8000001,
                price=request.get("price"),
                volume=request.get("volume"),
                comment="",
            )
        if action == self.TRADE_ACTION_PENDING:
            ticket = self._next_order_ticket
            self._next_order_ticket += 1
            self.orders[ticket] = SimpleNamespace(
                ticket=ticket,
                symbol=request.get("symbol"),
                type=request.get("type"),
                price_open=request.get("price"),
                price_current=request.get("price"),
                volume_current=request.get("volume"),
                volume_initial=request.get("volume"),
                sl=request.get("sl", 0.0),
                tp=request.get("tp", 0.0),
                time_setup=int(time.time()),
                type_time=request.get("type_time"),
                magic=request.get("magic", 0),
                comment=request.get("comment", ""),
            )
            return SimpleNamespace(
                retcode=10009,
                order=ticket,
                deal=0,
                price=request.get("price"),
                volume=request.get("volume"),
                comment="",
            )
        return SimpleNamespace(retcode=10009, order=0, deal=0, price=0.0, volume=0.0, comment="")


@pytest.fixture
def ctx(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _FakeMt5()
    driver._mt5 = fake
    driver._connected = True
    context = BridgeContext(settings=settings, driver=driver)
    try:
        yield context
    finally:
        driver.disconnect()


NEGOTIATED = V2Negotiation(negotiated=True, schema_version="2")
MARKET = V2OpenOrderRequest(symbol="EURUSD", side="buy", volume=0.1)


# ----------------------------------------------------------------------
# refusal: flag off
# ----------------------------------------------------------------------
def test_v2_flag_defaults_off_in_settings(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.enable_v2_open_order is False


def test_v2_flag_opt_in_via_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NSE_BRIDGE_ENABLE_V2_OPEN_ORDER", "1")
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    assert settings.enable_v2_open_order is True


def test_v2_refuses_when_flag_off_even_if_negotiated(ctx):
    """Flag off + negotiated platform = still refused. The flag is an
    independent gate; negotiation alone never opens the hook."""
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, MARKET, flag_enabled=False, negotiation=NEGOTIATED)
    assert excinfo.value.code is ErrorCode.DISABLED
    assert "flag" in excinfo.value.message.lower()
    assert ctx.driver._mt5.send_calls == 0  # nothing reached MT5


def test_v2_check_names_the_closed_flag_gate():
    with pytest.raises(BridgeError) as excinfo:
        check_v2_open_order_available(flag_enabled=False, negotiation=NEGOTIATED)
    assert excinfo.value.code is ErrorCode.DISABLED
    assert excinfo.value.details["gate"] == "flag"


# ----------------------------------------------------------------------
# refusal: not negotiated
# ----------------------------------------------------------------------
def test_v2_refuses_when_not_negotiated_even_if_flag_on(ctx):
    """Flag on + no v2 handshake = refused. The flag alone does not
    enable trading — the Platform P3 negotiation is the second gate."""
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, MARKET, flag_enabled=True, negotiation=V2Negotiation())
    assert excinfo.value.code is ErrorCode.DISABLED
    assert "negotiat" in excinfo.value.message.lower()
    assert ctx.driver._mt5.send_calls == 0


def test_v2_refuses_when_negotiation_is_none(ctx):
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, MARKET, flag_enabled=True, negotiation=None)
    assert excinfo.value.code is ErrorCode.DISABLED
    assert excinfo.value.details["gate"] == "negotiation"
    assert ctx.driver._mt5.send_calls == 0


def test_v2_refuses_when_schema_version_mismatches(ctx):
    """A negotiated handshake carrying a schema this Bridge does not
    speak is refused — the schema is the contract version, and a
    mismatch means the two sides disagree about the wire shape."""
    wrong = V2Negotiation(negotiated=True, schema_version="2026-11-24")
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, MARKET, flag_enabled=True, negotiation=wrong)
    assert excinfo.value.code is ErrorCode.DISABLED
    assert ctx.driver._mt5.send_calls == 0


# ----------------------------------------------------------------------
# negotiation: protocol_version "2" is honoured only by a v2 Bridge
# ----------------------------------------------------------------------
def test_negotiate_protocol_version_honours_v2_for_v2_bridge():
    from nse_bridge.engine.v2_open_order import negotiate_protocol_version

    n = negotiate_protocol_version("2")
    assert n.negotiated is True
    assert n.schema_version == "2"


def test_negotiate_protocol_version_falls_back_to_v1():
    from nse_bridge.engine.v2_open_order import negotiate_protocol_version

    assert negotiate_protocol_version(None).negotiated is False
    assert negotiate_protocol_version("1").negotiated is False
    assert negotiate_protocol_version("99").negotiated is False


def test_negotiate_protocol_version_refuses_v2_for_old_bridge():
    from nse_bridge.engine.v2_open_order import negotiate_protocol_version

    assert negotiate_protocol_version("2", bridge_version="1.2.9").negotiated is False
    assert negotiate_protocol_version("2", bridge_version="1.3.0").negotiated is True


def test_version_at_least_comparison():
    from nse_bridge.engine.v2_open_order import version_at_least

    assert version_at_least("1.3.1", "1.3.0") is True
    assert version_at_least("1.3.0", "1.3.0") is True
    assert version_at_least("1.2.9", "1.3.0") is False
    assert version_at_least("banana", "1.3.0") is False  # malformed never qualifies


# ----------------------------------------------------------------------
# dispatch: both gates open (the P3-ready path, still not live-wired)
# ----------------------------------------------------------------------
def test_v2_dispatches_market_order_when_both_gates_open(ctx):
    out = execute_v2_open_order(ctx, MARKET, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    assert out["retcode"] == 10009
    assert ctx.driver._mt5.send_calls == 1
    assert ctx.driver._mt5.sent_requests[0]["comment"] == "NSE_OPEN"


def test_v2_dispatches_pending_order_when_both_gates_open(ctx):
    req = V2OpenOrderRequest(symbol="EURUSD", side="sell", volume=0.1, order_kind="limit", price=1.1050)
    out = execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    assert out["order_ticket"] is not None
    assert ctx.driver._mt5.send_calls == 1


def test_v2_pending_without_price_is_validation_error(ctx):
    req = V2OpenOrderRequest(symbol="EURUSD", side="buy", volume=0.1, order_kind="limit")
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert ctx.driver._mt5.send_calls == 0


# ----------------------------------------------------------------------
# wired into the live command path — but both gates still default closed
# ----------------------------------------------------------------------
def test_v2_hook_is_wired_into_command_path_but_gates_default_closed():
    """B3 wiring: the server command path DOES import the v2 hook
    now (the dormant-hook pin is retired), and the channel passes
    the operator flag into the validator — but a server-sent
    open_order is still refused unless BOTH gates are open (flag +
    negotiated v2 envelope). The hook's existence changes nothing
    on the live path while the gates are closed."""
    repo = Path(__file__).resolve().parents[2]
    commands_src = (repo / "nse_bridge" / "server" / "commands.py").read_text(encoding="utf-8")
    channel_src = (repo / "nse_bridge" / "realtime" / "channel.py").read_text(encoding="utf-8")
    assert "v2_open_order" in commands_src
    assert "execute_v2_open_order" in commands_src
    assert "enable_v2_open_order" in channel_src
    assert "open_order_authorized" in channel_src


def test_server_open_order_still_rejected_by_default_gate(ctx):
    """End-to-end guard at the validator level: with the default
    (closed) §17.5 gate, a server-sent open_order frame is
    UnsupportedOperation — the v2 hook's existence changes nothing
    on the live path."""
    from datetime import UTC, datetime, timedelta

    from nse_bridge.server.commands import CommandValidator, RejectionReason
    from nse_bridge.server.rest import Entitlements

    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    validator = CommandValidator(
        "dev-1",
        get_account_id=lambda: "123456",
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: now,
    )
    issued = now - timedelta(minutes=1)
    frame = {
        "type": "command",
        "command_id": "01900000-0000-7000-8000-000000000001",
        "account_id": "123456",
        "device_id": "dev-1",
        "operation": {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "01900000-0000-7000-8000-000000000001",
    }
    outcome = validator.validate(frame)
    assert outcome.rejection is RejectionReason.UNSUPPORTED_OPERATION
    assert ctx.driver._mt5.send_calls == 0


# ----------------------------------------------------------------------
# from_operation: Platform §14.2 wire aliases
# ----------------------------------------------------------------------
PLATFORM_LIMIT = {
    "type": "open_order",
    "ticket": "0",
    "symbol": "EURUSD",
    "side": "sell",
    "order_type": "limit",
    "volume": 0.1,
    "price": 1.1050,
}

PLATFORM_STOP_LIMIT = {
    "type": "open_order",
    "ticket": "0",
    "symbol": "EURUSD",
    "side": "buy",
    "order_type": "stop_limit",
    "volume": 0.1,
    "price": 1.1050,
    "stop_limit": 1.1040,
}


def test_v2_from_operation_accepts_platform_order_type():
    req = V2OpenOrderRequest.from_operation(PLATFORM_LIMIT)
    assert req.order_kind == "limit"
    assert req.price == pytest.approx(1.1050)
    assert req.stoplimit is None


def test_v2_from_operation_accepts_platform_stop_limit():
    req = V2OpenOrderRequest.from_operation(PLATFORM_STOP_LIMIT)
    assert req.order_kind == "stop_limit"
    assert req.stoplimit == pytest.approx(1.1040)


def test_v2_from_operation_bridge_names_still_work():
    req = V2OpenOrderRequest.from_operation(
        {"type": "open_order", "symbol": "EURUSD", "side": "buy", "order_kind": "stop", "volume": 0.1, "price": 1.1050}
    )
    assert req.order_kind == "stop"


def test_v2_from_operation_bridge_name_wins_over_platform_alias():
    req = V2OpenOrderRequest.from_operation(
        {**PLATFORM_LIMIT, "order_kind": "stop", "stoplimit": 1.1040, "stop_limit": 1.1030}
    )
    assert req.order_kind == "stop"
    assert req.stoplimit == pytest.approx(1.1040)


def test_v2_from_operation_no_kind_defaults_market_only_when_no_order_type():
    req = V2OpenOrderRequest.from_operation({"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1})
    assert req.order_kind == "market"


def test_v2_from_operation_unknown_kind_fails_loudly():
    with pytest.raises(ValueError, match="unknown order kind"):
        V2OpenOrderRequest.from_operation({**PLATFORM_LIMIT, "order_type": "banana"})


def test_v2_from_operation_missing_required_fields_fail_loudly():
    with pytest.raises(ValueError, match="missing 'symbol'"):
        V2OpenOrderRequest.from_operation({"type": "open_order", "side": "buy", "volume": 0.1})
    with pytest.raises(ValueError, match="missing 'volume'"):
        V2OpenOrderRequest.from_operation({"type": "open_order", "symbol": "EURUSD", "side": "buy"})


def test_v2_from_operation_wrong_type_fails_loudly():
    with pytest.raises(ValueError, match="unexpected type"):
        V2OpenOrderRequest.from_operation({"type": "close_position", "ticket": "9000001"})


def test_v2_dispatches_platform_shaped_pending_order(ctx):
    """Regression: a pending entry sent under the Platform wire names
    must dispatch as PENDING — never silently as a market deal."""
    from nse_bridge.engine.execution import PENDING_TYPES

    req = V2OpenOrderRequest.from_operation(PLATFORM_LIMIT)
    out = execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    assert out["order_ticket"] is not None
    assert ctx.driver._mt5.send_calls == 1
    assert len(ctx.driver._mt5.orders) == 1
    assert len(ctx.driver._mt5.positions) == 0
    assert ctx.driver._mt5.sent_requests[0]["type"] == PENDING_TYPES["SELL_LIMIT"]


def test_v2_dispatches_platform_shaped_stop_limit_with_stoplimit(ctx):
    from nse_bridge.engine.execution import PENDING_TYPES

    req = V2OpenOrderRequest.from_operation(PLATFORM_STOP_LIMIT)
    out = execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    assert ctx.driver._mt5.sent_requests[0]["type"] == PENDING_TYPES["BUY_STOP_LIMIT"]
    assert ctx.driver._mt5.sent_requests[0]["stoplimit"] == pytest.approx(1.1040)


# ----------------------------------------------------------------------
# from_operation: fail-closed on malformed v2 fields
# ----------------------------------------------------------------------
def test_v2_from_operation_stop_limit_without_stop_limit_fails():
    with pytest.raises(ValueError, match="requires stop_limit"):
        V2OpenOrderRequest.from_operation({**PLATFORM_LIMIT, "order_type": "stop_limit", "price": 1.1050})


def test_v2_from_operation_pending_without_price_fails():
    with pytest.raises(ValueError, match="requires price"):
        V2OpenOrderRequest.from_operation(
            {"type": "open_order", "symbol": "EURUSD", "side": "buy", "order_type": "limit", "volume": 0.1}
        )


def test_v2_from_operation_market_with_price_fails():
    """A price on a market entry is forbidden by §14.2 — refusing
    beats silently ignoring a field the sender thought mattered."""
    with pytest.raises(ValueError, match="forbids price"):
        V2OpenOrderRequest.from_operation(
            {
                "type": "open_order",
                "symbol": "EURUSD",
                "side": "buy",
                "order_type": "market",
                "volume": 0.1,
                "price": 1.10,
            }
        )


def test_v2_from_operation_negative_deviation_fails():
    with pytest.raises(ValueError, match="deviation"):
        V2OpenOrderRequest.from_operation({**PLATFORM_LIMIT, "order_type": "market", "deviation": -5, "price": None})


def test_v2_from_operation_expiry_over_max_fails():
    with pytest.raises(ValueError, match="expiry_secs"):
        V2OpenOrderRequest.from_operation(
            {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1, "expiry_secs": 901}
        )


def test_v2_from_operation_non_positive_sl_tp_fail():
    with pytest.raises(ValueError, match="sl must be > 0"):
        V2OpenOrderRequest.from_operation(
            {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1, "sl": 0.0}
        )
    with pytest.raises(ValueError, match="tp must be > 0"):
        V2OpenOrderRequest.from_operation(
            {"type": "open_order", "symbol": "EURUSD", "side": "buy", "volume": 0.1, "tp": -1.0}
        )


def test_v2_from_operation_unknown_side_fails():
    with pytest.raises(ValueError, match="unknown side"):
        V2OpenOrderRequest.from_operation({"type": "open_order", "symbol": "EURUSD", "side": "sideways", "volume": 0.1})


# ----------------------------------------------------------------------
# passthrough: deviation / expiry / comment reach the MT5 request
# ----------------------------------------------------------------------
def test_v2_market_deviation_reaches_order_send(ctx):
    req = V2OpenOrderRequest(symbol="EURUSD", side="buy", volume=0.1, deviation=5, comment="NSE_TEST")
    out = execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    assert ctx.driver._mt5.sent_requests[0]["deviation"] == 5
    assert ctx.driver._mt5.sent_requests[0]["comment"] == "NSE_TEST"


def test_v2_pending_expiry_becomes_specified_time(ctx):
    req = V2OpenOrderRequest(
        symbol="EURUSD", side="sell", volume=0.1, order_kind="limit", price=1.1050, expiry_secs=300
    )
    out = execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert out["status"] == "done"
    sent = ctx.driver._mt5.sent_requests[0]
    assert sent["type_time"] == 1  # ORDER_TIME_SPECIFIED
    assert "expiration" in sent


def test_v2_market_preflight_rejects_volume_below_min(ctx):
    """MT5 pre-flight: a volume below the broker minimum fails
    before order_check / order_send are ever reached."""
    req = V2OpenOrderRequest(symbol="EURUSD", side="buy", volume=0.001)
    with pytest.raises(BridgeError) as excinfo:
        execute_v2_open_order(ctx, req, flag_enabled=True, negotiation=NEGOTIATED)
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert "volume_below_min" in str(excinfo.value.details.get("reasons"))
    assert ctx.driver._mt5.send_calls == 0
