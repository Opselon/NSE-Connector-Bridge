"""Unit tests for the C2 execution work (spec §§6–7, gap §5 C2).

Label: unit. No MT5 — a fake MT5 module is injected into the driver
(``driver._mt5 = fake``; ``driver._connected = True``). Covers:
stop-limit pending types, close-by, partial-close reconciliation,
PARTIALLY_COMPLETED lifecycle, modify-pending volume removal, the
exmode/calc-mode + margin-mode model fixes, §6 retcode coverage, and
canonical-symbol resolution in the execution path.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext, resolve_order_symbol
from nse_bridge.engine.execution import (
    PENDING_TYPES,
    PendingOrderRequest,
    trade_send_pending_order,
    validate_pending,
)
from nse_bridge.engine.orders import ModifyPendingRequest, trade_modify_pending_order
from nse_bridge.engine.trading import close_position, close_position_by, market_order, modify_position_sltp
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.mt5.types import build_account_model, build_symbol_model
from nse_bridge.retcodes import RETCODE_MAP, translate_retcode
from nse_bridge.server.commands import CommandExecution, CommandState

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------
class _FakeMt5:
    """Minimal MT5 module: constants + a mutable broker state."""

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

    def __init__(
        self,
        *,
        positions=None,
        symbol_rows=None,
        ticks=None,
        account=None,
        send_retcode=10009,
        send_partial_volume=None,
    ):
        self.positions = {p.ticket: p for p in (positions or [])}
        self.orders: dict[int, SimpleNamespace] = {}
        self.symbol_rows = {r.name: r for r in (symbol_rows or [])}
        self.ticks = ticks or {}
        self.account = account
        self.send_retcode = send_retcode
        # When set, a DEAL close reduces the position by this amount
        # instead of the requested volume (simulates a partial fill).
        self.send_partial_volume = send_partial_volume
        self.sent_requests: list[dict] = []
        self.send_calls = 0
        self._next_order_ticket = 7000001

    # -- broker state -------------------------------------------------
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
        rows = list(self.orders.values())
        if symbol:
            rows = [o for o in rows if o.symbol == symbol]
        return rows

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def symbol_info(self, name):
        return self.symbol_rows.get(name)

    def symbols_get(self):
        return list(self.symbol_rows.values())

    def symbol_select(self, name, enable=True):
        return True

    def account_info(self):
        return self.account

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    # -- dispatch -----------------------------------------------------
    def order_send(self, request):
        self.sent_requests.append(dict(request))
        self.send_calls += 1
        action = request.get("action")
        if action == self.TRADE_ACTION_DEAL and "position" in request:
            # close (full or partial): apply to the stored position
            pos = self.positions.get(int(request["position"]))
            if pos is not None and self.send_retcode in (10009, 10010):
                filled = self.send_partial_volume if self.send_partial_volume is not None else float(request["volume"])
                remaining = round(float(pos.volume) - filled, 8)
                if remaining <= 1e-9:
                    del self.positions[int(request["position"])]
                else:
                    pos.volume = remaining
        elif action == self.TRADE_ACTION_PENDING:
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
                retcode=self.send_retcode,
                order=ticket,
                deal=0,
                price=request.get("price"),
                volume=request.get("volume"),
                comment="",
            )
        elif action == self.TRADE_ACTION_CLOSE_BY:
            a = self.positions.get(int(request["position"]))
            b = self.positions.get(int(request["position_by"]))
            if a is not None and b is not None and self.send_retcode == 10009:
                va, vb = float(a.volume), float(b.volume)
                if va <= vb:
                    del self.positions[int(request["position"])]
                    if vb > va:
                        b.volume = round(vb - va, 8)
                    else:
                        del self.positions[int(request["position_by"])]
                else:
                    del self.positions[int(request["position_by"])]
                    a.volume = round(va - vb, 8)
        return SimpleNamespace(
            retcode=self.send_retcode,
            order=request.get("order", 0),
            deal=8000001,
            price=request.get("price", 0.0),
            volume=request.get("volume", 0.0),
            comment="",
        )


def _symbol_row(name, **overrides):
    fields = {
        "name": name,
        "description": f"{name} desc",
        "path": f"Test\\{name}",
        "visible": True,
        "selected": True,
        "trade_mode": 4,
        "digits": 5,
        "point": 0.00001,
        "spread": 20,
        "spread_float": 2.0,
        "trade_tick_size": 0.00001,
        "trade_tick_value": 1.0,
        "trade_tick_value_loss": 1.0,
        "trade_contract_size": 100000.0,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_stops_level": 10,
        "trade_freeze_level": 0,
        "trade_exemode": 2,
        "trade_calc_mode": 0,
        "filling_mode": 3,
        "swap_mode": 1,
        "swap_long": 0.5,
        "swap_short": -0.7,
        "swap_rollover3days": 3,
        "currency_base": None,
        "currency_profit": None,
        "currency_margin": None,
        "margin_initial": 1000.0,
        "margin_maintenance": 500.0,
        "margin_hedged": 250.0,
        "order_gtc_mode": 0,
        "start_time": 0,
        "expiration_time": 0,
        "session_deals": 12,
        "session_turnover": 345.0,
        "session_open": 1.10,
        "session_close": 1.11,
        "session_volume": 7.5,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _position(ticket, *, symbol="EURUSD", type_=0, volume=1.0, magic=0):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type_,
        volume=volume,
        price_open=1.1000,
        price_current=1.1002,
        sl=0.0,
        tp=0.0,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=magic,
        comment="",
        time=int(time.time()),
    )


def _tick(bid=1.1000, ask=1.1002):
    return SimpleNamespace(time=int(time.time()), bid=bid, ask=ask, last=1.1001, volume=10, flags=0)


def _account(margin_mode=1):
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
        margin_mode=margin_mode,
        trade_allowed=True,
        trade_expert=True,
    )


@pytest.fixture
def ctx(tmp_path):
    """BridgeContext with a fake MT5; the fake is reachable as ctx.driver._mt5."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _FakeMt5(
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
    )
    driver._mt5 = fake
    driver._connected = True
    context = BridgeContext(settings=settings, driver=driver)
    try:
        yield context
    finally:
        driver.disconnect()


