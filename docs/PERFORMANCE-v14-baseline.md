# Bridge v1.4.0 — realtime latency baseline (measured, loopback)

Measurement-only baseline for the v1.4.0 upgrade brief. No product code
was changed to produce these numbers. The harness is
`scripts/bench_realtime.py`; the raw JSON reports for runs 2 and 3 are
`docs/PERFORMANCE-v14-baseline-run2.json` and
`docs/PERFORMANCE-v14-baseline-run3.json`.

## Method (what actually ran)

- **Real code on the measured path:** the production `ServerChannel`,
  `CommandExecutor`, `CommandValidator` and `ChartService` run
  unmodified. Instrumentation is runtime wrappers installed by the
  bench script only (per-step `time.monotonic()` marks).
- **Fakes:** the in-repo mock Platform server (`scripts/mock_server.py`,
  extended minimally with per-device send-on-demand + arrival
  timestamps) on `127.0.0.1`, and a `FakeMt5` terminal behind a
  serialized single-worker thread that mirrors `Mt5Driver.run`
  (queue-wait included in recorded op durations, bounded queue,
  per-op metrics).
- **Measured op:** (a) `close_position` (the v1 trading op) end to end;
  (b) `chart.subscribe` poll cycles (`chart.tick` ingest); (c)
  `chart.request` warm-up, count 500 and 1000; (d) contention.
- **MT5 IPC latency is a parameter**, not a claim: profile A = 0 ms/call
  (the Bridge-only floor); profile B = 5 ms/call (a realistic local
  IPC hop). The fake terminal sleeps that long per call. Under load the
  sleep overshoots — the per-op table below shows the *observed*
  per-call cost, which is what the totals are built from.
- **No production, no live MT5, no real credentials.** Mock token
  `test-token`, mock account `12345678901`, one synthetic EURUSD
  position per command (re-opened between samples).

## Environment

- Host: `htch-runtime`, Linux 7.0.0-44-generic, Python 3.12.3, 4 vCPUs
  visible / 1.5 shared CPUs for this cell, ~7.9 GiB RAM.
- Transport: local loopback `127.0.0.1` only. **The user-PC ->
  Germany-server network leg is UNMEASURED** (see verdict).
- Ambient load varied a lot between runs, and the numbers move with
  it — that variance is itself a finding, not noise to hide:
  - Run 1: load average not recorded (quietest run; figures below are
    from the console transcript — its JSON was overwritten by run 2
    before being archived).
  - Run 2: load average 6.66 / 5.33 / 3.82 at start.
  - Run 3: load average 10.30 / 7.69 / 4.93 at start.
- Reproduce: `python scripts/bench_realtime.py [--out report.json]`
  (needs the bridge venv; ~2 min).

## Results

All times in milliseconds. `total` is server-send -> mock ingest
(for commands) or fetch-start -> mock ingest (for ticks). `N` is the
sample count. Zero failures in every phase of runs 2 and 3 (every
command returned `status=executed`; every chart frame arrived).

### WS loopback floor (no Bridge code on this path)

| Step | Run 1 (N) | Run 2 (N) | Run 3 (N) |
| --- | --- | --- | --- |
| WS handshake p50 / p95 | 1.50 / 10.30 (5) | 1.84 / 5.82 (5) | 3.28 / 7.98 (5) |
| Heartbeat RTT p50 / p95 | 0.22 / 0.66 (100) | 0.15 / 0.93 (100) | 0.14 / 1.19 (100) |

The loopback transport itself is sub-millisecond at p50. Everything
above that is Bridge code, terminal IPC, or scheduling.

### (a) WS command receive -> MT5 submit -> confirm (`close_position`)

| Step (p50 / p95) | Run 1, IPC 0 ms (N=100) | Run 2, IPC 0 ms (N=100) | Run 3, IPC 0 ms (N=100) |
| --- | --- | --- | --- |
| ws_in (server send -> bridge receive) | 0.61 / 2.37 | 1.44 / 11.93 | 2.15 / 51.64 |
| dispatch (receive -> executor start) | 0.21 / 0.99 | 0.35 / 6.00 | 0.40 / 8.09 |
| mt5_execute (validate + MT5 calls + result build) | 0.76 / 3.14 | 3.17 / 53.33 | 1.90 / 59.90 |
| enqueue_wait | 0.10 / 0.39 | 0.14 / 1.90 | 0.15 / 2.46 |
| ws_send_ingest (enqueue -> mock receipt) | 0.46 / 1.41 | 0.61 / 2.05 | 0.74 / 5.85 |
| **total** | **2.43 / 7.72** | **7.96 / 64.04** | **8.76 / 76.10** |

