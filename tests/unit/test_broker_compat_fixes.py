"""Unit tests for the broker-compatibility fixes (audit R1–R6, R10, A1–A4).

Label: unit. No MT5 — a fake MT5 module is injected into the driver
(``driver._mt5 = fake``; ``driver._connected = True``). Each test names
the audit finding it pins, and encodes the audit's repro case: the
defect as found (wrong value / crash / silent mislabel) must not recur.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext, resolve_filling_mode
from nse_bridge.engine.execution import validate_pending
from nse_bridge.engine.symbols import SymbolsListRequest, get_trading_symbols
from nse_bridge.engine.trading import close_position, market_order
from nse_bridge.errors import BridgeError, ErrorCode
from nse_bridge.mt5.driver import (
    MT5_PLATFORM_SUPPORTED,
    MT5_WINDOWS_REQUIRED_CODE,
    Mt5Driver,
)
from nse_bridge.server.commands import CommandEnvelope, CommandExecutor, CommandValidator
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.sync import SyncCheckpoint, SyncEngine, parse_broker_time

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

    def __init__(self, *, positions=None, symbol_rows=None, ticks=None):
        self.positions = {p.ticket: p for p in (positions or [])}
        self.symbol_rows = {r.name: r for r in (symbol_rows or [])}
        self.ticks = ticks or {}
        self.sent_requests: list[dict] = []
        self.send_calls = 0
        # A4: initialize() outcome + last_error() value, set by the test.
        self.initialize_result = True
        self.last_error_result: tuple | None = (0, "")

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(int(ticket))
            return [p] if p is not None else []
        rows = list(self.positions.values())
        if symbol:
            rows = [p for p in rows if p.symbol == symbol]
        return rows

    def orders_get(self, ticket=None, symbol=None):
        return []

    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def symbol_info(self, name):
        return self.symbol_rows.get(name)

    def symbols_get(self):
        return list(self.symbol_rows.values())

    def symbol_select(self, name, enable=True):
        return True

    def account_info(self):
        return None

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return self.last_error_result

    def initialize(self, **kwargs):
        return self.initialize_result

    def shutdown(self):
        pass

    def order_check(self, request):
        """Official pre-send check (W2): always passes in this fake."""
        self.check_calls = getattr(self, "check_calls", 0) + 1
        self.check_requests = getattr(self, "check_requests", [])
        self.check_requests.append(dict(request))
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
        return SimpleNamespace(
            retcode=10009,
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
        "visible": True,
        "trade_mode": 4,
        "digits": 5,
        "point": 0.00001,
        "trade_tick_size": 0.00001,
        "trade_stops_level": 10,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_exemode": 2,
        "trade_calc_mode": 0,
        "filling_mode": 3,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _tick(bid=1.1000, ask=1.1002):
    return SimpleNamespace(time=int(time.time()), bid=bid, ask=ask, last=1.1001, volume=10, flags=0)


def _position(ticket, *, symbol="EURUSD", type_=0, volume=1.0, sl=0.0, tp=0.0, magic=0):
    return SimpleNamespace(
        ticket=ticket,
        symbol=symbol,
        type=type_,
        volume=volume,
        price_open=1.1000,
        price_current=1.1002,
        sl=sl,
        tp=tp,
        profit=0.0,
        swap=0.0,
        commission=0.0,
        magic=magic,
        comment="",
        time=int(time.time()),
    )


def _account(**overrides):
    fields = {
        "login": 123456,
        "name": "Test",
        "server": "Test-Server",
        "currency": "USD",
        "trade_mode": 0,
        "leverage": 100,
        "balance": 10000.0,
        "equity": 10000.0,
        "margin": 0.0,
        "margin_free": 10000.0,
        "margin_level": 0.0,
        "profit": 0.0,
        "swap": 0.0,
        "commission": 0.0,
        "credit": 0.0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def ctx(tmp_path):
    """BridgeContext with a fake MT5; the fake is reachable as ctx.driver._mt5."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _FakeMt5(
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": _tick()},
        positions=[_position(9000001)],
    )
    driver._mt5 = fake
    driver._connected = True
    context = BridgeContext(settings=settings, driver=driver)
    try:
        yield context
    finally:
        driver.disconnect()


def _executor(ctx):
    validator = CommandValidator("device-1", get_account_id=lambda: "123456")
    return CommandExecutor(ctx, validator)


# ----------------------------------------------------------------------
# R1 — sync checkpoint mis-parses broker-local epochs as UTC
# ----------------------------------------------------------------------
def test_r1_parse_broker_time_epoch_subtracts_offset():
    noon_z = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    epoch = int(noon_z.timestamp())
    assert parse_broker_time(epoch, 120) == datetime(2026, 11, 16, 10, 0, tzinfo=UTC)
    assert parse_broker_time(epoch, 0) == noon_z


