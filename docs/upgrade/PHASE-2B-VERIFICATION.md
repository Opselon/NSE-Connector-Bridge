# Phase 2b — live verification record (2026-11-19)

Every record below was produced on this machine, today, against the
REAL MetaTrader 5 Demo terminal through the Bridge's own execution
path. Nothing here is mocked, inferred, or carried over from history.
Secrets are not recorded. All trade objects were test-owned
(`NSE-P2B-VERIFY` / `E2E_*` comments, 0.01–0.02 lots, auto-cleanup)
and the account was verified clean afterwards.

## Environment

| Field | Value |
|---|---|
| Terminal | MetaTrader 5 (build from driver probe: `pkg=500.6323.16 Nov 2026`) |
| Account | login `10011755849`, server `MetaQuotes-Demo` |
| Account trade mode | `trade_mode: 0` = **DEMO** (`ENUM_ACCOUNT_TRADE_MODE`) |
| Account margin mode | `margin_mode: 2` = **RETAIL_HEDGING** (`ENUM_ACCOUNT_MARGIN_MODE` — verified against the package constants, E-OC-1) |
| Balance at probe | 26 209.26 USD |
| Market state | FX session open (fresh EURUSD ticks throughout) |
| Gateway hop | **NOT available** — `bridge.nexusverse.space` is still not serving (PLATFORM-GAPS §6 / D11). The Gateway→Bridge leg therefore remains mock-proven (`tests/integration/test_mock_server.py`); everything below exercises the Bridge→MT5 leg, including the exact command-channel code path (`CommandExecutor`/`CommandValidator` + engine) that gateway traffic flows through. |

## E-OC-1 — official `order_check` behavior per trade action (probe)

`scripts/phase2b_probe_order_check.py` — `order_check()` never sends,
so this probe is read-only (its one mutation is a far-off test-owned
pending order, placed and removed within the probe: retcodes
10009/10009).

| Check | Result |
|---|---|
| `order_check` on TRADE_ACTION_DEAL (0.01 EURUSD) | retcode **0**, full preview: balance 26 209.26, equity 26 205.92, margin 32.97, margin_free 26 172.95, margin_level 79 484.14, comment "Done" |
| Same DEAL at 1000 lots | retcode **10014** "Invalid volume" — refused |
| Same DEAL at 0.001 lots | retcode **10014** "Invalid volume" — refused |
| `order_check` on TRADE_ACTION_SLTP (unchanged SL/TP) | retcode **10025** "No changes" — accepted, meaningful |
| `order_check` on TRADE_ACTION_SLTP (changed values, invalid geometry) | retcode **10016** "Invalid stops" — refused |
| `order_check` on TRADE_ACTION_MODIFY (unchanged pending order) | retcode **10025** "No changes" — accepted |
| `order_check` on TRADE_ACTION_MODIFY (bogus ticket) | retcode **10013** "Invalid request" — refused |
| `order_check` on TRADE_ACTION_REMOVE | retcode **0** — accepted |
| Package constants | `ACCOUNT_MARGIN_MODE_RETAIL_NETTING=0`, `_EXCHANGE=1`, `_RETAIL_HEDGING=2` |

**Design consequence (W2):** the check result code 0 = pass; 10025 =
pass for SLTP/MODIFY (no-changes semantics); anything else = refusal
(order_send never reached); `None` = fail closed.

## E-P2B-1 — account/state probe (`phase2b_live_verify.py probe`)

login 10011755849 / MetaQuotes-Demo / trade_mode 0 (DEMO) /
margin_mode 2 (hedging) / driver CONNECTED / 2 open positions — both
the user's own pre-existing ones (magic 0), untouched throughout / 0
test-owned positions.

## E-P2B-2 — real `close_position` (full)

