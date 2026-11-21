"""Reproducible local-processing performance benchmark (mission §B1/§B3).

Measures the Bridge's LOCAL API + serialization paths in-process with a
fake MT5 driver — deliberately EXCLUDING MT5 IPC latency and network
latency, which are reported separately by the driver's own per-op
metrics and never presented as local processing time.

Sections:
  api_health        GET /health (light probe)  — liveness path
  api_status        POST /api/v1/get_bridge_status
  api_commands      GET  /api/v1/commands (91-command registry listing)
  api_diag_summary  GET  /api/v1/diagnostics/summary (heaviest mgmt route)
  sync_positions    SyncEngine.positions_items() for a 500-position page
  json_batch        JSON serialization of that batch (the §3.3 wire shape)

Methodology: warmup pass, then N=400 samples per endpoint through the
ASGI transport (no socket), percentiles p50/p95/p99 + mean. The batch
sections run 200 iterations. Output is a JSON report on stdout so CI
or docs can embed it verbatim.

Usage:
  python scripts/bench_local_api.py [--samples 400] [--out report.json]

These numbers are environment-specific (see docs/PERFORMANCE.md for the
recorded baselines and their hardware); compare only same-machine runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


class _FakeDriver:
    """Minimal driver stand-in: answers the state calls without MT5 IPC."""

    def __init__(self) -> None:
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def connection_state_dict(self) -> dict[str, Any]:
        return {
            "state": "CONNECTED",
            "detail": "",
            "terminal_version": "5.0.0 (bench)",
            "login": 10011755849,
            "server": "MetaQuotes-Bench",
            "last_error": None,
            "diagnostic_code": None,
        }

    def refresh_terminal(self) -> dict[str, Any]:
        return {"connected": True, "name": "MetaTrader 5", "trade_allowed": True, "login": 10011755849}

    def metrics_dict(self) -> dict[str, Any]:
        return {"bench.op": {"calls": 1, "failures": 0, "avg_ms": 0.5, "max_ms": 0.5, "last_ms": 0.5}}

    def ensure_connected(self) -> None:
        return None


def _build_app():
    from nse_bridge.api.app import create_app
    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext

    settings = BridgeSettings(data_dir=Path(__import__("tempfile").gettempdir()) / "nse-bench")
    ctx = BridgeContext(settings=settings, driver=_FakeDriver())  # type: ignore[arg-type]
    return create_app(ctx)


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    ordered = sorted(samples_ms)
    n = len(ordered)

    def pct(p: float) -> float:
        idx = min(n - 1, round(p / 100.0 * (n - 1)))
        return round(ordered[idx], 3)

    return {
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
        "mean_ms": round(statistics.fmean(ordered), 3),
        "max_ms": round(ordered[-1], 3),
    }


async def _bench_http(client: Any, method: str, path: str, samples: int) -> dict[str, float]:
    timings: list[float] = []
    for _ in range(samples):
        t0 = time.perf_counter()
        response = await client.request(method, path)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if response.status_code != 200:
            raise RuntimeError(f"{method} {path} answered {response.status_code}: {response.text[:200]}")
        timings.append(elapsed)
    return _percentiles(timings)


def _bench_sync_positions(iterations: int) -> dict[str, float]:
    from nse_bridge.server.sync import SyncEngine

    timings: list[float] = []
    positions = [
        {
            "ticket": 100000 + i,
            "symbol": "EUR/USD" if i % 2 else "GBP/USD",
            "side": "buy" if i % 3 else "sell",
            "volume": 0.01 + (i % 5) * 0.01,
            "open_price": 1.0800 + i * 1e-5,
            "current_price": 1.0801 + i * 1e-5,
            "sl": 1.0750,
            "tp": 1.0900,
            "profit": round((i % 17) * 0.13 - 1.0, 2),
            "swap": 0.0,
            "time": "2026-11-21T09:00:00Z",
            "comment": "bench",
            "magic": 0,
        }
        for i in range(500)
    ]
    orders: list[dict[str, Any]] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        items = SyncEngine.positions_items(positions, orders)
        timings.append((time.perf_counter() - t0) * 1000.0)
    result = _percentiles(timings)
    result["items_out"] = len(items["positions"]) + len(items["orders"])  # type: ignore[union-attr,index]
    return result


def _bench_json_batch(iterations: int) -> dict[str, float]:
    from nse_bridge.server.sync import SyncEngine, build_batch

    positions = [{"ticket": 100000 + i, "symbol": "EUR/USD", "volume": 0.01} for i in range(500)]
    items = SyncEngine.positions_items(positions, [])
    # The REAL §3.3 wire shape (flat batch envelope, not an ad-hoc dict).
    batch = build_batch("positions", items)
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        json.dumps(batch)
        timings.append((time.perf_counter() - t0) * 1000.0)
    result = _percentiles(timings)
    result["wire_bytes"] = len(json.dumps(batch))
    return result


async def main_async(samples: int) -> dict[str, Any]:
    from httpx import ASGITransport, AsyncClient

    app = _build_app()
    # The local API key is mandatory (create_app ensured one on the
    # context's settings) — the bench authenticates like any client.
    api_key = app.state.ctx.settings.local_api_key
    report: dict[str, Any] = {"meta": {"samples_per_endpoint": samples}}
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://bench.local", headers={"X-API-Key": api_key}
    ) as client:
        # Warmup (JIT-ish caches, route tables, pydantic model reuse).
        for path in ("/health", "/api/v1/commands"):
            await client.request("GET", path)
        await client.request("POST", "/api/v1/get_bridge_status")
        await client.request("GET", "/api/v1/diagnostics/summary")

        report["api_health"] = await _bench_http(client, "GET", "/health", samples)
        report["api_status"] = await _bench_http(client, "POST", "/api/v1/get_bridge_status", samples)
        report["api_commands"] = await _bench_http(client, "GET", "/api/v1/commands", samples)
        report["api_diag_summary"] = await _bench_http(client, "GET", "/api/v1/diagnostics/summary", samples)

    report["sync_positions_500"] = _bench_sync_positions(200)
    report["json_batch_500"] = _bench_json_batch(200)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--out", type=Path, default=None, help="also write the JSON report here")
    args = parser.parse_args(argv)

    report = asyncio.run(main_async(args.samples))
    text = json.dumps(report, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