| Step (p50 / p95) | Run 1, IPC 5 ms (N=100) | Run 2, IPC 5 ms (N=100) | Run 3, IPC 5 ms (N=100) |
| --- | --- | --- | --- |
| ws_in | 0.70 / 8.33 | 1.83 / 54.80 | 1.96 / 50.30 |
| dispatch | 0.24 / 2.87 | 0.36 / 4.47 | 0.40 / 7.38 |
| mt5_execute | 27.53 / 58.76 | 74.45 / 95.11 | 73.65 / 87.86 |
| enqueue_wait | 0.18 / 1.29 | 0.24 / 4.33 | 0.24 / 2.63 |
| ws_send_ingest | 0.59 / 4.12 | 1.02 / 14.25 | 0.88 / 9.66 |
| **total** | **29.72 / 71.48** | **85.35 / 111.21** | **83.47 / 103.33** |

Reading: with zero IPC latency the Bridge itself is a few ms end to
end. With 5 ms/call simulated IPC the close path is ~30 ms p50 when
the VM is quiet and ~83–85 ms p50 when it is loaded, with p95 over
100 ms in both loaded runs. The Bridge's own steps (ws_in, dispatch,
enqueue, send) stay in the 0–2 ms range at p50 in every run; the
growth is all in `mt5_execute` plus scheduling tails on every step.

Per-op MT5 breakdown for the 5 ms/call profile (avg / max per call,
100 commands = 100 calls per op, except where noted):

| MT5 op (close path, in order) | Run 2 avg / max | Run 3 avg / max |
| --- | --- | --- |
| positions_get (re-read position) | 12.14 / 67.09 | 11.57 / 63.32 |
| symbol_info_tick (fresh close price) | 8.99 / 65.25 | 10.54 / 69.58 |
| symbol_info (filling mode) | 11.33 / 64.04 | 9.89 / 67.94 |
| mt5_constants (action/type constants; local read, no terminal IPC) | 0.73 / 22.05 | 0.41 / 9.60 |
| order_check (pre-send validation) | 13.87 / 67.85 | 13.96 / 65.08 |
| order_send_close (the submit) | 12.33 / 68.20 | 10.35 / 77.13 |

Configured 5 ms/call, observed ~9–14 ms/call on this loaded VM
(`time.sleep` overshoot under CPU pressure) — so the "5 ms" profile
is effectively a ~10 ms/call profile here, and single calls spike to
~65–77 ms. The close path makes **5 sequential terminal IPC calls**
(order_check and order_send included); that sequence, not Bridge
code, is what the total is made of. Real MT5 IPC on the user's PC
will differ — measure there before treating any absolute number as
the production figure.

### (b) MT5 tick -> Bridge processing -> platform ingest

Subscription `EURUSD M1, poll 5 s, include_ticks`, 8 poll cycles
(fetch 5 ms/hop). One cycle = fetch 100 candles + 100 ticks, build
`chart.data` + `chart.tick`, WS-send both, mock ingest.

| Step (p50 / p95) | Run 1 (N=8) | Run 2 (N=8) | Run 3 (N=8) |
| --- | --- | --- | --- |
| mt5_fetch (both fetches) | 13.07 / 16.88 | 28.19 / 83.84 | 20.17 / 67.98 |
| build_enqueue (frame build + queue) | 0.12 / 0.21 | 0.27 / 1.34 | 0.24 / 1.14 |
| ws_send_ingest | 0.79 / 1.08 | 1.38 / 4.58 | 1.11 / 55.67 |
| **total (fetch start -> last frame ingest)** | **14.25 / 17.75** | **29.28 / 87.80** | **36.69 / 69.31** |

Frame building + enqueue + send is sub-millisecond at p50 in all
runs. The tick path is dominated by the two MT5 fetches (same
per-call cost as the command path) and by scheduling tails under
load. Note N=8 is small; the p95 here is indicative, not precise.

### (c) Chart warm-up: `chart.request` per symbol/timeframe

Fetch latency 20 ms for the single `copy_rates` IPC hop; frame sizes
67,049 bytes (count 500) and 133,927 bytes (count 1000).

