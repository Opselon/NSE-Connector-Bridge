"""Close-path IPC caching (audit P1-4) — hop-count regression tests.

Label: unit. No MT5 — a fake MT5 module is injected into the real
``Mt5Driver`` (same pattern as ``test_order_check_gateway.py``), and
the driver's own per-op metrics (``driver.metrics_dict()``) count the
terminal IPC hops, so each test pins the exact hop budget:

* full close, steady state: 4 hops (``positions_get`` + ``symbol_info_tick``
  + ``order_check`` + ``order_send_close``) — ``mt5_constants`` and
  ``symbol_info`` (filling mode) are served from the per-context cache
  after the first close;
* retryable close: the retryable-retcode guard's ``positions_get`` is
  carried into the next attempt instead of fetched twice;
* ``order_check`` + partial-close reconciliation still run fresh every
  time — the caching is latency only, never a behaviour change.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine import common as common_mod
from nse_bridge.engine.common import BridgeContext, resolve_filling_mode
from nse_bridge.engine.retry import ExecutionRetryPolicy
from nse_bridge.engine.trading import close_position, modify_position_sltp, mt5_trade_constants
from nse_bridge.mt5.driver import Mt5Driver

pytestmark = pytest.mark.unit

TICKET = 9000001


class _FakeMt5:
    """Minimal MT5 module with a scripted order_send + live positions.

    ``send_script`` is the retcode returned by successive ``order_send``
    calls (default: every send reports DONE 10009). A close send
    mutates the fake book the way the terminal would: a full close
    removes the position, a partial close reduces its volume.
    """

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

    def __init__(self, *, positions=None, send_script=None):
        self.positions = {p.ticket: p for p in (positions or [])}
        self.send_script = list(send_script or [])
        self.send_calls = 0
        self.symbol_info_calls = 0

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            p = self.positions.get(int(ticket))
            return [p] if p is not None else []
        rows = list(self.positions.values())
        return [p for p in rows if p.symbol == symbol] if symbol else rows

    def orders_get(self, ticket=None, symbol=None):
        return []

    def symbol_info(self, name):
        self.symbol_info_calls += 1
        return SimpleNamespace(
            name=name,
            visible=True,
            trade_mode=4,
            digits=5,
            point=0.00001,
            trade_tick_size=0.00001,
            trade_stops_level=10,
            trade_freeze_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            filling_mode=3,  # FOK|IOC bits -> IOC
        )

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(time.time()), bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)

    def account_info(self):
        return SimpleNamespace(login=123456, trade_mode=0, margin_mode=2, balance=10000.0, equity=10000.0)

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
        self.send_calls += 1
        retcode = self.send_script.pop(0) if self.send_script else 10009
        ticket = request.get("position")
        if retcode == 10009 and ticket is not None and int(ticket) in self.positions:
            pos = self.positions[int(ticket)]
            vol = float(request.get("volume") or 0.0)
            if vol + 1e-9 >= float(pos.volume):
                del self.positions[int(ticket)]
            else:
                pos.volume = float(pos.volume) - vol
        return SimpleNamespace(
            retcode=retcode,
            order=8000001,
            deal=8000001,
            price=request.get("price", 0.0),
            volume=request.get("volume", 0.0),
            comment="",
        )


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


@pytest.fixture
def ctx(tmp_path):
    """BridgeContext with a fake MT5; the fake is reachable as ctx.driver._mt5."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    fake = _FakeMt5(positions=[_position(TICKET)])
    driver._mt5 = fake
    driver._connected = True
    context = BridgeContext(settings=settings, driver=driver)
    try:
        yield context
    finally:
        driver.disconnect()


def _op_calls(ctx, op):
    return int(ctx.driver.metrics_dict().get(op, {}).get("calls", 0))


def _reopen(ctx, volume=1.0):
    ctx.driver._mt5.positions[TICKET] = _position(TICKET, volume=volume)


# ----------------------------------------------------------------------
# P1-4: constants + filling mode cached across closes
# ----------------------------------------------------------------------
def test_close_caches_constants_and_filling_across_closes(ctx):
    out1 = close_position(ctx, TICKET)
    assert out1["status"] == "done"
    _reopen(ctx)
    out2 = close_position(ctx, TICKET)
    assert out2["status"] == "done"
    # Two full closes: 4 fresh hops each (position + tick + check + send);
    # constants + filling mode fetched once, on the first close.
    assert _op_calls(ctx, "mt5_constants") == 1
    assert _op_calls(ctx, "symbol_info") == 1
    assert _op_calls(ctx, "positions_get") == 2
    assert _op_calls(ctx, "symbol_info_tick") == 2
    assert _op_calls(ctx, "order_check") == 2
    assert _op_calls(ctx, "order_send_close") == 2


def test_cached_constants_returned_as_copy(ctx):
    consts = mt5_trade_constants(ctx)
    assert consts["action_deal"] == 0
    consts["action_deal"] = 999  # a caller mutating its copy must not poison the cache
    assert mt5_trade_constants(ctx)["action_deal"] == 0
    assert _op_calls(ctx, "mt5_constants") == 1


