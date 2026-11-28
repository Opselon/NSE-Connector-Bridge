"""Unit tests for the modify serialization lock (Codex P1).

Label: unit. No MT5 — a fake MT5 module is injected into the driver.

``modify_sl_tp`` is read-modify-write: the dispatcher fetches the live
position, fills the unspecified side (SL or TP) from it, and sends.
Two concurrent partial updates for the same position interleave and
one is lost (the second send overwrites the first's change with the
stale value it read). ``CommandExecutor`` serializes modify ops with
a lock; this test proves the invariant deterministically with a
barrier in the fake's first read per thread: without the lock both
threads read the stale (0, 0) before either sends and the first
update is lost; with the lock the second thread's read sees the
first send's result and both updates land.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.server.commands import CommandExecutor, CommandState, CommandValidator
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.unit

DEVICE = "dev-1"
ACCOUNT = "12345678901"
TICKET = 9000001


class _FakeMt5:
    """One position; SLTP sends apply to it. The first read per thread
    rendezvous at a barrier so two concurrent reads provably overlap
    when the lock is absent."""

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

    def __init__(self) -> None:
        self.position = SimpleNamespace(
            ticket=TICKET,
            symbol="EURUSD",
            type=0,
            volume=1.0,
            price_open=1.1000,
            price_current=1.1002,
            sl=0.0,
            tp=0.0,
            profit=0.0,
            swap=0.0,
            commission=0.0,
            magic=0,
            comment="",
            time=0,
        )
        self._fetch_barrier = threading.Barrier(2, timeout=1.0)
        self._seen_threads: set[int] = set()
        self._seen_lock = threading.Lock()

    def positions_get(self, ticket=None, symbol=None):
        tid = threading.get_ident()
        with self._seen_lock:
            first = tid not in self._seen_threads
            self._seen_threads.add(tid)
        if first:
            try:
                self._fetch_barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass  # locked case: the other thread is still behind the lock
        if ticket is not None:
            return [self.position] if int(ticket) == TICKET else []
        return [self.position]

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
        # P2-4: modify_position_sltp pre-flights SL/TP geometry
        # against the live tick (bid 1.1000 / ask 1.1002 for the long
        # at 1.1000) — a None tick would fail the modify closed.
        return SimpleNamespace(time=0, bid=1.1000, ask=1.1002, last=1.1001, volume=10, flags=0)

    def account_info(self):
        return SimpleNamespace(login=123456, trade_mode=0, margin_mode=1)

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def last_error(self):
        return (0, "")

    def shutdown(self):
        pass

    def order_check(self, request):
        """Official pre-send check (W2): always passes in this fake."""
        self.check_calls = getattr(self, "check_calls", 0) + 1
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
        if request.get("action") == self.TRADE_ACTION_SLTP:
            self.position.sl = float(request["sl"])
            self.position.tp = float(request["tp"])
        return SimpleNamespace(retcode=10009, order=0, deal=8000001, price=0.0, volume=0.0, comment="")


def _ctx(tmp_path: Path) -> BridgeContext:
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    driver._mt5 = _FakeMt5()
    driver._connected = True
    ctx = BridgeContext(settings=settings, driver=driver)
    return ctx


def _frame(*, command_id: str, sl: float | None, tp: float | None) -> dict:
    now = datetime.now(UTC)
    operation: dict = {"type": "modify_sl_tp", "ticket": TICKET}
    if sl is not None:
        operation["sl"] = sl
    if tp is not None:
        operation["tp"] = tp
    return {
        "type": "command",
        "command_id": command_id,
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "operation": operation,
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": command_id,
    }


def _validator() -> CommandValidator:
    return CommandValidator(
        DEVICE,
        get_account_id=lambda: ACCOUNT,
        entitlements=Entitlements(position_manager=True, auto_trade=True),
        clock=lambda: datetime.now(UTC),
    )


@pytest.mark.unit
def test_concurrent_partial_sltp_updates_both_land(tmp_path: Path):
    ctx = _ctx(tmp_path)
    executor = CommandExecutor(ctx, _validator(), results_queue=None)
    fake: _FakeMt5 = ctx.driver._mt5  # type: ignore[assignment]

    results: list = []

    def run(frame: dict) -> None:
        results.append(executor.execute(frame))

    t1 = threading.Thread(target=run, args=(_frame(command_id=new_uuid7_str(), sl=1.08, tp=None),))
    # TP 1.11: the position is a long at bid 1.1000, so the TP must sit
    # above the current price (P2-4 geometry pre-flight rejects a
    # below-market TP before any send).
    t2 = threading.Thread(target=run, args=(_frame(command_id=new_uuid7_str(), sl=None, tp=1.11),))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()  # no deadlock on the lock

    for execution, result in results:
        assert execution.state is CommandState.COMPLETED, result
        assert result["status"] == "executed"
    # Both partial updates landed: the second thread's read saw the
    # first send's SL instead of the stale 0.0.
    assert fake.position.sl == 1.08
    assert fake.position.tp == 1.11