| Step (p50 / p95) | Run 1, count 500 (N=20) | Run 2, count 500 (N=20) | Run 3, count 500 (N=20) |
| --- | --- | --- | --- |
| ws_in | 0.78 / 3.75 | 2.80 / 6.12 | 4.54 / 62.69 |
| handle_request (fetch + frame build) | 28.89 / 40.21 | 78.99 / 95.24 | 96.89 / 105.84 |
| deliver (enqueue -> mock receipt) | 4.10 / 12.07 | 8.52 / 12.39 | 10.27 / 31.72 |
| **total** | **34.16 / 64.11** | **91.34 / 116.25** | **115.83 / 182.44** |

| count 1000 total (p50 / p95) | Run 1 (N=10) | Run 2 (N=10) | Run 3 (N=10) |
| --- | --- | --- | --- |
| total | 43.44 / 70.44 | 98.85 / 173.64 | 177.71 / 237.50 |

In-process micro-bench (same `ChartService.handle_request` +
`json.dumps`, fetch forced to 0 ms, N=200, count 500):

| Step (p50 / p95) | Run 1 | Run 2 | Run 3 |
| --- | --- | --- | --- |
| handle_request | 10.42 / 56.07 | 20.11 / 81.53 | 41.09 / 89.54 |
| json.dumps (67 KB frame) | 1.15 / 4.94 | 1.17 / 6.23 | 1.18 / 5.14 |

Caveat: the fake fetcher builds the 500 candles synthetically
(`strftime` per bar), so the micro-bench's `handle_request` includes
that generation. The real path does the same shape of work per bar
(`broker_time_str` + dict build) but the absolute number is an
approximation — the point that survives is that per-bar Python-side
shaping for 500 bars is 10–40 ms at p50 on this VM, i.e. the same
order as the IPC fetch itself, and `json.dumps` (~1 ms) is not the
cost. Warm-up is not on the per-command realtime path, but it shares
the worker and the receive loop with it (see hot spots 1–2).

### (d) Contention: does a busy dashboard or a blocked worker delay the realtime path?

Yes — measured, twice, in both directions. All rows are the same
`close_position` loop (N=50) with the load in the label.

**D1 — dashboard hammer.** Workers hit `GET /api/v1/diagnostics/summary`
+ `POST /api/v1/get_bridge_status` through the real FastAPI app. The
summary path calls `refresh_terminal()`, i.e. one `terminal_info`
job on the MT5 worker per request.

| Load | Run 2 total p50 / p95 | Run 3 total p50 / p95 |
| --- | --- | --- |
| Idle (IPC 5 ms profile, N=100) | 85.35 / 111.21 | 83.47 / 103.33 |
| Stress: 6 workers, as fast as possible (58.1 req/s run 2; 40.9 req/s run 3; 1,312 / 1,144 `terminal_info` jobs) | 423.00 / 610.54 | 505.67 / 706.35 |
| Realistic: 1 worker, ~1 req/s (1.8 req/s; 8 / 12 `terminal_info` jobs) | 79.00 / 95.42 | 93.42 / 121.28 |

Under the stress hammer the command's `mt5_execute` step alone is
p50 401 ms (run 2) / 478 ms (run 3) — the commands queue behind the
flood of `terminal_info` jobs on the single worker. At a realistic
dashboard poll rate (~1–2 req/s) the p50 is indistinguishable from
idle (79–93 ms vs 83–85 ms); only the p95 moves. So the mechanism is
real and the magnitude scales with caller rate: a dashboard that
polls diagnostics once a second is fine; anything that hammers the
local API (or any high-rate `driver.run` caller) delays trading
linearly with queue depth.

**D2 — blocked MT5 worker (300 ms chart fetch).** A `chart.request`
count=1000 with a 300 ms fetch is sent; the `close_position` is sent
10 ms later. N=10 per run.

| Step (p50 / p95) | Run 2 | Run 3 |
| --- | --- | --- |
| ws_in (server send -> bridge receive) | 331.49 / 370.47 | 367.10 / 409.71 |
| dispatch | 13.79 / 81.35 | 14.70 / 99.26 |
| mt5_execute | 35.18 / 86.12 | 87.13 / 96.71 |
| **total** | **423.68 / 493.02** | **480.13 / 587.05** |
| chart fetch round-trip (for reference) | 396.16 / 458.82 | 404.20 / 529.99 |