def test_r1_parse_broker_time_iso_strings_stay_utc():
    # ISO strings are UTC by codebase convention — the offset must NOT shift them.
    assert parse_broker_time("2026-11-16T12:00:00Z", 120) == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_r1_checkpoint_advance_applies_offset_to_epochs():
    # A +2 broker: a deal at 12:00 broker-local is 10:00 UTC, so the
    # 11:00 UTC ISO time_utc must win the max — before the fix the epoch
    # read as 12:00 UTC and the checkpoint landed 2h ahead, inverting
    # the next history range and stalling sync permanently.
    noon_z = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    cp = SyncCheckpoint()
    moved = cp.advance_from_items(
        {
            "deals": [{"ticket": 9, "time": int(noon_z.timestamp())}],
            "orders": [{"ticket": 2, "time_utc": "2026-11-16T11:00:00Z"}],
        },
        120,
    )
    assert moved == 1
    assert cp.value == datetime(2026, 11, 16, 11, 0, tzinfo=UTC)


# ----------------------------------------------------------------------
# R2 — validate_pending rejects every pending order when volume_max is omitted
# ----------------------------------------------------------------------
def test_r2_omitted_volume_bounds_are_no_constraint(ctx):
    ctx.driver._mt5.symbol_rows["EURUSD"] = _symbol_row("EURUSD", volume_min=None, volume_max=None, volume_step=None)
    reasons = validate_pending(ctx, "EURUSD", "BUY_LIMIT", 1.09, 0.0, 0.0, 0.5)
    assert reasons == []


def test_r2_reported_volume_bounds_still_enforced(ctx):
    ctx.driver._mt5.symbol_rows["EURUSD"] = _symbol_row("EURUSD", volume_max=100.0)
    assert validate_pending(ctx, "EURUSD", "BUY_LIMIT", 1.09, 0.0, 0.0, 0.5) == []
    assert "volume_above_max" in validate_pending(ctx, "EURUSD", "BUY_LIMIT", 1.09, 0.0, 0.0, 101.0)


# ----------------------------------------------------------------------
# R3 — resolve_filling_mode crashes on a non-numeric filling_mode
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (3, 1),  # FOK|IOC bits -> IOC
        (1, 0),  # FOK only
        (0, 2),  # none -> RETURN
        (None, 2),  # unreported -> RETURN
        ("IOC", 1),
        ("FOK", 0),
        ("RETURN", 2),
        ("2", 1),  # numeric string
        ("garbage", 1),  # unparseable -> IOC default, never a crash
    ],
)
def test_r3_filling_mode_coerced_defensively(ctx, raw, expected):
    ctx.driver._mt5.symbol_rows["EURUSD"] = _symbol_row("EURUSD", filling_mode=raw)
    assert resolve_filling_mode(ctx, "EURUSD") == expected


# ----------------------------------------------------------------------
# R4 — Entitlements.from_dict crashes on non-numeric counts
# ----------------------------------------------------------------------
def test_r4_entitlements_non_numeric_counts_degrade_to_zero():
    ent = Entitlements.from_dict({"max_devices": "abc", "max_accounts": "3"})
    assert ent.max_devices == 0
    assert ent.max_accounts == 3


# ----------------------------------------------------------------------
# R5 — unreported trade_mode labelled "demo"; margin fields omitted
# ----------------------------------------------------------------------
def test_r5_unreported_trade_mode_omits_type():
    items = SyncEngine.account_items(_account(trade_mode=None), {"state": "CONNECTED"})
    assert "type" not in items["account"]


def test_r5_unknown_trade_mode_omits_type():
    items = SyncEngine.account_items(_account(trade_mode=99), {"state": "CONNECTED"})
    assert "type" not in items["account"]


def test_r5_known_trade_mode_still_labelled():
    items = SyncEngine.account_items(_account(trade_mode=0), {"state": "CONNECTED"})
    assert items["account"]["type"] == "demo"


def test_r5_margin_fields_included_and_labelled_mt5_supplied():
    acc = _account(margin_level=250.0, leverage=100, swap=1.5, commission=2.5)
    items = SyncEngine.account_items(acc, {"state": "CONNECTED"})
    account = items["account"]
    assert account["margin_level"] == 250.0
    assert account["leverage"] == 100
    assert account["swap"] == 1.5
    assert account["commission"] == 2.5
    for key in ("margin_level", "leverage", "swap", "commission"):
        assert items["provenance"][key] == "mt5_supplied"


# ----------------------------------------------------------------------
# R6 — _reconcile 1e-9 tolerance reports landed modifications as UNKNOWN
# ----------------------------------------------------------------------
def test_r6_reconcile_recovers_tick_normalized_sl(ctx):
    # Broker normalized the requested 1.085015 to one tick (1.08501);
    # the modification landed and must be recovered, not UNKNOWN.
    ctx.driver._mt5.positions[9000001] = _position(9000001, sl=1.08501, tp=0.0)
    executor = _executor(ctx)
    envelope = CommandEnvelope(
        command_id="c1",
        account_id="123456",
        device_id="device-1",
        operation_type="modify_sl_tp",
        ticket="9000001",
        sl=1.085015,
        tp=0.0,
    )
    recovered = executor._reconcile(envelope)
    assert recovered is not None
    assert recovered["status"] == "done"
    assert recovered["sl"] == 1.08501