| Field | Value |
|---|---|
| Ticket | 152934678525 (test-owned, opened 0.01 EURUSD via the Bridge's own market-order path) |
| Operation | close_position (full) |
| Retcode | **10009 DONE** |
| order_check retcode | **0** (pre-send check passed) |
| Fill price | 1.09811 |
| Requested / closed / remaining | 0.01 / 0.01 / 0.0 (position gone) |
| Duration | **170.5 ms** client-measured |

## E-P2B-3 — real `modify_sl_tp`

| Field | Value |
|---|---|
| Ticket | 152934679419 (test-owned 0.01 EURUSD) |
| Retcode | **10009 DONE** |
| order_check retcode | **0** |
| Requested SL/TP | 1.09779 / 1.09873 |
| Broker state before | sl 0.0, tp 0.0 |
| Broker state after (re-read from the terminal) | **sl 1.09779, tp 1.09873 — landed exactly** |
| Duration | **140.0 ms** |

## E-P2B-4 — real partial close (W3 reduce-volume path)

| Field | Value |
|---|---|
| Ticket | 152934681740 (test-owned 0.02 EURUSD) |
| Operation | `close_position(volume=0.01)` — the W3 server-path reduce-volume route |
| Retcode | **10009 DONE** |
| order_check retcode | **0** |
| Requested / filled / remaining | 0.01 / **0.01** / **0.01** (re-read from the terminal) |
| `partial` / `reconciled` | true / true — requested vs filled volumes are distinct fields, remaining volume proven against broker truth |
| Retry | **none scheduled** |
| Duration | 155.6 ms |

Operational finding: MT5 **rewrote the position comment** on the
partial close (the `NSE-P2B-VERIFY` marker no longer starts the
field), which is why ownership tracking by comment alone is fragile —
the Bridge's `strict_magic_guard` + magic stamping is the reliable
ownership mechanism (the harness closed this position by its known
ticket).

## E-P2B-5 — uncertain-result reconciliation (W4, live)

Scenario: the close **executed on the broker** but the response frame
was withheld once (a proxy swallowed the `order_send` result — the
only simulated element; the broker-side state change and the
reconciliation reads are entirely real).

| Field | Value |
|---|---|
| Ticket | 152934681796 (test-owned 0.01 EURUSD) |
| Response observed by the engine | None → retcode_label `NO_RESPONSE` |
| Bridge behavior | send loop STOPPED; reconciled against live broker state; **no re-send** (`sends_observed: 1`) |
| Reconciliation source | `position_gone` (live `positions_get`) |
| Reported | `status done`, closed_volume 0.01, remaining 0.0, `reconciled: true`, `ambiguous_fill_recovered: true` |
| Duration | 152.4 ms |

The duplicate-execution hazard is closed by construction: the W4 path
never re-sends a volume-bearing op while the outcome is unknown
(pinned by `tests/unit/test_order_check_gateway.py`, including the
partial-fill variant where a re-send would double-close).

## E-P2B-6 — `modify_pending` live proof (W1 §17.4 close-out, condition (c))

| Field | Value |
|---|---|
| Ticket | 152934682397 — a **pending order** (`orders_get`, test-owned BUY_LIMIT 0.01 EURUSD @ 1.0931) |
| MT5 action | `TRADE_ACTION_MODIFY` via `trade_modify_pending_order` — the exact function the server channel routes `modify_pending` to (§17.4) |
| Retcode | **10009 DONE** |
| order_check retcode | **0** (pre-send check on the MODIFY request) |
| Requested SL | 1.0921 |
| Order state before | sl 0.0, price_open 1.0931 |
| Order state after (re-read from the terminal) | **sl 1.0921, price_open 1.0931 — landed exactly, price untouched** |
| Duration | 172.5 ms |
| Cleanup | order removed (retcode 10009), zero leftovers |

**Gate status after this proof:** §17.4 close-out conditions are now
(a) code — done, (b) tests — done (unit + this live record), (c) live
proof at market open — **DONE (this record)**, (d) published minimum
`connector_version` — **still owed, Platform-owned**. The Bridge's
`modify_pending_proven` gate therefore STAYS CLOSED by default until
the Platform owner publishes the minimum version (mission §6 decision
table). The gate note in `server/commands.py` records exactly this
state.

## E-P2B-7 — full e2e suite against the live terminal (first execution ever)

`pytest tests/e2e -q` → **104 passed, 0 failed, 0 skipped in ~26 s**
on the live MetaQuotes-Demo terminal (2026-11-19). The historical
"104 e2e" claims in the old acceptance records were collections, never
executions (ACCEPTANCE row 15 says so explicitly) — this is the first
time the suite has actually run, and it runs GREEN.

Two suite defects had to be fixed before this could ever pass anywhere
(they only manifest on a machine WITH a terminal — the only machines
where e2e can run):

1. The e2e HTTP helper never sent the mandatory `X-API-Key`, so every
   `/api/v1` call was rejected 401 (`tests/conftest.py` — now carries
   the app's own key on every call, including direct client.post/get
   sites).
2. The WS round-trip test never provisioned entitlements, so the §6.3
   step-6 gate correctly rejected the command with
   `AutoTradeNotEnabled` — the test now provisions the entitled
   validator itself (what validate-key would deliver).

Category evidence (all live): cat1 connection (real terminal info,
broker offset detection), cat2 account, cat3 symbols, cat4 market,
cat5 charts (real candle/tick data + configured chart context), cat6
positions, cat7 orders (real pending placement/cancel), cat8 execution
(real 0.01-lot open + partial close + pending orders, self-cleaning),
cat9 risk, cat10 diagnostics, system upgrades, and the full WS
server-channel roundtrip (real close over WS, chart.data + chart.tick
frames, §5/§6/§17.1 protocol).

## Clean-state proof

After every step: `positions_get`/`orders_get` show ONLY the user's
own pre-existing positions (tickets 152914036247, 152914036294 —
present before this session began and never touched); zero test-owned
objects remain.

## What remains unproven (exact blockers)

| Scenario | Blocker |
|---|---|
| Gateway→Bridge leg over the real WSS | Platform gateway undeployed (D11, PLATFORM-GAPS §6) — mock-integration proof exists; live proof needs the Platform deploy |
| Telemetry/diagnostics upload | CONTRACT v1 §4.2 defines no endpoint (D1) — the durable queue drains by TTL; sink stays inert by design |
| `modify_pending` activation | (d) published minimum connector version — Platform-owned decision (§17.4) |
| `open_order` activation | All four §17.5 conditions — user decision; the default gate stays CLOSED |
| `policy.envelope` delivery | The frame type is a Phase 2b PROPOSAL — the contract defines no policy frame yet; the Bridge machinery is dormant until the Platform adopts one |
| Release signing / first tagged release | Operator-owned (Ed25519 public key provisioning, release tag authority) |
