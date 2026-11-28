"""Realtime latency baseline for Bridge v1.4.0 (measurement-only).

Measures the realtime path end-to-end against the in-repo mock
Platform server (``scripts/mock_server.py``) on local loopback, with a
simulated MT5 terminal (``FakeMt5``) behind the same serialized
single-worker-thread shape the production ``Mt5Driver`` uses:

  (a) WS command receive -> MT5 submit -> action.result ingest
      (``close_position``, the v1 trading op)
  (b) MT5 tick -> Bridge processing -> platform ingest (``chart.tick``)
  (c) chart warm-up: ``chart.request`` count=500 per symbol/timeframe
  (d) contention: busy local dashboard / blocked MT5 worker vs idle
      (d1 dashboard hammer, d2 one in-flight 300 ms fetch, d3 a
      queued flood of chart fetches — the P1-3 acceptance phase:
      with the priority lane a close jumps queued chart work)

The MT5 worker is a serialized single thread with priority lanes
(P1-3): server commands (closes in the URGENT lane) jump chart /
sync / poll jobs still queued on the worker; a job already running
is never pre-empted. The FakeDriver below mirrors that queue shape
exactly, so the contention phases measure the production lane
assignment, not a bench-only approximation.

Method: the REAL ``ServerChannel``, ``CommandExecutor``,
``CommandValidator`` and ``ChartService`` run; only the terminal and
the platform are fakes. MT5 IPC latency is a parameter
(``--mt5-latency-ms`` per terminal call): profile A runs it at 0 ms
(the Bridge-only floor) and profile B at 5 ms/call (a realistic
local MT5 IPC hop). Per-step timestamps use ``time.monotonic()`` on
the same host, so every step is directly comparable.

No production, no live MT5, no real credentials. Nothing in
``nse_bridge/`` is modified; all instrumentation lives in this
script (wrappers around the real methods, installed at runtime).

Usage (from the repo root, with a venv that has the bridge deps)::

    python scripts/bench_realtime.py [--samples 100] [--out report.json]

Exit code 0 when every phase produced the requested samples; 2 when
a phase came up short (the JSON still carries what was measured).
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import platform
import queue
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")

import websockets
from mock_server import MockServer

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import (
    MT5_PRIORITY_BACKGROUND,
    MT5_PRIORITY_COMMAND,
    current_mt5_priority,
)
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.chart import ChartService
from nse_bridge.server.commands import CommandExecutor, CommandValidator
from nse_bridge.server.rest import Entitlements
from nse_bridge.server.uuid7 import new_uuid7_str

ACCOUNT_ID = "12345678901"  # mock-server demo account == fake terminal login
TICKET = 9000001
SYMBOL = "EURUSD"


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def pct(samples: list[float], p: float) -> float:
    """Nearest-rank percentile (ms). Empty input -> NaN (never invent)."""
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, round(p / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


def stats_ms(samples: list[float]) -> dict[str, Any]:
    samples = [round(s, 3) for s in samples]
    return {
        "n": len(samples),
        "p50_ms": round(pct(samples, 50), 3),
        "p95_ms": round(pct(samples, 95), 3),
        "p99_ms": round(pct(samples, 99), 3),
        "mean_ms": round(statistics.fmean(samples), 3) if samples else float("nan"),
        "max_ms": round(max(samples), 3) if samples else float("nan"),
    }


# ---------------------------------------------------------------------------
# Fake MT5 terminal (in-memory; one IPC sleep per call, like the real IPC)
# ---------------------------------------------------------------------------
class FakeMt5:
    """In-memory MT5 terminal: the calls the bench path makes, with a
    configurable per-call sleep standing in for the real terminal IPC
    round-trip. State is deliberately tiny: one open position, one
    symbol, empty order/history books."""

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_MODIFY = 7
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 1
    TIMEFRAME_M1 = 1

    def __init__(self, latency_ms: float = 0.0) -> None:
        self.latency_ms = latency_ms
        self._lock = threading.Lock()
        self._deal_seq = 2000000
        self._positions: dict[int, dict[str, Any]] = {}
        self.reset_position()

    def _ipc(self) -> None:
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

    def reset_position(self) -> None:
        """Re-open the bench position (a close consumes it)."""
        with self._lock:
            self._positions[TICKET] = {
                "ticket": TICKET,
                "symbol": SYMBOL,
                "type": 0,  # buy
                "volume": 0.10,
                "price_open": 1.08400,
                "price_current": 1.08501,
                "sl": 0.0,
                "tp": 0.0,
                "profit": 10.10,
                "swap": 0.0,
                "commission": 0.0,
                "magic": 0,
                "comment": "bench",
                "time": int(time.time()),
            }

    # -- terminal API used by the measured paths + connect-time sync -----
    def positions_get(self, ticket: int | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        self._ipc()
        with self._lock:
            rows = list(self._positions.values())
        if ticket is not None:
            return [dict(r) for r in rows if r["ticket"] == ticket]
        if symbol is not None:
            return [dict(r) for r in rows if r["symbol"] == symbol]
        return [dict(r) for r in rows]

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        self._ipc()
        return SimpleNamespace(time=int(time.time()), bid=1.08501, ask=1.08523, last=1.08501, volume=1)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self._ipc()
        return True

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        self._ipc()
        return SimpleNamespace(
            name=symbol,
            description=f"{symbol} bench",
            path="",
            visible=True,
            select=True,
            trade_mode=4,
            digits=5,
            point=0.00001,
            spread=2,
            spread_float=0.0,
            trade_tick_size=0.00001,
            trade_tick_value=1.0,
            trade_tick_value_loss=1.0,
            trade_contract_size=100000.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            volume_limit=0.0,
            trade_stops_level=0,
            trade_freeze_level=0,
            trade_exemode=0,
            trade_calc_mode=0,
            filling_mode=2,  # IOC
            swap_long=0.0,
            swap_short=0.0,
            margin_initial=0.0,
            margin_maintenance=0.0,
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="EUR",
            bid=1.08501,
            ask=1.08523,
            last=1.08501,
            session_deals=0,
            session_volume=0,
        )

    def account_info(self) -> dict[str, Any]:
        self._ipc()
        return {
            "login": int(ACCOUNT_ID),
            "name": "Bench",
            "server": "Bench-Server",
            "currency": "USD",
            "trade_mode": 0,
            "leverage": 100,
            "balance": 10000.0,
            "equity": 10010.10,
            "margin": 100.0,
            "margin_free": 9910.10,
            "margin_level": 10010.1,
            "profit": 10.10,
            "swap": 0.0,
            "commission": 0.0,
            "credit": 0.0,
            "margin_so_mode": 0,
            "margin_so_call": 50.0,
            "margin_so_so": 20.0,
            "margin_mode": 0,
            "trade_allowed": True,
            "trade_expert": True,
        }

    def order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        self._ipc()
        return {
            "retcode": 0,
            "balance": 10000.0,
            "equity": 10010.10,
            "profit": 10.10,
            "margin": 100.0,
            "margin_free": 9910.10,
            "margin_level": 10010.1,
            "comment": "Done",
        }

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        self._ipc()
        with self._lock:
            self._deal_seq += 1
            deal = self._deal_seq
            ticket = request.get("position")
            if ticket is not None:
                self._positions.pop(int(ticket), None)
        return {
            "retcode": 10009,  # TRADE_RETCODE_DONE
            "order": deal,
            "deal": deal,
            "price": 1.08501,
            "volume": float(request.get("volume") or 0.10),
            "comment": "Done",
            "request_id": 1,
            "retcode_external": 0,
        }

    def orders_get(self, ticket: int | None = None, symbol: str | None = None) -> list[Any]:
        self._ipc()
        return []

    def history_orders_get(self, from_dt: Any, to_dt: Any, group: str | None = None) -> list[Any]:
        self._ipc()
        return []

    def history_deals_get(self, from_dt: Any, to_dt: Any, group: str | None = None) -> list[Any]:
        self._ipc()
        return []

    def terminal_info(self) -> SimpleNamespace:
        self._ipc()
        return SimpleNamespace(
            connected=True,
            name="Fake MT5",
            trade_allowed=True,
            login=int(ACCOUNT_ID),
            trade_mode=0,
        )

    def symbols_get(self) -> list[SimpleNamespace]:
        self._ipc()
        return [SimpleNamespace(name=SYMBOL, visible=True), SimpleNamespace(name="XAUUSD", visible=True)]

    def version(self) -> tuple[int, int, int]:
        return (5, 0, 6231)


# ---------------------------------------------------------------------------
# Fake driver: same serialized single-worker-thread shape as Mt5Driver
# ---------------------------------------------------------------------------
class FakeDriver:
    """Serialized worker thread + bounded priority queue, mirroring
    ``Mt5Driver.run`` (queue-wait included in the recorded duration,
    per-op metrics, fast failure when the queue is full).

    The queue shape mirrors the production driver exactly (P1-3):
    entries are (priority, seq, job, future) — lower priority runs
    first, FIFO within a lane — and the ambient lane is read from the
    same :func:`current_mt5_priority` the production scopes set, so
    the command/chart/poll lane assignment in ``nse_bridge/`` is what
    this bench measures. The worker stays strictly serial: one job
    at a time, never parallel."""

    def __init__(self, mt5: FakeMt5) -> None:
        self._mt5 = mt5
        self._jobs: queue.PriorityQueue[tuple[int, int, Any, Future]] = queue.PriorityQueue(maxsize=256)
        self._seq = itertools.count()
        self._thread = threading.Thread(name="fake-mt5-worker", target=self._work, daemon=True)
        self._metrics: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()
        self._max_queue_depth = 0
        self._connected = True
        self._thread.start()

    def _work(self) -> None:
        while True:
            _priority, _seq, job, fut = self._jobs.get()
            try:
                fut.set_result(job(self._mt5))
            except BaseException as exc:  # noqa: BLE001 — isolate the worker
                fut.set_exception(exc)

    def run(
        self,
        job: Any,
        op: str | None = None,
        context: dict[str, Any] | None = None,
        timeout: float = 60.0,
        priority: int | None = None,
    ) -> Any:
        name = op or str(getattr(job, "__name__", "unnamed_job"))
        lane = current_mt5_priority() if priority is None else priority
        fut: Future = Future()
        try:
            self._jobs.put_nowait((lane, next(self._seq), job, fut))
        except queue.Full as exc:
            raise RuntimeError("MT5 job queue full (256) — worker stalled") from exc
        with self._lock:
            self._max_queue_depth = max(self._max_queue_depth, self._jobs.qsize())
        t0 = time.perf_counter()
        try:
            result = fut.result(timeout=timeout)
        except Exception:
            self._record(name, False, (time.perf_counter() - t0) * 1000.0)
            raise
        self._record(name, True, (time.perf_counter() - t0) * 1000.0)
        return result

    def run_order_send(
        self,
        job: Any,
        op: str | None = None,
        context: dict[str, Any] | None = None,
        priority: int | None = None,
    ) -> Any:
        """Placement path: mirrors ``Mt5Driver.run_order_send`` —
        COMMAND lane unless an explicit priority is given."""
        lane = MT5_PRIORITY_COMMAND if priority is None else priority
        return self.run(job, op=op, context=context, timeout=30.0, priority=lane)

    @property
    def queued_jobs(self) -> int:
        """Jobs currently waiting in the queue (excludes the one
        running on the worker, if any)."""
        return self._jobs.qsize()

    def _record(self, op: str, ok: bool, duration_ms: float) -> None:
        with self._lock:
            m = self._metrics.setdefault(op, {"calls": 0, "failures": 0, "total_ms": 0.0, "max_ms": 0.0})
            m["calls"] += 1
            m["total_ms"] += duration_ms
            m["max_ms"] = max(m["max_ms"], duration_ms)
            if not ok:
                m["failures"] += 1

    def metrics_snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {k: dict(v) for k, v in self._metrics.items()}

    def total_calls(self) -> int:
        with self._lock:
            return int(sum(m["calls"] for m in self._metrics.values()))

    @property
    def max_queue_depth(self) -> int:
        with self._lock:
            return self._max_queue_depth

    @property
    def connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MT5 adapter is not connected")

    def connection_state_dict(self) -> dict[str, Any]:
        return {
            "state": "CONNECTED",
            "detail": "",
            "since": datetime.now(UTC).isoformat(),
            "connected": True,
            "platform_supported": False,
            "pkg_version": "5.0.6231 (bench)",
            "terminal_version": "Fake MT5",
            "login": int(ACCOUNT_ID),
            "diagnostic_code": None,
            "last_error": None,
        }

    def metrics_dict(self) -> dict[str, Any]:
        return {
            k: {
                "calls": int(v["calls"]),
                "failures": int(v["failures"]),
                "avg_ms": round(v["total_ms"] / v["calls"], 3) if v["calls"] else 0.0,
                "max_ms": round(v["max_ms"], 3),
                "last_ms": round(v["total_ms"] / v["calls"], 3) if v["calls"] else 0.0,
                "last_error": None,
            }
            for k, v in sorted(self._metrics.items())
        }

    def ring_dict(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def refresh_terminal(self) -> dict[str, Any] | None:
        if not self._connected:
            return None

        def job(mt5: Any) -> dict[str, Any]:
            raw = mt5.terminal_info()
            return {
                "connected": bool(getattr(raw, "connected", False)),
                "name": getattr(raw, "name", None),
                "trade_allowed": bool(getattr(raw, "trade_allowed", False)),
                "login": getattr(raw, "login", None),
            }

        return self.run(job, op="terminal_info")


# ---------------------------------------------------------------------------
# Fake chart fetchers (routed through the driver worker, like the real
# bind_mt5_fetchers path — so a slow fetch genuinely occupies the
# serialized MT5 worker and contends with commands)
# ---------------------------------------------------------------------------
def gen_candles(symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
    base = datetime.now(UTC).replace(second=0, microsecond=0)
    step = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}.get(timeframe, 1)
    candles = []
    for i in range(count):
        t = base - timedelta(minutes=step * (count - 1 - i))
        price = 1.08500 + i * 0.00001
        candles.append(
            {
                "time": t.strftime("%Y-%m-%dT%H:%M:%S"),
                "open": round(price, 5),
                "high": round(price + 0.00010, 5),
                "low": round(price - 0.00008, 5),
                "close": round(price + 0.00005, 5),
                "tick_volume": 100 + i,
                "spread": 2,
            }
        )
    return candles


def gen_ticks(symbol: str, count: int) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    return [
        {
            "time_ms": (now - timedelta(milliseconds=100 * (count - 1 - i))).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{i % 1000:03d}",
            "bid": round(1.08501 + i * 0.000001, 5),
            "ask": round(1.08523 + i * 0.000001, 5),
        }
        for i in range(count)
    ]


class BenchHarness:
    """Owns the mock server, the real ServerChannel (with injected
    fake driver / fake chart fetchers / mock entitlements) and the
    timestamp marks that split every round-trip into steps."""

    def __init__(self, *, samples: int, mt5_latency_ms: float, fetch_latency_ms: float) -> None:
        self.samples = samples
        self.mt5_latency_ms = mt5_latency_ms
        self.fetch_latency_ms = fetch_latency_ms
        self.tmpdir = Path(tempfile.mkdtemp(prefix="nse-bench-rt-"))
        self.mock = MockServer(token="test-token", send_demo_command=False)
        self.mt5 = FakeMt5(latency_ms=mt5_latency_ms)
        self.driver = FakeDriver(self.mt5)
        # Per-frame marks: key -> {step: monotonic timestamp}
        self.marks: dict[str, dict[str, float]] = {}
        self.fetch_spans: list[dict[str, Any]] = []  # fetch_candles/fetch_ticks submit->return
        self.enqueue_marks: dict[str, list[float]] = {}  # chart enqueue times per id
        self.fetch_latency_s = fetch_latency_ms / 1000.0
        self.channel: ServerChannel | None = None
        self.ctx: BridgeContext | None = None
        self.device_id = "bench-device-001"

    # -- construction ----------------------------------------------------
    def _fetch_candles(self, symbol: str, timeframe: str, count: int, from_: Any, to: Any) -> list[dict[str, Any]]:
        t0 = time.monotonic()

        def job(mt5: Any) -> list[dict[str, Any]]:
            if self.fetch_latency_s > 0:
                time.sleep(self.fetch_latency_s)
            return gen_candles(symbol, timeframe, count)

        # P1-3: chart fetches run in the BACKGROUND lane (same lane
        # the production bind_mt5_fetchers assigns).
        out = self.driver.run(
            job, op="copy_rates_from_pos", context={"symbol": symbol, "count": count}, priority=MT5_PRIORITY_BACKGROUND
        )
        t1 = time.monotonic()
        self.fetch_spans.append({"op": "fetch_candles", "count": count, "t0": t0, "t1": t1, "ms": (t1 - t0) * 1000.0})
        return out

    def _fetch_ticks(self, symbol: str, count: int, from_: Any, to: Any) -> list[dict[str, Any]]:
        t0 = time.monotonic()

        def job(mt5: Any) -> list[dict[str, Any]]:
            if self.fetch_latency_s > 0:
                time.sleep(self.fetch_latency_s)
            return gen_ticks(symbol, count)

        # P1-3: BACKGROUND lane, as _fetch_candles above.
        out = self.driver.run(
            job, op="copy_ticks_range", context={"symbol": symbol, "count": count}, priority=MT5_PRIORITY_BACKGROUND
        )
        t1 = time.monotonic()
        self.fetch_spans.append({"op": "fetch_ticks", "count": count, "t0": t0, "t1": t1, "ms": (t1 - t0) * 1000.0})
        return out

    def _ensure_market_watch(self, symbol: str) -> None:
        # P1-3: BACKGROUND lane, as _fetch_candles above.
        self.driver.run(
            lambda mt5: mt5.symbol_select(symbol, True),
            op="symbol_select",
            context={"symbol": symbol},
            priority=MT5_PRIORITY_BACKGROUND,
        )

    async def start(self) -> None:
        self.mock.start()
        settings = BridgeSettings(
            environment="dev",
            server_base_url=self.mock.rest_base_url,
            server_url=self.mock.ws_url,
            server_token="test-token",
            device_id=self.device_id,
            data_dir=self.tmpdir,
            heartbeat_interval_s=15.0,
        )
        self.ctx = BridgeContext(settings=settings, driver=self.driver)  # type: ignore[arg-type]
        chart = ChartService(
            fetch_candles=self._fetch_candles,
            fetch_ticks=self._fetch_ticks,
            ensure_market_watch=self._ensure_market_watch,
        )
        # The mock validate-key answers entitlements all-true for the
        # test token; mirror that here (the bench never calls
        # validate-key, so the validator gets the same entitlements
        # the mock server would resolve).
        validator = CommandValidator(
            self.device_id,
            get_account_id=lambda: ACCOUNT_ID,
            entitlements=Entitlements.from_dict(
                {
                    "history_sync": True,
                    "ai_coach": True,
                    "position_manager": True,
                    "auto_trade": True,
                    "max_devices": 3,
                    "max_accounts": 2,
                }
            ),
        )
        executor = CommandExecutor(self.ctx, validator, results_queue=None, user_id=None)
        self.channel = ServerChannel(self.ctx, bus=None, chart_service=chart, executor=executor)
        self._install_wrappers()
        self.channel.start()
        await self._wait_connected()
        await self._wait_quiesce()

    async def _wait_connected(self, timeout: float = 15.0) -> None:
        assert self.channel is not None and self.ctx is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ctx.server_channel_state.get("connected") and self.mock.state.ws_connections >= 1:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("ServerChannel did not connect to the mock server")

    async def _wait_quiesce(self, timeout: float = 20.0) -> None:
        """Wait out the connect-time sync_request burst so steady-state
        phases are not polluted by one-time startup MT5 jobs."""
        deadline = time.monotonic() + timeout
        last_calls = self.driver.total_calls()
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            calls = self.driver.total_calls()
            if calls != last_calls:
                last_calls = calls
                last_change = time.monotonic()
            elif time.monotonic() - last_change >= 1.0:
                return
        raise RuntimeError("MT5 driver did not quiesce after connect")

    async def stop(self) -> None:
        if self.channel is not None:
            await self.channel.stop()
        self.mock.stop()

    # -- runtime instrumentation (wrappers only; product code untouched) -
    def _install_wrappers(self) -> None:
        assert self.channel is not None
        channel = self.channel

        orig_hsm = channel._handle_server_message

        async def hsm_wrapped(raw: Any) -> None:
            try:
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    key = msg.get("command_id") or msg.get("request_id") or msg.get("subscription_id")
                    if key:
                        self.marks.setdefault(str(key), {})["recv"] = time.monotonic()
            except (ValueError, TypeError):
                pass
            return await orig_hsm(raw)

        channel._handle_server_message = hsm_wrapped  # type: ignore[method-assign]

        executor = channel._executor
        assert executor is not None
        orig_execute = executor.execute

        def execute_wrapped(frame: Any) -> Any:
            cid = str(frame.get("command_id") or "") if isinstance(frame, dict) else ""
            self.marks.setdefault(cid, {})["exec_start"] = time.monotonic()
            try:
                return orig_execute(frame)
            finally:
                self.marks[cid]["exec_end"] = time.monotonic()

        executor.execute = execute_wrapped  # type: ignore[method-assign]

        orig_put_result = channel._outbound.put_result

        def put_result_wrapped(frame: dict[str, Any]) -> None:
            cid = frame.get("correlation_id")
            if cid:
                self.marks.setdefault(str(cid), {})["enqueued"] = time.monotonic()
            return orig_put_result(frame)

        channel._outbound.put_result = put_result_wrapped  # type: ignore[method-assign]

        chart = channel._chart
        assert chart is not None
        orig_handle_request = chart.handle_request

        def handle_request_wrapped(frame: dict[str, Any]) -> Any:
            rid = str(frame.get("request_id") or "")
            self.marks.setdefault(rid, {})["chart_start"] = time.monotonic()
            try:
                return orig_handle_request(frame)
            finally:
                self.marks[rid]["chart_end"] = time.monotonic()

        chart.handle_request = handle_request_wrapped  # type: ignore[method-assign]

        orig_enqueue_chart = channel._enqueue_chart

        def enqueue_chart_wrapped(frame: dict[str, Any]) -> None:
            key = str(frame.get("request_id") or frame.get("subscription_id") or "")
            if key:
                self.enqueue_marks.setdefault(key, []).append(time.monotonic())
            return orig_enqueue_chart(frame)

        channel._enqueue_chart = enqueue_chart_wrapped  # type: ignore[method-assign]

    # -- frame builders ----------------------------------------------------
    def command_frame(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        cid = new_uuid7_str()
        return {
            "type": "command",
            "command_id": cid,
            "account_id": ACCOUNT_ID,
            "device_id": self.device_id,
            "operation": {"type": "close_position", "ticket": str(TICKET)},
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "idempotency_key": cid,
        }

    def driver_metrics_delta(self, before: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        """Per-op totals accrued since ``before`` (avg/max over the delta)."""
        after = self.driver.metrics_snapshot()
        out: dict[str, dict[str, float]] = {}
        for op, a in after.items():
            b = before.get(op, {"calls": 0, "total_ms": 0.0, "max_ms": 0.0})
            calls = int(a["calls"] - b["calls"])
            if calls <= 0:
                continue
            total = float(a["total_ms"] - b["total_ms"])
            out[op] = {
                "calls": calls,
                "avg_ms": round(total / calls, 3),
                "max_ms": round(float(a["max_ms"]), 3),
                "total_ms": round(total, 3),
            }
        return out


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------
async def phase_ws_rtt(harness: BenchHarness, n: int = 100) -> dict[str, Any]:
    """Pure WS loopback round-trip: heartbeat send -> heartbeat reply,
    plus the WS handshake cost. No Bridge code on this path — it is the
    transport floor every other phase sits on."""
    url = harness.mock.ws_url
    # NOTE: a distinct probe device-id — the mock routes bench sends by
    # device, and reusing the Bridge's id would hijack its socket slot.
    headers = {"Authorization": "Bearer test-token", "X-NSE-Device-Id": "bench-rtt-probe"}
    handshakes: list[float] = []
    for _ in range(5):
        t0 = time.monotonic()
        async with websockets.connect(url, additional_headers=headers):
            handshakes.append((time.monotonic() - t0) * 1000.0)
    rtts: list[float] = []
    async with websockets.connect(url, additional_headers=headers) as ws:
        for _ in range(n):
            t0 = time.monotonic()
            await ws.send(json.dumps({"type": "heartbeat", "device_id": harness.device_id}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("type") == "heartbeat":
                    break
                # sync_request / anything else: not our echo, keep waiting
            rtts.append((time.monotonic() - t0) * 1000.0)
    return {"handshake_ms": stats_ms(handshakes), "heartbeat_rtt_ms": stats_ms(rtts)}


async def phase_commands(harness: BenchHarness, n: int, label: str) -> dict[str, Any]:
    """(a) close_position end-to-end, sequential, one command at a time.
    Steps are in ms; ``total`` is server-send -> mock ingest."""
    steps: dict[str, list[float]] = {
        "ws_in": [],
        "dispatch": [],
        "mt5_execute": [],
        "enqueue_wait": [],
        "ws_send_ingest": [],
        "total": [],
    }
    failures: list[dict[str, Any]] = []
    before = harness.driver.metrics_snapshot()
    for _ in range(n):
        harness.mt5.reset_position()
        frame = harness.command_frame()
        cid = frame["command_id"]
        t_send = harness.mock.send_json(frame)
        result = await asyncio.to_thread(harness.mock.wait_for_result, cid, 10.0)
        if result is None:
            failures.append({"command_id": cid, "error": "no action.result in 10s"})
            continue
        arrival = harness.mock.state.result_arrivals.get(cid)
        marks = harness.marks.get(cid, {})
        if arrival is None or "recv" not in marks or "exec_start" not in marks or "exec_end" not in marks:
            failures.append({"command_id": cid, "error": "missing step marks", "marks": sorted(marks)})
            continue
        if result.get("status") != "executed":
            failures.append({"command_id": cid, "error": f"status={result.get('status')}", "result": result})
            continue
        enqueued = marks.get("enqueued", marks["exec_end"])
        steps["ws_in"].append((marks["recv"] - t_send) * 1000.0)
        steps["dispatch"].append((marks["exec_start"] - marks["recv"]) * 1000.0)
        steps["mt5_execute"].append((marks["exec_end"] - marks["exec_start"]) * 1000.0)
        steps["enqueue_wait"].append((enqueued - marks["exec_end"]) * 1000.0)
        steps["ws_send_ingest"].append((arrival - enqueued) * 1000.0)
        steps["total"].append((arrival - t_send) * 1000.0)
    return {
        "label": label,
        "mt5_latency_ms_per_call": harness.mt5.latency_ms,
        "steps_ms": {k: stats_ms(v) for k, v in steps.items()},
        "failures": failures,
        "driver_ops": harness.driver_metrics_delta(before),
        "max_worker_queue_depth": harness.driver.max_queue_depth,
    }


async def phase_ticks(harness: BenchHarness, cycles: int = 8) -> dict[str, Any]:
    """(b) one subscription poll cycle: MT5 fetch -> frame build ->
    WS send -> mock ingest. Poll interval 5 s (the §17.1 minimum)."""
    sub_id = new_uuid7_str()
    harness.fetch_latency_s = 0.005  # one realistic IPC hop per fetch
    fetch_mark = len(harness.fetch_spans)
    arrivals_before = len(harness.mock.state.chart_arrivals)
    enqueues_before = len(harness.enqueue_marks.get(sub_id, []))
    harness.mock.send_json(
        {
            "type": "chart.subscribe",
            "subscription_id": sub_id,
            "symbol": SYMBOL,
            "timeframe": "M1",
            "poll_interval_s": 5,
            "include_ticks": True,
        }
    )
    deadline = time.monotonic() + cycles * 5.0 + 30.0
    while time.monotonic() < deadline:
        got = [a for a in harness.mock.state.chart_arrivals[arrivals_before:] if a[1] == sub_id]
        if len(got) >= 2 * cycles:
            break
        await asyncio.sleep(0.2)
    harness.mock.send_json({"type": "chart.unsubscribe", "subscription_id": sub_id})
    harness.fetch_latency_s = harness.fetch_latency_ms / 1000.0
    arrivals = [a for a in harness.mock.state.chart_arrivals[arrivals_before:] if a[1] == sub_id]
    enqueues = harness.enqueue_marks.get(sub_id, [])[enqueues_before:]
    spans = harness.fetch_spans[fetch_mark:]
    steps: dict[str, list[float]] = {"mt5_fetch": [], "build_enqueue": [], "ws_send_ingest": [], "total": []}
    frames = [f for f in harness.mock.state.chart_frames_received if f.get("subscription_id") == sub_id]
    for i in range(min(cycles, len(arrivals) // 2, len(spans) // 2)):
        candles_span = spans[2 * i]
        ticks_span = spans[2 * i + 1]
        fetch_start = candles_span["t0"]
        fetch_end = ticks_span["t1"]
        enq = enqueues[2 * i + 1] if len(enqueues) > 2 * i + 1 else enqueues[2 * i]
        arr = arrivals[2 * i + 1][2]
        steps["mt5_fetch"].append((fetch_end - fetch_start) * 1000.0)
        steps["build_enqueue"].append((enq - fetch_end) * 1000.0)
        steps["ws_send_ingest"].append((arr - enq) * 1000.0)
        steps["total"].append((arr - fetch_start) * 1000.0)
    return {
        "label": f"chart.subscribe {SYMBOL} M1 poll=5s include_ticks (fetch 5ms/hop)",
        "cycles_requested": cycles,
        "cycles_measured": len(steps["total"]),
        "frames_received": len(frames),
        "steps_ms": {k: stats_ms(v) for k, v in steps.items()},
        "fetch_spans_ms": {
            k: stats_ms([s["ms"] for s in spans if s["op"] == k]) for k in ("fetch_candles", "fetch_ticks")
        },
    }


async def phase_chart(harness: BenchHarness, count: int, n: int, label: str) -> dict[str, Any]:
    """(c) chart warm-up: chart.request -> chart.data ingest, sequential."""
    steps: dict[str, list[float]] = {"ws_in": [], "handle_request": [], "deliver": [], "total": []}
    frame_bytes: list[int] = []
    candle_counts: list[int] = []
    failures: list[dict[str, Any]] = []
    before = harness.driver.metrics_snapshot()
    for _ in range(n):
        rid = new_uuid7_str()
        t_send = harness.mock.send_json(
            {"type": "chart.request", "request_id": rid, "symbol": SYMBOL, "timeframe": "M1", "count": count}
        )
        frame = await asyncio.to_thread(harness.mock.wait_for_chart, rid, 10.0)
        if frame is None:
            failures.append({"request_id": rid, "error": "no chart.data in 10s"})
            continue
        arrival = next((t for _, key, t in harness.mock.state.chart_arrivals if key == rid), None)
        marks = harness.marks.get(rid, {})
        enqueues = harness.enqueue_marks.get(rid, [])
        if arrival is None or "chart_start" not in marks or "chart_end" not in marks or not enqueues:
            failures.append({"request_id": rid, "error": "missing step marks"})
            continue
        steps["ws_in"].append((marks["chart_start"] - t_send) * 1000.0)
        steps["handle_request"].append((marks["chart_end"] - marks["chart_start"]) * 1000.0)
        steps["deliver"].append((arrival - enqueues[0]) * 1000.0)
        steps["total"].append((arrival - t_send) * 1000.0)
        frame_bytes.append(len(json.dumps(frame)))
        candle_counts.append(len(frame.get("candles", [])))
    return {
        "label": label,
        "count": count,
        "steps_ms": {k: stats_ms(v) for k, v in steps.items()},
        "frame_bytes": stats_ms([float(b) for b in frame_bytes]) if frame_bytes else {"n": 0},
        "candles_per_frame": candle_counts[:3],
        "failures": failures,
        "driver_ops": harness.driver_metrics_delta(before),
    }


def phase_chart_micro(harness: BenchHarness, count: int = 500, iterations: int = 200) -> dict[str, Any]:
    """In-process cost of ChartService.handle_request + JSON wire
    serialization for a count-500 frame, with the MT5 fetch at 0 ms —
    the Bridge-only processing floor of the warm-up path."""
    assert harness.channel is not None and harness.channel._chart is not None
    chart = harness.channel._chart
    saved = harness.fetch_latency_s
    harness.fetch_latency_s = 0.0
    handle: list[float] = []
    wire: list[float] = []
    sizes: list[int] = []
    try:
        for _ in range(iterations):
            frame = {
                "type": "chart.request",
                "request_id": new_uuid7_str(),
                "symbol": SYMBOL,
                "timeframe": "M1",
                "count": count,
            }
            t0 = time.perf_counter()
            frames = chart.handle_request(frame)
            handle.append((time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
            blob = json.dumps(frames[0])
            wire.append((time.perf_counter() - t0) * 1000.0)
            sizes.append(len(blob))
    finally:
        harness.fetch_latency_s = saved
    return {
        "label": f"in-process handle_request + json.dumps, count={count}, fetch=0ms",
        "handle_request_ms": stats_ms(handle),
        "json_dumps_ms": stats_ms(wire),
        "frame_bytes": stats_ms([float(s) for s in sizes]),
    }


async def phase_contention_dashboard(
    harness: BenchHarness, n: int = 50, *, workers: int = 6, interval_s: float = 0.0, label: str | None = None
) -> dict[str, Any]:
    """(d1) the same close_position loop while the local dashboard is
    busy: workers hammer diagnostics/summary + get_bridge_status —
    the summary path calls refresh_terminal() on the MT5 worker, so
    every dashboard request enqueues one worker job behind which a
    command must queue. ``interval_s`` paces each worker (0 = as fast
    as possible, i.e. a stress test; 1.0 = ~1 req/s, a realistic
    dashboard poll rate)."""
    from httpx import ASGITransport, AsyncClient

    from nse_bridge.api.app import create_app

    assert harness.ctx is not None
    app = create_app(harness.ctx)
    api_key = harness.ctx.settings.local_api_key
    stop = asyncio.Event()
    req_lat: list[float] = []
    req_count = {"n": 0}

    async def worker(client: Any) -> None:
        while not stop.is_set():
            for method, path, payload in (
                ("GET", "/api/v1/diagnostics/summary", None),
                ("POST", "/api/v1/get_bridge_status", {}),
            ):
                t0 = time.monotonic()
                try:
                    resp = await client.request(method, path, json=payload)
                    if resp.status_code == 200:
                        req_count["n"] += 1
                except Exception:  # noqa: BLE001 — a failed hammer request is load, not a bench failure
                    req_count["failed"] = req_count.get("failed", 0) + 1
                req_lat.append((time.monotonic() - t0) * 1000.0)
                if stop.is_set():
                    return
            if interval_s > 0:
                await asyncio.sleep(interval_s)

    transport = ASGITransport(app=app)
    t_start = time.monotonic()
    async with AsyncClient(
        transport=transport, base_url="http://bench.local", headers={"X-API-Key": api_key}
    ) as client:
        tasks = [asyncio.create_task(worker(client)) for _ in range(workers)]
        # Let the hammer reach steady state before measuring.
        await asyncio.sleep(1.0)
        result = await phase_commands(
            harness,
            n,
            label=label or f"close_position under dashboard load ({workers} hammer workers)",
        )
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - t_start
    result["dashboard_load"] = {
        "workers": workers,
        "interval_s": interval_s,
        "hammer_requests": req_count["n"],
        "hammer_failed": req_count.get("failed", 0),
        "hammer_seconds": round(elapsed, 1),
        "hammer_req_per_s": round(req_count["n"] / elapsed, 1) if elapsed > 0 else None,
        "hammer_request_ms": stats_ms(req_lat),
    }
    return result


async def phase_contention_blocked_worker(harness: BenchHarness, n: int = 10) -> dict[str, Any]:
    """(d2) a 300 ms MT5 chart fetch occupies the serialized worker;
    each close_position is sent 10 ms after the fetch starts, so it
    must queue behind the fetch on the worker thread."""
    saved = harness.fetch_latency_s
    harness.fetch_latency_s = 0.300
    steps: dict[str, list[float]] = {
        "ws_in": [],
        "dispatch": [],
        "mt5_execute": [],
        "enqueue_wait": [],
        "ws_send_ingest": [],
        "total": [],
    }
    failures: list[dict[str, Any]] = []
    queue_before = harness.driver.max_queue_depth
    try:
        for _ in range(n):
            harness.mt5.reset_position()
            chart_rid = new_uuid7_str()
            t_chart_send = harness.mock.send_json(
                {"type": "chart.request", "request_id": chart_rid, "symbol": SYMBOL, "timeframe": "M1", "count": 1000}
            )
            await asyncio.sleep(0.01)  # fetch is now running on the worker
            frame = harness.command_frame()
            cid = frame["command_id"]
            t_send = harness.mock.send_json(frame)
            result = await asyncio.to_thread(harness.mock.wait_for_result, cid, 10.0)
            chart_frame = await asyncio.to_thread(harness.mock.wait_for_chart, chart_rid, 10.0)
            chart_arrival = next((t for _, key, t in harness.mock.state.chart_arrivals if key == chart_rid), None)
            if chart_frame is not None and chart_arrival is not None:
                steps.setdefault("chart_fetch_roundtrip", []).append((chart_arrival - t_chart_send) * 1000.0)
            if result is None:
                failures.append({"command_id": cid, "error": "no action.result in 10s"})
                continue
            arrival = harness.mock.state.result_arrivals.get(cid)
            marks = harness.marks.get(cid, {})
            if arrival is None or "recv" not in marks or "exec_start" not in marks or "exec_end" not in marks:
                failures.append({"command_id": cid, "error": "missing step marks"})
                continue
            if result.get("status") != "executed":
                failures.append({"command_id": cid, "error": f"status={result.get('status')}"})
                continue
            enqueued = marks.get("enqueued", marks["exec_end"])
            steps["ws_in"].append((marks["recv"] - t_send) * 1000.0)
            steps["dispatch"].append((marks["exec_start"] - marks["recv"]) * 1000.0)
            steps["mt5_execute"].append((marks["exec_end"] - marks["exec_start"]) * 1000.0)
            steps["enqueue_wait"].append((enqueued - marks["exec_end"]) * 1000.0)
            steps["ws_send_ingest"].append((arrival - enqueued) * 1000.0)
            steps["total"].append((arrival - t_send) * 1000.0)
    finally:
        harness.fetch_latency_s = saved
    return {
        "label": "close_position sent 10ms after a 300ms chart fetch started (same MT5 worker)",
        "chart_fetch_ms": 300.0,
        "steps_ms": {k: stats_ms(v) for k, v in steps.items()},
        "failures": failures,
        "max_worker_queue_depth": harness.driver.max_queue_depth,
        "queue_depth_before": queue_before,
    }


async def phase_contention_queued_chart_flood(
    harness: BenchHarness, n: int = 10, *, floods: int = 6, fetch_ms: float = 100.0
) -> dict[str, Any]:
    """(d3) the P1-3 acceptance phase: ``floods`` chart fetches are
    sent back-to-back so they occupy the serialized worker AND queue
    behind it; each close_position is sent while fetches are still
    queued. On a plain FIFO the close waits behind every queued
    fetch (~``(floods-1) * fetch_ms``); with the priority lane it
    jumps the queued fetches and waits only for the one fetch
    already running on the worker. The queued depth at close-send is
    recorded, so a run that never actually queued work cannot claim
    a win. Chart frames are drained after each close so floods never
    pile up across iterations."""
    saved = harness.fetch_latency_s
    harness.fetch_latency_s = fetch_ms / 1000.0
    steps: dict[str, list[float]] = {
        "ws_in": [],
        "dispatch": [],
        "mt5_execute": [],
        "enqueue_wait": [],
        "ws_send_ingest": [],
        "total": [],
    }
    queued_depths: list[int] = []
    failures: list[dict[str, Any]] = []
    try:
        for _ in range(n):
            harness.mt5.reset_position()
            rids = [new_uuid7_str() for _ in range(floods)]
            for rid in rids:
                harness.mock.send_json(
                    {"type": "chart.request", "request_id": rid, "symbol": SYMBOL, "timeframe": "M1", "count": 1000}
                )
            # Wait until the flood is actually queued on the worker
            # (one fetch in flight, the rest waiting) — or proceed
            # after 2 s and record the smaller depth honestly.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and harness.driver.queued_jobs < floods - 1:
                await asyncio.sleep(0.005)
            queued_depths.append(harness.driver.queued_jobs)
            frame = harness.command_frame()
            cid = frame["command_id"]
            t_send = harness.mock.send_json(frame)
            result = await asyncio.to_thread(harness.mock.wait_for_result, cid, 10.0)
            # Drain the flood's chart frames before the next round.
            for rid in rids:
                await asyncio.to_thread(harness.mock.wait_for_chart, rid, 10.0)
            if result is None:
                failures.append({"command_id": cid, "error": "no action.result in 10s"})
                continue
            arrival = harness.mock.state.result_arrivals.get(cid)
            marks = harness.marks.get(cid, {})
            if arrival is None or "recv" not in marks or "exec_start" not in marks or "exec_end" not in marks:
                failures.append({"command_id": cid, "error": "missing step marks"})
                continue
            if result.get("status") != "executed":
                failures.append({"command_id": cid, "error": f"status={result.get('status')}"})
                continue
            enqueued = marks.get("enqueued", marks["exec_end"])
            steps["ws_in"].append((marks["recv"] - t_send) * 1000.0)
            steps["dispatch"].append((marks["exec_start"] - marks["recv"]) * 1000.0)
            steps["mt5_execute"].append((marks["exec_end"] - marks["exec_start"]) * 1000.0)
            steps["enqueue_wait"].append((enqueued - marks["exec_end"]) * 1000.0)
            steps["ws_send_ingest"].append((arrival - enqueued) * 1000.0)
            steps["total"].append((arrival - t_send) * 1000.0)
    finally:
        harness.fetch_latency_s = saved
    return {
        "label": (
            f"close_position sent while {floods} x {fetch_ms:.0f}ms chart fetches are queued "
            "(same MT5 worker; P1-3 acceptance phase)"
        ),
        "chart_fetches_per_round": floods,
        "chart_fetch_ms": fetch_ms,
        "queued_chart_jobs_at_close_send": stats_ms([float(d) for d in queued_depths]) if queued_depths else {"n": 0},
        "steps_ms": {k: stats_ms(v) for k, v in steps.items()},
        "failures": failures,
        "max_worker_queue_depth": harness.driver.max_queue_depth,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def environment() -> dict[str, Any]:
    mem_total_kb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
                break
    except OSError:
        pass
    load = None
    try:
        load = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        pass
    return {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_avg_1_5_15": load,
        "mem_total_mib": round(mem_total_kb / 1024) if mem_total_kb else None,
        "transport": "local loopback 127.0.0.1 (mock server + fake MT5, same VM)",
        "note": "Loopback only. The user-PC -> Germany-server network leg is UNMEASURED (see verdict).",
    }


def print_summary(report: dict[str, Any]) -> None:
    def row(name: str, s: dict[str, Any]) -> str:
        return f"  {name:<28} n={s['n']:<4} p50={s['p50_ms']:>8.3f} ms  p95={s['p95_ms']:>8.3f} ms  mean={s['mean_ms']:>8.3f}  max={s['max_ms']:>8.3f}"

    print("\n=== Bridge v1.4.0 realtime latency baseline (loopback, mock MT5 + mock server) ===")
    rtt = report["ws_rtt"]
    print("WS loopback floor:")
    print(row("handshake", rtt["handshake_ms"]))
    print(row("heartbeat RTT", rtt["heartbeat_rtt_ms"]))
    for key in ("commands_mt5_0ms", "commands_mt5_5ms"):
        phase = report[key]
        print(f"\n{phase['label']}:")
        for step, s in phase["steps_ms"].items():
            print(row(step, s))
        if phase["failures"]:
            print(f"  FAILURES: {len(phase['failures'])}")
    ticks = report["ticks"]
    print(f"\n{ticks['label']}: cycles={ticks['cycles_measured']}/{ticks['cycles_requested']}")
    for step, s in ticks["steps_ms"].items():
        print(row(step, s))
    for key in ("chart_500", "chart_1000"):
        phase = report[key]
        print(f"\n{phase['label']}:")
        for step, s in phase["steps_ms"].items():
            print(row(step, s))
        print(f"  frame_bytes p50={phase['frame_bytes']['p50_ms']:.0f}")
    micro = report["chart_micro"]
    print(f"\n{micro['label']}:")
    print(row("handle_request", micro["handle_request_ms"]))
    print(row("json.dumps", micro["json_dumps_ms"]))
    d1 = report.get("contention_dashboard")
    if d1 is not None:
        load = d1["dashboard_load"]
        print(
            f"\n{d1['label']}: hammer_requests={load['hammer_requests']} "
            f"({load['hammer_req_per_s']} req/s, hammer p50={load['hammer_request_ms']['p50_ms']:.1f} ms)"
        )
        for step, s in d1["steps_ms"].items():
            print(row(step, s))
    d1r = report.get("contention_dashboard_realistic")
    if d1r is not None:
        load = d1r["dashboard_load"]
        print(
            f"\n{d1r['label']}: hammer_requests={load['hammer_requests']} "
            f"({load['hammer_req_per_s']} req/s, hammer p50={load['hammer_request_ms']['p50_ms']:.1f} ms)"
        )
        for step, s in d1r["steps_ms"].items():
            print(row(step, s))
    d2 = report.get("contention_blocked_worker")
    if d2 is not None:
        print(f"\n{d2['label']}:")
        for step, s in d2["steps_ms"].items():
            print(row(step, s))
    d3 = report.get("contention_queued_chart_flood")
    if d3 is not None:
        q = d3["queued_chart_jobs_at_close_send"]
        print(
            f"\n{d3['label']}: queued_jobs_at_send p50={q['p50_ms']:.0f} "
            f"(n={q['n']}, max queue depth {d3['max_worker_queue_depth']})"
        )
        for step, s in d3["steps_ms"].items():
            print(row(step, s))
        if d3["failures"]:
            print(f"  FAILURES: {len(d3['failures'])}")
    print("\nVerdict vs <100ms target: see docs/PERFORMANCE-v14-baseline.md (WAN leg UNMEASURED).")


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    harness = BenchHarness(samples=args.samples, mt5_latency_ms=0.0, fetch_latency_ms=args.fetch_latency_ms)
    report: dict[str, Any] = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(),
            "environment": environment(),
            "params": {
                "command_samples": args.samples,
                "tick_cycles": args.tick_cycles,
                "chart_samples": args.chart_samples,
                "mt5_latency_profiles_ms_per_call": [0.0, args.mt5_latency_ms],
                "chart_fetch_latency_ms": args.fetch_latency_ms,
            },
            "method": (
                "Real ServerChannel + CommandExecutor + ChartService; mock Platform server "
                "(scripts/mock_server.py) on loopback; FakeMt5 behind a serialized "
                "single-worker thread mirroring Mt5Driver.run. No production, no live MT5."
            ),
        }
    }
    try:
        await harness.start()
        report["ws_rtt"] = await phase_ws_rtt(harness)
        harness.mt5.latency_ms = 0.0
        report["commands_mt5_0ms"] = await phase_commands(
            harness, args.samples, "close_position, MT5 IPC 0ms/call (Bridge floor)"
        )
        harness.mt5.latency_ms = args.mt5_latency_ms
        report["commands_mt5_5ms"] = await phase_commands(
            harness, args.samples, f"close_position, MT5 IPC {args.mt5_latency_ms}ms/call"
        )
        report["ticks"] = await phase_ticks(harness, cycles=args.tick_cycles)
        report["chart_500"] = await phase_chart(harness, 500, args.chart_samples, "chart.request count=500")
        report["chart_1000"] = await phase_chart(harness, 1000, 10, "chart.request count=1000")
        report["chart_micro"] = phase_chart_micro(harness, count=500, iterations=200)
        if not args.no_contention:
            report["contention_dashboard"] = await phase_contention_dashboard(harness, n=50)
            report["contention_dashboard_realistic"] = await phase_contention_dashboard(
                harness,
                n=50,
                workers=1,
                interval_s=1.0,
                label="close_position under realistic dashboard load (1 worker, ~1 req/s)",
            )
            report["contention_blocked_worker"] = await phase_contention_blocked_worker(harness, n=10)
            report["contention_queued_chart_flood"] = await phase_contention_queued_chart_flood(harness, n=10)
    finally:
        await harness.stop()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100, help="close_position samples per profile (default 100)")
    parser.add_argument("--tick-cycles", type=int, default=8, help="subscription poll cycles to measure (default 8)")
    parser.add_argument("--chart-samples", type=int, default=20, help="chart.request samples per count (default 20)")
    parser.add_argument(
        "--mt5-latency-ms", type=float, default=5.0, help="simulated MT5 IPC latency per call, profile B (default 5)"
    )
    parser.add_argument(
        "--fetch-latency-ms", type=float, default=20.0, help="simulated chart-fetch IPC latency (default 20)"
    )
    parser.add_argument("--no-contention", action="store_true", help="skip the contention phases")
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON report here")
    args = parser.parse_args(argv)

    report = asyncio.run(main_async(args))
    text = json.dumps(report, indent=2)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"[BENCH] JSON report written to {args.out}")
    print_summary(report)
    short = 0
    for key in ("commands_mt5_0ms", "commands_mt5_5ms", "chart_500"):
        phase = report.get(key, {})
        if phase.get("failures"):
            short += len(phase["failures"])
    if report.get("ticks", {}).get("cycles_measured", 0) < args.tick_cycles:
        short += 1
    return 2 if short else 0


if __name__ == "__main__":
    raise SystemExit(main())