def _ctx_with(fake, tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    driver._mt5 = fake
    driver._connected = True
    context = BridgeContext(settings=settings, driver=driver)
    return context, driver


# ----------------------------------------------------------------------
# stop-limit pending types (spec §6, task 1)
# ----------------------------------------------------------------------
def test_pending_types_include_stop_limit():
    assert PENDING_TYPES == {
        "BUY_LIMIT": 2,
        "SELL_LIMIT": 3,
        "BUY_STOP": 4,
        "SELL_STOP": 5,
        "BUY_STOP_LIMIT": 6,
        "SELL_STOP_LIMIT": 7,
    }


def test_validate_pending_buy_stop_limit_ok(ctx):
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1010, 1.0990, 1.1020, 0.1, stoplimit=1.1005)
    assert reasons == []


def test_validate_pending_stop_limit_requires_stoplimit(ctx):
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1010, 0.0, 0.0, 0.1)
    assert reasons == ["stoplimit_required"]
    reasons = validate_pending(ctx, "EURUSD", "SELL_STOP_LIMIT", 1.0990, 0.0, 0.0, 0.1)
    assert reasons == ["stoplimit_required"]


def test_validate_pending_stop_limit_wrong_side(ctx):
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1010, 0.0, 0.0, 0.1, stoplimit=1.1010)
    assert "stoplimit_not_below_trigger" in reasons
    reasons = validate_pending(ctx, "EURUSD", "SELL_STOP_LIMIT", 1.0990, 0.0, 0.0, 0.1, stoplimit=1.0990)
    assert "stoplimit_not_above_trigger" in reasons