def test_filling_cache_is_per_symbol(ctx):
    assert resolve_filling_mode(ctx, "EURUSD") == 1
    assert resolve_filling_mode(ctx, "XAUUSD") == 1
    assert resolve_filling_mode(ctx, "EURUSD") == 1  # cache hit, no new hop
    assert _op_calls(ctx, "symbol_info") == 2


def test_filling_cache_expires_after_ttl(ctx, monkeypatch):
    monkeypatch.setattr(common_mod, "FILLING_MODE_CACHE_TTL_S", 0)
    assert resolve_filling_mode(ctx, "EURUSD") == 1
    assert resolve_filling_mode(ctx, "EURUSD") == 1  # TTL 0: every call re-fetches
    assert _op_calls(ctx, "symbol_info") == 2


def test_constants_invalidated_on_reconnect_epoch_bump(ctx):
    mt5_trade_constants(ctx)
    assert _op_calls(ctx, "mt5_constants") == 1
    # The real driver bumps connection_epoch on every successful
    # connect (Mt5Driver._connect_job); simulate that reconnect here.
    ctx.driver._connection_epoch += 1
    mt5_trade_constants(ctx)  # miss: re-fetch against the new session
    assert _op_calls(ctx, "mt5_constants") == 2
    mt5_trade_constants(ctx)  # hit again under the new epoch
    assert _op_calls(ctx, "mt5_constants") == 2
    # Filling modes invalidate on the same epoch bump.
    assert resolve_filling_mode(ctx, "EURUSD") == 1
    assert _op_calls(ctx, "symbol_info") == 1


def test_explicit_invalidate_drops_cache(ctx):
    mt5_trade_constants(ctx)
    resolve_filling_mode(ctx, "EURUSD")
    ctx.ipc_cache.invalidate()
    mt5_trade_constants(ctx)
    resolve_filling_mode(ctx, "EURUSD")
    assert _op_calls(ctx, "mt5_constants") == 2
    assert _op_calls(ctx, "symbol_info") == 2


# ----------------------------------------------------------------------
# P1-4: retry carries the guard's position fetch into the next attempt
# ----------------------------------------------------------------------
def test_close_retry_reuses_guard_position_fetch(ctx):
    ctx.driver._mt5.send_script = [10004, 10009]  # REQUOTE (retryable), then DONE
    policy = ExecutionRetryPolicy(sleep=lambda _: None)
    out = close_position(ctx, TICKET, retry_policy=policy)
    assert out["status"] == "done"
    assert out["attempts"] == 2
    # Attempt 1: fetch + tick + filling + constants + check + send.
    # Guard: one more fetch (position still open) — carried into
    # attempt 2, so NO third fetch. Without the carry it would be 3.
    assert _op_calls(ctx, "positions_get") == 2
    assert _op_calls(ctx, "mt5_constants") == 1
    assert _op_calls(ctx, "symbol_info") == 1
    # order_check still runs fresh on every attempt (untouched).
    assert _op_calls(ctx, "order_check") == 2
    assert _op_calls(ctx, "order_send_close") == 2


# ----------------------------------------------------------------------
# P1-4: modify_position_sltp gets the same caching via the shared helpers
# ----------------------------------------------------------------------
def test_modify_caches_constants_and_filling_across_calls(ctx):
    out1 = modify_position_sltp(ctx, TICKET, sl=1.09, tp=1.11)
    assert out1["status"] == "done"
    out2 = modify_position_sltp(ctx, TICKET, sl=1.09, tp=1.11)
    assert out2["status"] == "done"
    assert _op_calls(ctx, "mt5_constants") == 1
    # symbol_info is 3, not 1: the filling-mode fetch is still cached
    # (1 call across both modifies), but the P2-4 geometry pre-flight
    # reads the symbol spec fresh per modify (2 calls) — stops level
    # is broker state and must not be cached, same as the position
    # fetch + order_check below. The tick read is likewise fresh per
    # modify: the geometry is checked against the CURRENT market.
    assert _op_calls(ctx, "symbol_info") == 3
    assert _op_calls(ctx, "symbol_info_tick") == 2
    # Position fetch + order_check still fresh per modify (untouched).
    assert _op_calls(ctx, "positions_get") == 2
    assert _op_calls(ctx, "order_check") == 2
    assert _op_calls(ctx, "order_send_sltp") == 2


# ----------------------------------------------------------------------
# Reconciliation untouched: partial close still re-reads live state
# ----------------------------------------------------------------------
def test_partial_close_still_reconciles_against_live_state(ctx):
    out = close_position(ctx, TICKET, volume=0.4)
    assert out["status"] == "done"
    assert out["partial"] is True
    assert out["closed_volume"] == pytest.approx(0.4)
    assert out["remaining_volume"] == pytest.approx(0.6)
    assert out["reconciled"] is True
    # Fetch + reconciliation re-read: the re-read is NOT collapsed.
    assert _op_calls(ctx, "positions_get") == 2
    assert _op_calls(ctx, "mt5_constants") == 1
    assert _op_calls(ctx, "symbol_info") == 1