The 300 ms does **not** land in `mt5_execute` — it lands in
`ws_in`. The command waits ~330–367 ms *before the Bridge even
starts on it* (see hot spot 1).

## Top 3 hot spots (with evidence)

1. **The WS receive loop awaits `chart.request` inline — a slow chart
   fetch blocks ALL incoming frames.** `ServerChannel._handle_server_message`
   does `await self._handle_chart_request(msg)` directly in the receive
   loop (commands, by contrast, are `asyncio.create_task`ed). While a
   chart fetch runs, the loop cannot receive the next command — it
   sits in the socket buffer. Evidence: D2, both runs — `ws_in` p50
   331 ms / 367 ms for a 300 ms fetch, while the command's own
   `mt5_execute` stays at 35 ms / 87 ms. Fix direction (not done
   here): handle chart requests on a task like commands, or bound the
   fetch so the receive loop never waits on MT5.
2. **One serialized MT5 worker FIFO is shared by everything.**
   Commands, chart fetches, sync, and every dashboard
   `refresh_terminal()` probe all enqueue on the same worker thread
   (`Mt5Driver.run` / `nse_bridge/mt5/driver.py`). Any slow or
   high-rate caller delays trading by its queue depth. Evidence: D1
   stress — 1,312 `terminal_info` jobs in ~24 s drive command
   `mt5_execute` p50 to 401 ms (run 2) / 478 ms (run 3); D2's 300 ms
   fetch is the same mechanism from the chart side. Fix direction:
   priority for command jobs over probes/fetches, or a separate lane
   for trading submits; at minimum, stop the dashboard from probing
   the terminal on every poll (cache `terminal_info`).
3. **The close path is 5 sequential terminal IPC calls.**
   `positions_get` -> `symbol_info_tick` -> `symbol_info` ->
   `order_check` -> `order_send` (plus a cheap local constants read).
   At ~10–14 ms effective per call under load that sequence alone is
   50–70 ms, i.e. most of the end-to-end total; single calls spike to
   ~65–77 ms. Evidence: per-op table above; `mt5_execute` p50 74 ms
   vs Bridge-only steps at ~1 ms. Fix direction: cut round-trips on
   the submit path (cache the filling mode per symbol, skip the
   pre-close `positions_get` when the ticket is already validated, or
   fold the tick read into the check) — each removed IPC hop is ~10 ms
   off the total at this latency.

What is *not* a hot spot: the Bridge's own WS/JSON/dispatch code
(`ws_in` ~1–2 ms, `dispatch` ~0.4 ms, `enqueue` ~0.2 ms,
`ws_send_ingest` ~1 ms, `json.dumps` of a 67 KB frame ~1 ms, all at
p50). The 0 ms IPC profile proves the Bridge floor is 2–9 ms
end-to-end; the budget is spent in terminal IPC, queueing, and
scheduling — not in Bridge Python.

## Verdict vs the <100 ms target

**Not supported by these measurements — on loopback, before any
network is even involved.**

- Quiet VM (run 1): close total p50 29.7 ms, p95 71.5 ms — under
  100 ms at p95.
- Loaded VM (runs 2–3, load 6.7–10.3 on this shared host): close
  total p50 83–85 ms, **p95 103–111 ms** — already over the target at
  p95 on loopback, with a 5 ms/call IPC assumption that itself
  measured ~10 ms/call effective.
- Tick ingest is inside budget at p50 (14–37 ms) but its p95 reaches
  69–88 ms under load.
- Chart warm-up count=500 is over budget at p50 under load
  (91–116 ms) and at p95 even when quiet (64 ms — inside, but the
  only warm-up number under 100 ms at p95).

And the production path is strictly longer than loopback: every
command pays the user-PC -> Germany-server round trip twice (command
down, `action.result` up) plus server-side processing, none of which
is in these numbers. **The WAN leg is UNMEASURED.** What it would
take to measure it: a timed WebSocket round-trip from the owner's
Windows PC to the Germany server — e.g. heartbeat RTT plus a
`close_position` (or a no-op echo) on a demo account against the real
server, with the same per-step timestamps. That needs the owner and a
demo account; it cannot be done from this VM.

Bottom line for the v1.4.0 work: fix hot spots 1 and 2 first (they
are the only defects that add *hundreds* of ms), then re-measure on
the owner's PC against the real server before claiming <100 ms.