def test_validate_pending_stop_limit_trigger_geometry(ctx):
    # BUY_STOP_LIMIT trigger must sit above the ask like a BUY_STOP.
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1000, 0.0, 0.0, 0.1, stoplimit=1.0995)
    assert "buy_stop_price_not_above_ask" in reasons
    reasons = validate_pending(ctx, "EURUSD", "SELL_STOP_LIMIT", 1.1004, 0.0, 0.0, 0.1, stoplimit=1.1009)
    assert "sell_stop_price_not_below_bid" in reasons


def test_validate_pending_sl_tp_checked_against_stoplimit(ctx):
    # SL must sit below the ENTRY (stoplimit 1.1005), not the trigger (1.1010).
    # min_stop_dist = 0.0001, so sl must be >= 1.1004 + 1e-9 to trip.
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1010, 1.10045, 0.0, 0.1, stoplimit=1.1005)
    assert "sl_not_below_entry" in reasons
    reasons = validate_pending(ctx, "EURUSD", "BUY_STOP_LIMIT", 1.1010, 1.0990, 0.0, 0.1, stoplimit=1.1005)
    assert reasons == []


def test_pending_dispatch_sends_stoplimit(ctx):
    payload = trade_send_pending_order(
        ctx,
        PendingOrderRequest(
            symbol="EURUSD",
            order_type="BUY_STOP_LIMIT",
            volume=0.1,
            price=1.1010,
            stoplimit=1.1005,
            sl=1.0990,
            tp=1.1020,
        ),
    )
    fake = ctx.driver._mt5
    assert len(fake.sent_requests) == 1
    req = fake.sent_requests[0]
    assert req["type"] == 6
    assert req["stoplimit"] == 1.1005
    assert req["price"] == 1.1010
    assert payload["status"] == "done"
    assert payload["order_ticket"] == 7000001


def test_pending_dispatch_non_stop_limit_has_no_stoplimit(ctx):
    trade_send_pending_order(
        ctx,
        PendingOrderRequest(symbol="EURUSD", order_type="BUY_LIMIT", volume=0.1, price=1.0990),
    )
    req = ctx.driver._mt5.sent_requests[0]
    assert req["type"] == 2
    assert "stoplimit" not in req


def test_pending_dispatch_stop_limit_without_stoplimit_is_validation_error(ctx):
    with pytest.raises(BridgeError) as excinfo:
        trade_send_pending_order(
            ctx,
            PendingOrderRequest(symbol="EURUSD", order_type="SELL_STOP_LIMIT", volume=0.1, price=1.0990),
        )
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert ctx.driver._mt5.send_calls == 0  # rejected BEFORE dispatch


