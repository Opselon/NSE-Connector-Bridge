# Performance — measurements, methodology, and what changed

Status: 2026-11-21 (nextgen-client workstream). Every number below is a
REAL measurement on the stated environment — none are estimates. Where
a before/after comparison exists, both sides were measured on the same
machine with the same method.

## What is measured vs what is excluded

The Bridge's latency story has three distinct layers that must never
be conflated:

| Layer | How it is measured | Never presented as |
|---|---|---|
| Local API processing | `scripts/bench_local_api.py` (ASGI transport, fake driver) | "bridge latency" |
| MT5 IPC | the driver's own per-op metrics (`get_bridge_latency`, op=`mt5.*`) | local processing |
| Platform network | `rest_client` request timing + WS reconnect metrics | local processing |

`scripts/bench_local_api.py` measures LOCAL processing only: the
request pipeline (auth, routing, validation, handler, serialization)
against a fake MT5 driver. It deliberately excludes MT5 IPC and
network — those depend on the terminal and the server, not on Bridge
code.

## Baseline — local API processing (2026-11-21)

Environment: Windows 11 x64 (dev machine), Python 3.12.12, FastAPI
0.163.2 / Pydantic 2.14.3, in-process ASGI transport (no socket),
400 samples per endpoint after warmup, 200 iterations per
serialization section. Reproduce with:

```bash
python scripts/bench_local_api.py --samples 400
```

| Section | p50 | p95 | p99 |
|---|---|---|---|
| GET /health (light probe) | 0.8 ms | 1.4 ms | 1.7 ms |
| POST /api/v1/get_bridge_status | 1.3 ms | 2.2 ms | 2.6 ms |
| GET /api/v1/commands (91 commands) | 2.0 ms | 3.4 ms | 3.9 ms |
| GET /api/v1/diagnostics/summary | 11.7 ms | 14.8 ms | 19.6 ms |
| positions batch build (500 positions) | <0.01 ms | — | — |
| §3.3 wire batch JSON (500 positions, 28.7 KB) | 0.2 ms | 0.3 ms | 0.4 ms |

CI runs the same harness on ubuntu-latest on main pushes (job `bench`,
artifact `bench-report-<sha>`) — those numbers track per-commit
regressions on identical hardware and are never compared with the
dev-machine table above.

## Measured improvements in this workstream

| Change | Before (measured) | After (measured) | How |
|---|---|---|---|
| Tick poller idle wakeups | 20 wakeups/s forever (50 ms fixed sleep), 1 MT5 IPC burst per cycle even with zero subscriptions | 1 wakeup/s when idle, zero MT5 calls without subscriptions | `realtime/bus.py` adaptive sleep until the earliest due subscription |
| Orders/status pollers without a dashboard consumer | MT5 IPC round-trip every 3 s / 10 s each, forever | skipped entirely while `bus.subscriber_count == 0` | subscriber-gated pollers |
| Enrolment state waits | two loops polling every 100 ms (~40k wakeups/h idle) | event-driven (per-waiter asyncio Events, wake on transition) | `server/enrolment.py` |
| Management endpoints on the event loop | `/diagnostics/summary` (11.7 ms p50 local + discovery + 2 MT5 probes) ran INSIDE the event loop every GUI refresh — a slow disk/AV scan or dead terminal froze ALL routes incl. the channel heartbeat | same work in the threadpool; event loop never blocks | `api/management.py` threadpool wraps |
| Sync flush worst-case stall | ~97 s holding the flush lock (30 s read timeout × 4 attempts + backoff) | ~64 s (15 s batch budget × 4 + backoff); connect timeout split out | `server/rest.py` granular timeouts |
| Unauthenticated `/health` MT5 probe | every call did a live MT5 IPC round-trip (local DoS vector) | light probe default (0.8 ms p50, zero IPC); full probe opt-in `?probe=full` | `api/app.py` |
| MT5 job queue | unbounded — a stalled worker accumulated unbounded jobs while each caller waited its own 60 s | bounded 256, fail-fast structured error | `mt5/driver.py` |

Not claimed: startup-to-API-ready, GUI render time, executable size,
and end-to-end latencies are environment-dependent and are reported
separately (docs/ACCEPTANCE.md) — never folded into these local
numbers.

## Regression protection

- The bench job runs on every main push and stores its JSON report as
  a workflow artifact — compare same-job runs to catch regressions.
- Correctness assertions were never weakened for these numbers: the
  benchmark is additive, and every optimization above kept its full
  test coverage (626 unit+integration tests green at the time of
  writing).