def test_r6_reconcile_still_unknown_when_modification_did_not_land(ctx):
    # Wanted SL differs from the live value by a full tick — genuinely
    # unlanded, must stay UNKNOWN (no false recovery).
    ctx.driver._mt5.positions[9000001] = _position(9000001, sl=1.08501, tp=0.0)
    executor = _executor(ctx)
    envelope = CommandEnvelope(
        command_id="c1",
        account_id="123456",
        device_id="device-1",
        operation_type="modify_sl_tp",
        ticket="9000001",
        sl=1.08502,
        tp=0.0,
    )
    assert executor._reconcile(envelope) is None


# ----------------------------------------------------------------------
# R10 — parse_envelope TypeError escapes validate()
# ----------------------------------------------------------------------
def test_r10_non_dict_frame_is_malformed_not_a_crash(ctx):
    executor = _executor(ctx)
    for bad_frame in ("not a dict", None, 42, ["command"]):
        outcome = executor.validator.validate(bad_frame)
        assert outcome.rejection is not None
        assert outcome.rejection.name == "MALFORMED"


# ----------------------------------------------------------------------
# A1 — broker-offset detection accepts stale ticks
# ----------------------------------------------------------------------
def test_a1_stale_tick_does_not_override_configured_offset(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    settings.broker_utc_offset_minutes = 120  # correct EET offset
    driver = Mt5Driver(settings)
    # Market closed 5h ago on a +2 broker: the freshest tick is 3h behind UTC.
    stale = int(time.time()) - 3 * 3600
    fake = _FakeMt5(
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": SimpleNamespace(time=stale, bid=1.1, ask=1.1002, last=1.1001, volume=10, flags=0)},
    )
    driver._mt5 = fake
    driver._connected = True
    driver._detect_broker_offset(fake)
    assert settings.broker_utc_offset_minutes == 120


def test_a1_fresh_tick_still_detects_offset(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    settings.broker_utc_offset_minutes = 0
    driver = Mt5Driver(settings)
    fresh = int(time.time()) + 120 * 60  # broker-local now on a +2 broker
    fake = _FakeMt5(
        symbol_rows=[_symbol_row("EURUSD")],
        ticks={"EURUSD": SimpleNamespace(time=fresh, bid=1.1, ask=1.1002, last=1.1001, volume=10, flags=0)},
    )
    driver._mt5 = fake
    driver._connected = True
    driver._detect_broker_offset(fake)
    assert settings.broker_utc_offset_minutes == 120


# ----------------------------------------------------------------------
# A2 — two different "tradable" answers
# ----------------------------------------------------------------------
def test_a2_tradeable_only_excludes_unreported_trade_mode(ctx):
    ctx.driver._mt5.symbol_rows = {
        "EURUSD": _symbol_row("EURUSD", trade_mode=4),
        "XAUUSD": _symbol_row("XAUUSD", trade_mode=0),
        "GHOST": _symbol_row("GHOST", trade_mode=None),
    }
    result = get_trading_symbols(ctx, SymbolsListRequest(tradeable_only=True))
    names = [item["name"] for item in result["symbols"]]
    assert names == ["EURUSD"]


# ----------------------------------------------------------------------
# A3 — zero-price tick reaches the broker
# ----------------------------------------------------------------------
def test_a3_market_order_zero_ask_refuses(ctx):
    ctx.driver._mt5.ticks["EURUSD"] = _tick(bid=1.1000, ask=0.0)
    with pytest.raises(BridgeError) as excinfo:
        market_order(ctx, symbol="EURUSD", side="buy", volume=0.1)
    assert excinfo.value.code is ErrorCode.MT5_CALL_FAILED
    assert ctx.driver._mt5.send_calls == 0


def test_a3_close_position_zero_bid_refuses(ctx):
    ctx.driver._mt5.ticks["EURUSD"] = _tick(bid=0.0, ask=1.1002)
    with pytest.raises(BridgeError) as excinfo:
        close_position(ctx, ticket=9000001)
    assert excinfo.value.code is ErrorCode.MT5_CALL_FAILED
    assert ctx.driver._mt5.send_calls == 0


# ----------------------------------------------------------------------
# A4 — last_error()[0] indexing after failed connect
# ----------------------------------------------------------------------
def test_a4_failed_connect_with_no_last_error_returns_failure_dict(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"), mt5_retries=1)
    driver = Mt5Driver(settings)
    if not MT5_PLATFORM_SUPPORTED:
        # v1.3.0 honest gate: on a platform where the IPC driver cannot
        # load, connect() fails fast with MT5_WINDOWS_REQUIRED before
        # any fake/real MT5 module is consulted — the A4
        # initialize-failed path below is only reachable where a
        # driver can load (Windows x64).
        result = driver.connect()
        assert result["ok"] is False
        assert result["diagnostic_code"] == MT5_WINDOWS_REQUIRED_CODE
        return
    fake = _FakeMt5(symbol_rows=[_symbol_row("EURUSD")], ticks={"EURUSD": _tick()})
    fake.initialize_result = False
    fake.last_error_result = None  # the broker reports no error detail
    driver._mt5 = fake
    result = driver.connect()
    assert result["ok"] is False
    assert driver._last_error == {"code": None, "message": "initialize failed (no error detail)"}