# ----------------------------------------------------------------------
# close-by (spec §6, task 2)
# ----------------------------------------------------------------------
def test_close_by_equal_volumes_both_gone(tmp_path):
    fake = _FakeMt5(
        positions=[_position(1001, type_=0, volume=0.5), _position(1002, type_=1, volume=0.5)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=1),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["closed_volume"] == 0.5
    assert out["remaining_volume"] == 0.0
    assert out["remaining_ticket"] is None
    assert fake.positions == {}
    req = fake.sent_requests[0]
    assert req["action"] == fake.TRADE_ACTION_CLOSE_BY
    assert req["position"] == 1001 and req["position_by"] == 1002


def test_close_by_unequal_volumes_larger_remains(tmp_path):
    fake = _FakeMt5(
        positions=[_position(1001, type_=0, volume=1.0), _position(1002, type_=1, volume=0.4)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=1),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert out["closed_volume"] == 0.4
    assert out["remaining_volume"] == 0.6
    assert out["remaining_ticket"] == 1001
    assert set(fake.positions) == {1001}
    assert fake.positions[1001].volume == pytest.approx(0.6)


def test_close_by_netting_account_refused(tmp_path):
    fake = _FakeMt5(
        positions=[_position(1001, type_=0), _position(1002, type_=1)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=0),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert "hedging" in excinfo.value.message
    assert fake.send_calls == 0


def test_close_by_unknown_margin_mode_refused(tmp_path):
    account = _account(margin_mode=1)
    delattr(account, "margin_mode")
    fake = _FakeMt5(
        positions=[_position(1001, type_=0), _position(1002, type_=1)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=account,
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert fake.send_calls == 0


def test_close_by_requires_same_symbol_and_opposite_types(tmp_path):
    fake = _FakeMt5(
        positions=[
            _position(1001, symbol="EURUSD", type_=0),
            _position(1002, symbol="GBPUSD", type_=1),
            _position(1003, symbol="EURUSD", type_=0),
        ],
        symbol_rows=[_symbol_row("EURUSD"), _symbol_row("GBPUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=1),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1002)
        assert excinfo.value.code is ErrorCode.VALIDATION
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1003)
        assert excinfo.value.code is ErrorCode.VALIDATION
    finally:
        driver.disconnect()
    assert fake.send_calls == 0


def test_close_by_broker_reject_preserves_retcode(tmp_path):
    fake = _FakeMt5(
        positions=[_position(1001, type_=0), _position(1002, type_=1)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=1),
        send_retcode=10006,
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.TRADE_REJECTED
    assert excinfo.value.retcode == 10006


def test_close_by_done_but_position_remains_is_unverified(tmp_path):
    fake = _FakeMt5(
        positions=[_position(1001, type_=0, volume=0.5), _position(1002, type_=1, volume=0.5)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(margin_mode=1),
    )

    # Broker claims DONE but the fake never applies the close: the
    # verification must catch it instead of reporting success.
    def no_apply(request):
        fake.sent_requests.append(dict(request))
        fake.send_calls += 1
        return SimpleNamespace(retcode=10009, order=0, deal=0, price=0.0, volume=0.0, comment="")

    fake.order_send = no_apply
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position_by(ctx, 1001, 1002)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED


# ----------------------------------------------------------------------
# partial close + DONE_PARTIAL (spec §7, task 3)
# ----------------------------------------------------------------------
def test_partial_close_reports_reconciled_partial(tmp_path):
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position(ctx, 2001, 0.4)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["partial"] is True
    assert out["closed_volume"] == pytest.approx(0.4)
    assert out["remaining_volume"] == pytest.approx(0.6)
    assert out["reconciled"] is True


def test_partial_close_broker_closed_everything(tmp_path):
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
    )

    # Broker closes the whole position even though 0.4 was requested.
    def close_all(request):
        fake.sent_requests.append(dict(request))
        fake.send_calls += 1
        fake.positions.pop(int(request["position"]), None)
        return SimpleNamespace(retcode=10009, order=0, deal=0, price=0.0, volume=0.0, comment="")

    fake.order_send = close_all
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position(ctx, 2001, 0.4)
    finally:
        driver.disconnect()
    assert out["partial"] is False
    assert out["closed_volume"] == pytest.approx(1.0)
    assert out["remaining_volume"] == 0.0


def test_close_done_partial_retcode_reconciles_without_retry(tmp_path):
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
        send_retcode=10010,
        send_partial_volume=0.7,  # broker filled 0.7 of the requested 1.0
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position(ctx, 2001)
    finally:
        driver.disconnect()
    # Full close requested, broker partially filled 0.7 of 1.0.
    assert out["partial"] is True
    assert out["closed_volume"] == pytest.approx(0.7)
    assert out["remaining_volume"] == pytest.approx(0.3)
    assert fake.send_calls == 1  # DONE_PARTIAL is never retried


def test_close_done_partial_unchanged_volume_is_unverified(tmp_path):
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
        send_retcode=10010,
        send_partial_volume=0.0,  # broker reports partial but nothing moved
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            close_position(ctx, 2001)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.TRADE_UNVERIFIED


def test_full_close_payload_unchanged(tmp_path):
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = close_position(ctx, 2001)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["closed_volume"] == pytest.approx(1.0)
    assert "partial" not in out  # full closes keep the old payload shape


# ----------------------------------------------------------------------
# modify pending: no volume in the request (gap §6 Q10, task 4)
# ----------------------------------------------------------------------
def test_modify_pending_request_sends_no_volume(ctx):
    ctx.driver._mt5.orders[6001] = SimpleNamespace(
        ticket=6001,
        symbol="EURUSD",
        type=4,
        price_open=1.1010,
        price_current=1.1010,
        volume_current=0.2,
        volume_initial=0.2,
        sl=1.0990,
        tp=1.1020,
        time_setup=int(time.time()),
        type_time=0,
        magic=0,
        comment="",
    )
    out = trade_modify_pending_order(ctx, ModifyPendingRequest(ticket=6001, price=1.1015, sl=1.0995))
    fake = ctx.driver._mt5
    assert out["status"] == "done"
    assert len(fake.sent_requests) == 1
    req = fake.sent_requests[0]
    assert req["action"] == fake.TRADE_ACTION_MODIFY
    assert "volume" not in req
    assert req["price"] == 1.1015
    assert "volume" not in ModifyPendingRequest.model_fields


# ----------------------------------------------------------------------
# model fixes (task 5)
# ----------------------------------------------------------------------
def test_build_symbol_model_splits_exmode_and_calc_mode():
    row = _symbol_row("EURUSD", trade_exemode=2, trade_calc_mode=3)
    model = build_symbol_model(row, 0)
    assert model.trade_execution == 2
    assert model.trade_execution_label == "MARKET"
    assert model.trade_calc_mode == 3
    assert model.trade_calc_mode_label == "CFD"


def test_build_account_model_margin_mode():
    row = _account(margin_mode=1)
    row.margin_so_mode = 0
    model = build_account_model(row)
    assert model.margin_mode == 1
    assert model.margin_mode_label == "RETAIL_HEDGING"
    assert model.margin_so_mode == 0  # stop-out mode stays separate


def test_build_account_model_trade_mode_uses_account_enum_not_symbol_enum():
    # Real-MT5 defect 2 (2026-11-18): TRADE_MODE_LABELS is the
    # ENUM_SYMBOL_TRADE_MODE labelling (0=DISABLED) but was also used
    # for accounts — the MetaQuotes-Demo account (trade_mode=0) was
    # labelled DISABLED. ENUM_ACCOUNT_TRADE_MODE says 0=DEMO.
    demo = build_account_model(_account())
    assert demo.trade_mode == 0
    assert demo.trade_mode_label == "DEMO"
    contest_raw = _account()
    contest_raw.trade_mode = 1
    assert build_account_model(contest_raw).trade_mode_label == "CONTEST"
    real_raw = _account()
    real_raw.trade_mode = 2
    assert build_account_model(real_raw).trade_mode_label == "REAL"
    # The symbol side is a DIFFERENT enum and must not move: a symbol
    # with trade_mode=0 is still DISABLED (metadata.py, inventory.py).
    symbol = build_symbol_model(_symbol_row("EURUSD", trade_mode=0), 0)
    assert symbol.trade_mode == 0
    assert symbol.trade_mode_label == "DISABLED"


def test_trading_permissions_returns_real_margin_mode(ctx):
    from nse_bridge.engine.account import get_trading_permissions

    out = get_trading_permissions(ctx, None)
    assert out["margin_mode"] == 1
    assert out["margin_mode_label"] == "RETAIL_HEDGING"


# ----------------------------------------------------------------------
# retcode coverage for spec §6 (task 6)
# ----------------------------------------------------------------------
def test_retcode_map_covers_spec_section_6_conditions():
    # spec §6 broker-specific execution handling → MT5 retcode.
    expected = {
        "invalid volume": 10014,
        "invalid price": 10015,
        "invalid stops": 10016,
        "insufficient margin": 10019,
        "market closed": 10018,
        "trading disabled": 10017,
        "symbol unavailable": 10025,
        "unsupported filling mode": 10030,
        "price changed": 10020,
        "requote": 10004,
        "partial execution": 10010,
        "execution timeout": 10012,
        "connection interruption": 10031,
        "broker rejection": 10006,
    }
    for condition, code in expected.items():
        assert code in RETCODE_MAP, condition
        assert translate_retcode(code) != f"UNKNOWN_MT5_RETCODE ({code})"
    assert translate_retcode(99999).startswith("UNKNOWN_MT5_RETCODE")


# ----------------------------------------------------------------------
# NO_CHANGES idempotency (real-MT5 defect 1, 2026-11-18)
# ----------------------------------------------------------------------
def test_modify_position_sltp_no_changes_is_idempotent_success(tmp_path):
    """A combined SL/TP modify with duplicate values returns NO_CHANGES
    (10025) — the desired state already holds. The old code read it as
    UNKNOWN_SYMBOL, retried 3 times, and failed even though the broker
    state was correct. Now: one dispatch, success, no retry."""
    fake = _FakeMt5(
        positions=[_position(2001, type_=0, volume=1.0)],
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
        send_retcode=10025,
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = modify_position_sltp(ctx, 2001, sl=1.0900, tp=1.1100)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["retcode"] == 10025
    assert out["retcode_label"].startswith("NO_CHANGES")
    assert out["no_changes"] is True
    assert fake.send_calls == 1  # no retry on an already-satisfied state


def test_modify_pending_order_no_changes_is_idempotent_success(ctx):
    """Same defect class for pending orders: a duplicate SL/TP modify
    returns NO_CHANGES and must succeed, not raise TRADE_REJECTED."""
    ctx.driver._mt5.orders[6001] = SimpleNamespace(
        ticket=6001,
        symbol="EURUSD",
        type=4,
        price_open=1.1010,
        price_current=1.1010,
        volume_current=0.2,
        volume_initial=0.2,
        sl=1.0990,
        tp=1.1020,
        time_setup=int(time.time()),
        type_time=0,
        magic=0,
        comment="",
    )
    ctx.driver._mt5.send_retcode = 10025
    out = trade_modify_pending_order(ctx, ModifyPendingRequest(ticket=6001, sl=1.0990, tp=1.1020))
    assert out["status"] == "done"
    assert out["retcode"] == 10025
    assert out["retcode_label"].startswith("NO_CHANGES")
    assert out["no_changes"] is True


# ----------------------------------------------------------------------
# canonical-symbol resolution in the execution path (task 7)
# ----------------------------------------------------------------------
def _xau_rows():
    return [
        _symbol_row(
            "XAUUSD.a",
            currency_base="XAU",
            currency_profit="USD",
            trade_contract_size=100.0,
            digits=2,
            point=0.01,
        ),
    ]


def test_market_order_resolves_canonical_symbol(tmp_path):
    fake = _FakeMt5(
        symbol_rows=_xau_rows(),
        ticks={"XAUUSD.a": _tick(bid=2640.0, ask=2640.5)},
        account=_account(),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = market_order(ctx, symbol="XAU/USD", side="buy", volume=0.1)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["symbol"] == "XAUUSD.a"
    assert fake.sent_requests[0]["symbol"] == "XAUUSD.a"


def test_market_order_ambiguous_canonical_is_rejected(tmp_path):
    rows = _xau_rows() + [
        _symbol_row(
            "XAUUSDm",
            currency_base="XAU",
            currency_profit="USD",
            trade_contract_size=100.0,
            digits=2,
            point=0.01,
        ),
    ]
    fake = _FakeMt5(
        symbol_rows=rows,
        ticks={"XAUUSD.a": _tick(bid=2640.0, ask=2640.5)},
        account=_account(),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            market_order(ctx, symbol="XAU/USD", side="buy", volume=0.1)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert excinfo.value.details["status"] == "ambiguous"
    assert fake.send_calls == 0  # never guessed, never dispatched


def test_market_order_unknown_canonical_is_rejected(tmp_path):
    fake = _FakeMt5(symbol_rows=[], ticks={}, account=_account())
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        with pytest.raises(BridgeError) as excinfo:
            market_order(ctx, symbol="XAU/USD", side="buy", volume=0.1)
    finally:
        driver.disconnect()
    assert excinfo.value.code is ErrorCode.VALIDATION
    assert excinfo.value.details["status"] == "not_found"
    assert fake.send_calls == 0


def test_market_order_exact_symbol_never_touches_resolver(tmp_path):
    fake = _FakeMt5(
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        account=_account(),
    )

    def exploding_symbols_get():
        raise AssertionError("resolver must not run for exact broker names")

    fake.symbols_get = exploding_symbols_get
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        out = market_order(ctx, symbol="EURUSD", side="buy", volume=0.1)
    finally:
        driver.disconnect()
    assert out["status"] == "done"
    assert out["symbol"] == "EURUSD"


def test_pending_order_resolves_canonical_symbol(tmp_path):
    fake = _FakeMt5(
        symbol_rows=_xau_rows(),
        ticks={"XAUUSD.a": _tick(bid=2640.0, ask=2640.5)},
        account=_account(),
    )
    ctx, driver = _ctx_with(fake, tmp_path)
    try:
        payload = trade_send_pending_order(
            ctx,
            PendingOrderRequest(symbol="XAU/USD", order_type="BUY_LIMIT", volume=0.1, price=2630.0),
        )
    finally:
        driver.disconnect()
    assert payload["status"] == "done"
    assert payload["symbol"] == "XAUUSD.a"
    assert fake.sent_requests[0]["symbol"] == "XAUUSD.a"


def test_resolve_order_symbol_passthrough_and_cache(ctx):
    assert resolve_order_symbol(ctx, "EURUSD") == "EURUSD"
    assert ctx.symbol_resolver is None  # exact names never build a resolver


# ----------------------------------------------------------------------
# PARTIALLY_COMPLETED lifecycle (spec §7, task 3)
# ----------------------------------------------------------------------
def test_partially_completed_transition_rules():
    execution = CommandExecution(command_id="cmd-1")
    execution.transition(CommandState.VALIDATED)
    execution.transition(CommandState.AUTHORIZED)
    execution.transition(CommandState.DISPATCHED)
    execution.transition(CommandState.EXECUTING)
    execution.transition(CommandState.PARTIALLY_COMPLETED)
    assert execution.state is CommandState.PARTIALLY_COMPLETED
    # Terminal: no outgoing edges.
    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        execution.transition(CommandState.COMPLETED)


def test_executor_partial_close_reports_partially_completed(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    from nse_bridge.server.commands import CommandExecutor, CommandValidator
    from nse_bridge.server.queue import BoundedPendingQueue
    from nse_bridge.server.rest import Entitlements
    from nse_bridge.server.uuid7 import new_uuid7_str

    # Same frozen-clock pattern as tests/unit/test_server_commands.py:
    # the validator clock freezes at NOW so the frame is not expired.
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    context = BridgeContext(settings=settings, driver=Mt5Driver(settings))

    def partial_close(ctx_, ticket):
        return {
            "status": "done",
            "ticket": ticket,
            "closed_volume": 0.4,
            "remaining_volume": 0.6,
            "partial": True,
            "reconciled": True,
        }

    monkeypatch.setattr("nse_bridge.engine.trading.close_position", partial_close)
    validator = CommandValidator(
        "dev-1",
        get_account_id=lambda: "12345678901",
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: now,
    )
    executor = CommandExecutor(context, validator, results_queue=BoundedPendingQueue(32, "test/results"))
    issued = now - timedelta(minutes=1)
    frame = {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": "12345678901",
        "device_id": "dev-1",
        "operation": {"type": "close_position", "ticket": "9000001"},
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": "same-as-command-id",
    }
    execution, result = executor.execute(frame)
    assert execution.state is CommandState.PARTIALLY_COMPLETED
    # Wire status stays "executed" (§6.5 has no partial status — D5);
    # the partial facts ride in the payload.
    assert result["status"] == "executed"
    assert result["payload"]["partial"] is True
    assert result["payload"]["remaining_volume"] == 0.6
