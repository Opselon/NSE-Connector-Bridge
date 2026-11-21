# Architecture

Status: client-only refactor, branch `refactor/client-only-bridge`
(Phase 6). Source of truth for the server contract: CONTRACT v1
(`NSE-CONNECTOR-CONTRACT.md`, NSE-Platform, merged via PR #102).
Section references (§x) are contract sections unless noted.

## 1. What the Bridge is — and is not

A secure, reliable, persistent **communication infrastructure**
between NSE Platform (cloud/server — central intelligence,
orchestration, control) and the user's local MetaTrader 5 terminal.
The Bridge owns connectivity, validation, local execution, and result
reporting. The server owns orchestration, decisions, permissions, and
task management.

The Bridge is NOT a trading product: the local AI Agent product
(`agent/`, LLM, router, coach, promptmaster) was deleted in Phase 3.
No autonomous decisions, no indicators, no risk engine, no backtest,
no optimizer lives in the client. Local computing is not permanently
banned — a task-execution boundary exists for future server-assigned
compute (§7) — but no engine is implemented now.

## 2. Component map

```
                         NSE Platform (server)
                    https://bridge.nexusverse.space
                       wss://…/api/v1/connector/ws
                                  ▲  outbound only
                                  │  Bearer + X-NSE-Device-Id
┌──────────────────── NSE-MT5-Bridge.exe ────────────────────────┐
│ main.py ── entrypoint, CLI: serve flags (--host/--port/--gui/   │
│              --no-connect/--diagnostic/--console), query        │
│              subcommands (status|health|diagnostics|mt5 list,   │
│              --json; dispatched BEFORE any serve setup — they   │
│              query the RUNNING instance and never boot a second │
│              one, see nse_bridge/cli.py), boot, uvicorn thread, │
│              GUI window; single-instance named mutex            │
│              (Local\NSE-MT5-Bridge-SingleInstance,              │
│              nse_bridge/single_instance.py) is acquired before  │
│              MT5 attach / port bind — a second instance exits   │
│              with code 2 + guidance instead                     │
│                                                                 │
│  server/  CONTRACT v1 client (Phase 2)                          │
│    rest.py       validate-key / register-device / sync/batch /   │
│                  heartbeat / session/* / releases/latest /      │
│                  manifest; §3.6 error mapping; retry/backoff   │
│    enrolment.py  state machine: not_configured → validating →    │
│                  registered | failed_auth | failed_transient;   │
│                  state waits are event-driven (per-waiter        │
│                  asyncio Events, enrolment.py:156/395) — no     │
│                  polling; re-enrolment is the recovery path     │
│                  after 401                                     │
│    commands.py   §6.2 envelope, §6.3 validation order, §6.1     │
│                  allowlist (3 sendable ops + closed §17.5       │
│                  open_order gate), replay cache (10,000),       │
│                  lifecycle, action.result (§6.5), reconcile    │
│    chart.py      §17.1 chart request/subscribe, rate limit      │
│                  60 frames/min, max 20 subscriptions           │
│    sync.py       §3.3 batches (page 500, 250 ms), checkpoint    │
│                  (max broker time, never backwards), queue      │
│                  bound 32, account-switch reset; feeds the     │
│                  journal collector, carries accounting          │
│                  provenance inside the batches (§8)            │
│    queue.py      BoundedPendingQueue (results persist across     │
│                  reconnects; oldest evicted WITH warning)      │
│    manifest.py   §4.1 manifest — only truthfully mappable       │
│                  fields (device_id, manifest_hash, action      │
│                  count/names; NO terminal_build — the driver   │
│                  exposes no numeric build)                     │
│    uuid7.py      uuidv7 ids (command_id, batch_id, message_id) │
│    errors.py     §3.6 ServerErrorCode + parse_error_response    │
│  realtime/                                                       │
│    channel.py    ServerChannel: WS session, 15 s heartbeat,     │
│                  §5.3 liveness is HEARTBEAT-REPLY based — 45 s  │
│                  without a server heartbeat reply = dead         │
│                  (channel.py:796); other server frames do NOT   │
│                  keep the session alive (mirrors Platform PR    │
│                  #144 Bug C). Lifecycle start/stop/restart is   │
│                  serialized under `_lifecycle_lock` with a      │
│                  running-guard (no duplicate supervisors);      │
│                  reconnect 500 ms doubling → 30 s + jitter;     │
│                  outbound `_OutboundQueue`: two FIFO deques +   │
│                  one wake event sharing the 128 bound (results  │
│                  first, chart frames second — a chart frame is  │
│                  never lost to a racing waiter), account-switch │
│                  runs a single in-flight sync, chart polls are  │
│                  per-subscription in-flight guarded             │
│    bus.py        local pollers (tick adaptive, account 1 s,     │
│                  positions 2 s, orders 3 s, status 10 s) —     │
│                  feeds REST sync + WS chart frames. The tick    │
│                  poller sleeps until the next subscription is   │
│                  due (idle wake 1 s — no 50 ms spin, zero MT5   │
│                  calls with no subscriptions); orders/status    │
│                  pollers skip work entirely when                │
│                  `bus.subscriber_count == 0` (bus.py:207,240);  │
│                  slow-consumer drops are counted on the bus     │
│                  (bus.py:62), never silent                      │
│  reliability/ (Phase 5/6, ARCHITECTURE-CONTRACT §8)             │
│    telemetry.py  structured JSON events, exactly 9 fields;      │
│                  ERROR immediate / WARN queued / INFO local-    │
│                  only / DEBUG diagnostic-mode only; bounded    │
│                  queue (128, TTL 1 h, dedup, backoff, expiry); │
│                  DiagnosticsSink adapter (default inert — no    │
│                  diagnostics endpoint exists in CONTRACT v1    │
│                  §4.2; see docs/PLATFORM-GAPS.md §2)          │
│    self_healing.py  15-scenario matrix (see §6 below)          │
│    result_store.py  persisted unacknowledged results            │
│  mt5/driver.py  official MetaTrader5 package, serialized worker │
│                  thread (process-global IPC, not thread-safe); │
│                  bounded job queue (256, MT5_JOB_QUEUE_MAX —   │
│                  a stalled worker fails fast with structured   │
│                  MT5_CALL_FAILED instead of piling up); lazy   │
│                  import (collection works without MT5);        │
│                  discovery: env → standard paths → App Paths → │
│                  custom/portable dirs → running processes      │
│  symbols/  broker-aware symbol discovery + resolution (spec §4, │
│            gap C1 — see docs/SYMBOLS.md): SymbolMetadata (all   │
│            broker fields, None when unreported, never invented),│
│            SymbolInventory (all/visible/hidden/search/          │
│            availability), SymbolResolver (canonical BASE/QUOTE  │
│            → exact broker symbol; ambiguous, never guesses;     │
│            mapping cache + refresh/invalidate). BRIDGE-LOCAL:   │
│            CONTRACT v1 defines no canonical format and no      │
│            symbol-inventory channel (D7); the only contract     │
│            symbol path is the §17.1 chart protocol.             │
│  engine/  the 91-command local registry (registry.py) +        │
│           handlers: connection, account, symbols, market,      │
│           charts, positions, orders, trading (execution        │
│           hardening), risk (pure calculators 73–81, local      │
│           only), diagnostics, system                           │
│  journal/  Phase C3 (spec §§8–9, docs/JOURNAL.md): event shape │
│           + collector (MT5 state/history only, dedup, bounded │
│           ranges/pages, reuses the sync checkpoint) +          │
│           accounting provenance labelling + balance-operation  │
│           surfacing. No upload channel (D6), no new sync kind. │
│  tasks/    task-execution boundary skeleton ONLY: envelope,     │
│           validation mirroring §6.3, bounded worker,           │
│           task.result. Kind registry starts EMPTY; NOT wired   │
│           to the WS command path; wire format needs a future   │
│           contract amendment (docs/PLATFORM-GAPS.md §5).       │
│  api/      FastAPI local API: one POST /api/v1/{command} per    │
│           registry command + management router (settings,      │
│           diagnostics, updates, terminals, subscription,      │
│           symbols inventory/resolve/refresh, execution recent,│
│           journal events) + /ui, /docs, /ws/ui. Mandatory     │
│           X-API-Key (§4) on API routes AND the /ws/ui upgrade │
│           (X-API-Key header or the loopback GUI cookie,        │
│           app.py:420–436). All blocking MT5/discovery/symbol  │
│           work in the management router is threadpool-wrapped │
│           (run_in_threadpool — the event loop never blocks on │
│           MT5 IPC or discovery). /health is a light probe by  │
│           default (cached driver state; `broker_connected` is │
│           honestly `None` when unknown); the full IPC probe   │
│           is opt-in via `/health?probe=full` (app.py:362–391).│
│  gui/      Connector Control Center: 7 sections (Connection,    │
│           MetaTrader, Account & Subscription read-only,        │
│           Symbols, Execution, Diagnostics, Updates & Releases;│
│           settings live under Connection). pywebview window,   │
│           browser fallback.                                    │
│  secrets.py  Windows Credential Manager (keyring, service      │
│           "NSEBridge") / DPAPI file store; dev-only file       │
│           fallback on non-Windows (never required for tests).  │
│           Legacy plaintext keys migrated + deleted on first run.│
│           A corrupt DPAPI store file is quarantined as          │
│           `secrets.dpapi.json.corrupt-<ts>` with a loud ERROR   │
│           log before regeneration (per-entry DPAPI failures    │
│           are NOT quarantined — the different-user case keeps  │
│           its store, secrets.py:224–242).                      │
│  updates.py  verified updater (§3.5): SHA-256 + Ed25519 before  │
│           any install; download-only staging; install on       │
│           restart via a staged script (our code, never the     │
│           artifact). check()/download() are serialized by one  │
│           asyncio op lock (updates.py:451,556,616); staging a │
│           new artifact prunes stale staged ones                │
│           (updates.py:727,755). Fail-closed without a public  │
│           key (§5).                                            │
│  config.py   JSON + NSE_BRIDGE_* env; production default        │
│           https://bridge.nexusverse.space; no hardcoded dev    │
│           endpoints; secrets never persisted here.             │
└─────────────────────────────────────────────────────────────────┘
                                  │  MetaTrader5 C++ IPC
                                  ▼
                         MT5 terminal64.exe
```

## 3. Data flow

**Outbound (Bridge → Platform).** Two channels, both outbound-only;
the Bridge opens no inbound port:

1. REST (`https://bridge.nexusverse.space/api/v1/connector/…`):
   enrolment (`validate-key`, `register-device`), sync batches
   (`sync/batch` — the ONLY sync channel), REST heartbeat,
   `session/refresh|revoke`, `releases/latest`, `manifest`.
2. WS (`wss://…/api/v1/connector/ws`): `heartbeat` (15 s),
   `action.result`, `chart.data`, `chart.tick`. Nothing else is sent.

**Inbound (Platform → Bridge).** WS frames only: `command`,
`sync_request`, `ack`, `heartbeat`, `chart.request/subscribe/
unsubscribe`. Unknown frames are silently ignored, never fatal.

**Local.** GUI/operator → loopback FastAPI (`127.0.0.1:8788`,
mandatory `X-API-Key`) → registry command → serialized MT5 worker
thread → terminal. Results flow back through the same layers; server
commands additionally produce an `action.result` on the WS channel
(persisted in the bounded queue when the socket is down, delivered on
reconnect — never dropped silently).

## 4. State machines

### Enrolment (`server/enrolment.py`)

```
not_configured ──(key present)──▶ validating ──▶ registered
                                     │   ▲            │
                                     │   └──(401 on another endpoint:
                                     │       re-enrol via register-device)
                                     ▼
                              failed_auth ── terminal for this key:
                              no retry, no unauthenticated fallback,
                              user must enter a new key
                              failed_transient ── backoff 500 ms
                              doubling → 30 s, then retry
```

### Command lifecycle — local (clarification #2, exact states)

```
RECEIVED → VALIDATED → AUTHORIZED → DISPATCHED → EXECUTING →
    COMPLETED | FAILED | UNKNOWN | EXPIRED
```

`AUTHORIZED` = entitlement + server authorization confirmed locally.
`UNKNOWN` = result uncertain after timeout/disconnect — reconcile the
actual MT5 state before any retry; never blind-retry a non-idempotent
op. Validation rejections end local life in `FAILED` (wire truth is
`action.result` status `rejected`); an expiry rejection ends in
`EXPIRED`. Illegal transitions raise (`LEGAL_TRANSITIONS` in
`server/commands.py`).

### Command lifecycle — server side (§6.6, Platform-owned)

```
pending → delivered → (executed | rejected | mcp_failed) | expired
```

The Bridge maps its local states onto `action.result` truthfully;
it does not own the server-side lifecycle.

### Validation order (§6.3, exact — the server interprets rejections by it)

```
malformed → expired → wrong-device → wrong-account → allowlist
(→ modify_pending capability gate §17.4 → open_order activation
gate §17.5, both CLOSED by default) → entitlement
(position_manager + auto_trade) → replay
```

Only a SUCCESSFUL validation consumes the replay id. Rejections
return `action.result` with `status: "rejected"` + reason; the Bridge
never executes a rejected command and never self-retries one.

## 5. Telemetry (ARCHITECTURE-CONTRACT §8)

One structured JSON event, exactly nine fields — no more, no less:

```
timestamp, severity, event_type, device_id, correlation_id,
command_id, component, error_code, sanitized_details
```

Levels: `ERROR` — report immediately when possible (queued AND an
immediate delivery attempt when a sink is attached); `WARN` —
important operational problems, queued; `INFO` — only meaningful
lifecycle events, local log only, NEVER queued (no success-log
flooding); `DEBUG` — local diagnostic mode only, disabled by default
(`diagnostic_mode`, env `NSE_BRIDGE_DIAGNOSTIC_MODE=1`, or the
`--diagnostic` CLI flag).

The bounded local queue (`DiagnosticQueue`, default 128 items, 1 h
TTL, dedup by event_type+error_code+correlation/command id, backoff
base 1 s doubling capped at 60 s, max 5 attempts) retries, evicts the
oldest WITH a warning when full, expires stale entries (counted), and
abandons after max attempts (counted).

**Delivery gap (CONTRACT v1 §4.2):** the contract defines NO
diagnostics/events upload endpoint. Telemetry therefore travels via
existing truthful channels only (`action.result` errors,
heartbeat/liveness, manifest updates), and the adapter interface
(`DiagnosticsSink.send(event) -> bool`) stays inert
(`NullDiagnosticsSink` — sends nothing, to no URL) until a future
contract version defines the endpoint. See docs/PLATFORM-GAPS.md §2.

## 6. Self-healing matrix (ARCHITECTURE-CONTRACT §8)

15 scenarios, registered as the `Scenario` enum
(`reliability/self_healing.py`: `SCENARIOS = tuple(Scenario)`) and
dispatched by name to one `_handle_<scenario>` method each — that is
why a grep for `Scenario(` finds only the enum definition: there is
no per-scenario constructor call; registration IS the enum + the
handler table. All 15 have handlers and unit tests
(`tests/unit/test_self_healing.py`, one test per scenario minimum):

| # | Scenario | Handling (summary) |
|---|---|---|
| 1 | internet_loss | degraded; WS reconnect backoff continues; no commands executed while offline; batches stay queued |
| 2 | platform_down | recovering when REST is reachable but WS is down (REST heartbeat keeps liveness); degraded otherwise |
| 3 | ws_drop | recovering; §5.4 reconnect; pending results re-sent on reconnect |
| 4 | server_restart | recovering; fresh session, no resume payload; persisted results re-sent; manifest re-sent on next sync |
| 5 | mt5_close | degraded; no execution attempted; `MT5_NOT_RUNNING` diagnostic |
| 6 | mt5_restart | recovering; fresh sync account → positions; UNKNOWN commands reconciled before any retry |
| 7 | ipc_loss | degraded; `MT5_IPC_FAILED`; no execution while IPC is down |
| 8 | terminal_path_change | degraded; re-discover terminals; stored path never overwritten automatically |
| 9 | auth_expiry | recovering via re-enrol (`register-device`); a dead key (`failed_auth`) is never retried |
| 10 | registration_invalid | failed, terminal structured error; no retry, no unauthenticated fallback |
| 11 | lost_execution_response | UNKNOWN first; reconcile against live MT5 state; never blind-retry |
| 12 | duplicate_commands | rejected as `Replayed` (§6.3 step 7); never executed |
| 13 | expired_command | rejected as `Expired` (§6.3 step 2); never executed |
| 14 | invalid_local_config | failed, explicit structured error; subsystem refuses to start (fail closed) |
| 15 | bridge_restart_mid_operation | recovering; persisted unacknowledged results re-queued |

Rules: where a scenario is already handled by existing code (WS
reconnect, replay cache, enrolment retry, result persistence), the
matrix entry wires to that handler through a `HealingContext`
callback — it does not rewrite working recovery logic. When no
callback is wired, the entry records "awaiting <action>" and stays
`degraded` — it never claims a recovery it did not perform. No fake
fallbacks: an unsupported fallback is an explicit structured error,
never a silent success. `ok` means "the fault was detected AND the
correct handling ran", not "everything is fine".

## 7. Boot, process model, and honest local status

Boot order (`nse_bridge/main.py`): query subcommands dispatch FIRST
(`main.py:147` — `status|health|diagnostics|mt5 list --json` talk to
the RUNNING bridge over the loopback API and never boot a second
instance), then the single-instance named mutex
`Local\NSE-MT5-Bridge-SingleInstance` (`nse_bridge/single_instance.py:25`)
is acquired BEFORE MT5 attach and port bind — a second launch exits
with code 2 and guidance instead of racing a live one — then uvicorn,
then MT5 auto-connect starts only AFTER the API is ready
(`main.py:233–237`; the old 0.5 s sleep raced uvicorn boot). On a
frozen console=False exe, `--console` runs the Bridge as a foreground
console service (`nse_bridge/console_mode.py`): attach or allocate a
console, no GUI window, clean Ctrl+C shutdown.

The status command `get_mt5_connection_status`
(`engine/connection.py:74`) runs a live terminal liveness probe that
is best-effort by design (`connection.py:82`): a host with MT5
installed but not attachable reports an honest degraded status with a
structured diagnostic code — the status endpoint itself never
becomes an HTTP 500. Executor crashes outside the handler table emit
a rejection-shaped `action.result` (`channel.py:1064–1084`) so a
command can never expire silently with no wire outcome.

## 8. Symbols — discovery and resolution (spec §4, gap C1)

`nse_bridge/symbols/` maps a server-supplied canonical instrument ID
(`BASE/QUOTE`, e.g. `XAU/USD` — a BRIDGE-LOCAL scheme, see below) to
the exact broker symbol name MT5 needs (`XAUUSD.a`), with full broker
metadata for comparison. `SymbolResolver.resolve()` returns
`resolved | ambiguous | not_found`; several candidates are reported
`ambiguous` with the full candidate list — never guessed between —
unless caller-supplied `expected` metadata leaves exactly one
standing. The mapping cache (TTL 300 s) is dropped via `invalidate()`
when the broker environment changes (B3 `broker_connection_change`
self-healing hook). Contract dependency D7: CONTRACT v1 defines no
canonical format and no symbol-inventory sync channel — the only
contract symbol path is the §17.1 chart protocol — so the scheme is
Bridge-local and no frame or endpoint is invented. Full rules,
ambiguity policy, cache/refresh, and the honest hidden-symbol
completeness caveat: **docs/SYMBOLS.md**.

## 9. What is deliberately NOT here

- **No backtest.** Zero backtest code, no backtest screen, no
  backtest status — intentionally excluded per clarification #2.
  MT5's Python API exposes no Strategy Tester control; a fake local
  optimizer would be dishonest. Do not add one in this phase.
- **No local engines.** No EMA/RSI/MACD, no LLM, no news/fundamental,
  no risk engine, no position sizing, no coaching, no optimizer, no
  portfolio intelligence. The task boundary (`nse_bridge/tasks/`) is
  a skeleton with an EMPTY kind registry.
- **No invented endpoints.** Every server shape is CONTRACT v1. Where
  the contract is silent (diagnostics upload, task wire format), the
  Bridge builds a clean adapter/boundary and documents the gap —
  it never invents a production URL or payload.

## 8. Journal & accounting source data (Phase C3, spec §§8–9)

`nse_bridge/journal/` — see `docs/JOURNAL.md` for the full contract.

- `journal/events.py`: the stable `JournalEvent` shape (full spec §8
  field set) and the documented dedup key
  `(event_type, deal/order/position/ticket id, execution_time)`.
- `journal/collector.py`: `JournalCollector` — builds events ONLY
  from actual MT5 state/history (snapshots, history orders/deals,
  closed-position summaries, real broker responses, real
  connection transitions). Never invents events, never infers fills
  from submitted requests. Bounded ranges (explicit `from`/`to`, no
  unlimited download), pagination (500, mirroring sync §3.3),
  event-level dedup, bounded stores (10,000 seen keys / 1,000
  events). Reuses the durable sync checkpoint (B1) — no second
  checkpoint mechanism.
- `journal/accounting.py`: provenance labelling (`mt5_supplied` /
  `locally_derived` / `not_reported`) for account fields, deals, and
  closed-position summaries, plus explicit deposit/withdrawal/
  balance-adjustment/fee/credit surfacing from MT5 deal types. Raw
  broker values only — no local accounting engine, no sums, no
  statistics.
- Wiring: the sync engine (`server/sync.py`) feeds the collector
  from records it already fetches and carries the labelling inside
  the existing §3.3 batches (`provenance` on `account` items and
  history pages, `balance_operations` on deals pages). No new sync
  kind. There is NO journal upload channel in CONTRACT v1
  (dependency D6) — events live locally for the Phase C4 Execution
  screen (`SyncEngine.journal_events()`) until a contract channel
  exists; the server derives journal entries from sync batches
  today. D8 (accounting channel) is answered by the labelling
  convention inside the four existing kinds, pending a contract
  statement.
