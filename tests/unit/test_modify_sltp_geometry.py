"""Unit tests for the modify_sl_tp geometry pre-flight (audit P2-4).

Label: unit. No MT5 — a fake MT5 module is injected into the driver.

The old path checked the requested SL/TP only against each other (and
the entry price, for opens) — never against the LIVE price — so a
"safe" modify could push the SL to the wrong side of the current
price (or the TP below the current price on a long) and MT5 refused
it late with retcode 10030. ``modify_position_sltp`` now pre-flights
SL/TP geometry against the live tick for the position's direction
(SL below / TP above the bid on a long, the mirror on a short)
before ``order_check`` and before any send.

"Rejected before MT5" here means: no ``order_check`` and no
``order_send`` — the position fetch + tick read the check itself
needs are the only driver calls. A modify the broker could never
accept must never reach the send.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.engine.trading import modify_position_sltp
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.commands import CommandExecutor, CommandValidator
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

DEVICE = "dev-1"
ACCOUNT = "12345678901"
LONG_TICKET = 9000001
SHORT_TICKET = 9000002


class _FakeMt5:
    """Long + short EURUSD positions; tick bid 1.1000 / ask 1.1002."""

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

    def __init__(self, *, no_tick: bool = False) -> None:
        self.positions = {
            LONG_TICKET: SimpleNamespace(
                ticket=LONG_TICKET,
                symbol="EURUSD",
                type=0,
                volume=1.0,
                price_open=1.1000,
                price_current=1.1002,
                sl=1.0900,
                tp=1.1100,
                profit=0.0,
                swap=0.0,
                commission=0.0,
                magic=0,
                comment="",
                time=0,
            ),
            SHORT_TICKET: SimpleNamespace(
                ticket=SHORT_TICKET,
                symbol="EURUSD",
                type=1,
                volume=1.0,
                price_open=1.1000,
                price_current=1.1000,
                sl=1.1100,
                tp=1.0900,
                profit=0.0,
                swap=0.0,
                commission=0.0,
                magic=0,
                comment="",
                time=0,
            ),
        }
        self._tick = (
            None
            if no_tick
            else SimpleNamespace(time=int(time.time()), bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)
        )
        self.check_calls = 0
        self.send_calls = 0

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            pos = self.positions.get(int(ticket))
            return [pos] if pos is not None else []
        return list(self.positions.values())

    def orders_get(self, ticket=None, symbol=None):
        return []

    def symbol_info(self, name):
        return SimpleNamespace(
            name=name,
            trade_tick_size=0.00001,
            point=0.00001,
            digits=5,
            filling_mode=3,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

    def symbol_info_tick(self, symbol):
        return self._tick

    def symbol_select(self, name, enable=True):
        return True

    def account_info(self):
        return SimpleNamespace(login=123456, trade_mode=0, margin_mode=1)

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    def order_check(self, request):
        self.check_calls += 1
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
        self.send_calls += 1
        if request.get("action") == self.TRADE_ACTION_SLTP:
            pos = self.positions[request["position"]]
            pos.sl = float(request["sl"])
            pos.tp = float(request["tp"])
        return SimpleNamespace(retcode=10009, order=0, deal=8000001, price=0.0, volume=0.0, comment="")


def _ctx(fake: _FakeMt5, tmp_path: Path) -> BridgeContext:
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    driver._mt5 = fake
    driver._connected = True
    return BridgeContext(settings=settings, driver=driver)


def _frame(*, ticket: int, sl: float | None, tp: float | None) -> dict:
    now = datetime.now(UTC)
    operation: dict = {"type": "modify_sl_tp", "ticket": ticket}
    if sl is not None:
        operation["sl"] = sl
    if tp is not None:
        operation["tp"] = tp
    return {
        "type": "command",
        "command_id": new_uuid7_str(),
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": operation,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": new_uuid7_str(),
    }


def _validator() -> CommandValidator:
    return CommandValidator(
        DEVICE,
        get_account_id=lambda: ACCOUNT,
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: datetime.now(UTC),
    )


def _execute(ctx: BridgeContext, *, ticket: int, sl: float | None, tp: float | None):
    executor = CommandExecutor(ctx, _validator(), results_queue=None)
    return executor.execute(_frame(ticket=ticket, sl=sl, tp=tp))


def _assert_rejected_before_send(fake: _FakeMt5, result: dict, reason: str) -> None:
    assert result["status"] == "mcp_failed", result
    assert result["error"]["code"] == ErrorCode.VALIDATION.value, result
    assert reason in result["error"]["cause"], result
    # The geometry pre-flight fires before order_check and before any
    # send — the broker never sees the request (no 10030 possible).
    assert fake.check_calls == 0, "order_check must not run for a wrong-side modify"
    assert fake.send_calls == 0, "order_send must not run for a wrong-side modify"


@pytest.mark.unit
def test_long_wrong_side_sl_rejected_before_send(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=1.1200, tp=1.1300)
    _assert_rejected_before_send(fake, result, "sl_not_below_price")
    assert fake.positions[LONG_TICKET].sl == 1.0900  # live position untouched


@pytest.mark.unit
def test_long_wrong_side_tp_rejected_before_send(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=1.0900, tp=1.0900)
    _assert_rejected_before_send(fake, result, "tp_not_above_price")
    assert fake.positions[LONG_TICKET].tp == 1.1100


@pytest.mark.unit
def test_short_wrong_side_sl_rejected_before_send(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=SHORT_TICKET, sl=1.0900, tp=1.0800)
    _assert_rejected_before_send(fake, result, "sl_not_above_price")
    assert fake.positions[SHORT_TICKET].sl == 1.1100


@pytest.mark.unit
def test_short_wrong_side_tp_rejected_before_send(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=SHORT_TICKET, sl=1.1100, tp=1.1100)
    _assert_rejected_before_send(fake, result, "tp_not_below_price")
    assert fake.positions[SHORT_TICKET].tp == 1.0900


@pytest.mark.unit
def test_short_sl_at_entry_rejected_once_price_moved_against_it(tmp_path: Path):
    # The audit's SELL case: SL at the entry price (1.1000) looks
    # "safe" against the entry, but the ask has risen to 1.1002 — the
    # SL is on the wrong side of the current price and MT5 would
    # refuse it with 10030. The old entry-only view accepted it.
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=SHORT_TICKET, sl=1.1000, tp=1.0900)
    _assert_rejected_before_send(fake, result, "sl_not_above_price")


@pytest.mark.unit
def test_long_sl_inside_stops_level_rejected(tmp_path: Path):
    # Right side, wrong distance: 1.09995 is below the bid but inside
    # the 10-point safety floor (min distance 0.0001) — the broker
    # would refuse it, so the pre-flight must too.
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=1.09995, tp=1.1100)
    _assert_rejected_before_send(fake, result, "sl_not_below_price")


@pytest.mark.unit
def test_negative_sl_rejected_before_send(tmp_path: Path):
    # Negative SL/TP pass parse_envelope unchecked — the geometry
    # check is the first place that can refuse them.
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=-1.0, tp=1.1100)
    _assert_rejected_before_send(fake, result, "sl_negative")


@pytest.mark.unit
def test_valid_long_modify_proceeds_to_mt5(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=1.0950, tp=1.1150)
    assert result["status"] == "executed", result
    assert fake.check_calls == 1
    assert fake.send_calls == 1
    assert fake.positions[LONG_TICKET].sl == 1.0950
    assert fake.positions[LONG_TICKET].tp == 1.1150


@pytest.mark.unit
def test_valid_short_modify_proceeds_to_mt5(tmp_path: Path):
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=SHORT_TICKET, sl=1.1050, tp=1.0950)
    assert result["status"] == "executed", result
    assert fake.check_calls == 1
    assert fake.send_calls == 1
    assert fake.positions[SHORT_TICKET].sl == 1.1050
    assert fake.positions[SHORT_TICKET].tp == 1.0950


@pytest.mark.unit
def test_valid_partial_modify_keeps_other_leg(tmp_path: Path):
    # SL-only modify: the TP is filled from the live position (1.1100)
    # and the combined state still passes geometry.
    fake = _FakeMt5()
    ctx = _ctx(fake, tmp_path)
    _, result = _execute(ctx, ticket=LONG_TICKET, sl=1.0950, tp=None)
    assert result["status"] == "executed", result
    assert fake.send_calls == 1
    assert fake.positions[LONG_TICKET].sl == 1.0950
    assert fake.positions[LONG_TICKET].tp == 1.1100


@pytest.mark.unit
def test_no_tick_fails_closed_without_send(tmp_path: Path):
    # Fail-closed: when the live price cannot be read, the modify is
    # refused rather than sent unchecked — never a blind send.
    fake = _FakeMt5(no_tick=True)
    ctx = _ctx(fake, tmp_path)
    with pytest.raises(BridgeError) as excinfo:
        modify_position_sltp(ctx, LONG_TICKET, 1.0950, 1.1150)
    assert excinfo.value.code is ErrorCode.MT5_CALL_FAILED
    assert fake.send_calls == 0
    assert fake.check_calls == 0
