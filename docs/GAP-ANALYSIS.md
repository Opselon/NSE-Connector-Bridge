# NSE Connector Bridge — Gap Analysis vs the Complete Infrastructure Spec

| Field | Value |
|---|---|
| Date | 2026-11-18 |
| Branch | `refactor/complete-mt5-infrastructure` |
| Head SHA | `3f0950c` — `tests: make secret-store tests platform-independent (simulate Linux backend)` |
| Working tree | Clean at audit time (`git status --short --branch` showed no modifications) |
| Governing spec | `SPEC-2026-11-18-COMPLETE-INFRASTRUCTURE.md` (user spec, 2026-11-18 10:16 +0330), §§0–25 — SUPERSEDES the earlier client-only refactor |
| Contract | `NSE-CONNECTOR-CONTRACT.md`, CONTRACT v1, 2,863 lines, read completely (§§0–19, including §14 deltas, §15 open items, §17, §18 inventory, §19 as-built Platform record) |
| Prior docs read | `AUDIT.md` (incl. its CORRECTION banner, which supersedes stale parts), `ARCHITECTURE-CONTRACT.md`, `PLAN.md` (incl. ADDENDUM #2) |
| Repo inspected | `~/workspace/NSE-Connector-Bridge-audit` (`Opselon/NSE-Connector-Bridge`), 16,412 lines of `nse_bridge/` + `scripts/` code (see §1) |

## Method

1. Read the governing spec in full (§§0–25) and the complete CONTRACT v1 (§§0–19).
2. Read the prior audit, architecture contract, and plan, including both addenda.
3. Inspected the code itself — did not trust summaries: module/function inventories,
   grep for every claimed capability, and read the decisive code paths
   (REST client, WS channel, command executor, sync engine, chart handler,
   driver discovery, telemetry/self-healing, secrets, updater, GUI, tasks).
4. Ran the test suite: `pytest tests/unit tests/integration -q` with
   `/tmp/nse-bridge-venv/bin/python` → **311 passed in 17.57 s** (verified by this audit,
   not taken from a report). The 104 e2e tests were NOT run: they require a live MT5
   terminal, which does not exist on this VM (by design they fail loudly without one).
5. Verified the known open items against code/CI where possible; where a cause could
   not be established from available evidence, it is recorded as undiagnosed, not guessed.

**Verdict scale used in §2:** *Implemented* (present, wired, tested at unit/mock level),
*Partial* (substantially present but a named piece is missing or unwired),
*Missing* (no implementation), *Deliberately not* (server-side by contract — not a gap).

**Scope note.** This document covers the Bridge only. NSE-Platform is untouched, and
every Platform-side item is recorded as a contract dependency (§4), never as a Bridge
defect. No endpoint, frame, payload, event name, or auth scheme is invented anywhere
in this document: each dependency says which contract section does not define it.

---

## §1 Current-state inventory — what the branch already delivers

Branch history: 12 commits from `main` @ `22ad015` (phases 0–6 of the earlier refactor,
carried onto the renamed branch). Code size (`wc -l`, `nse_bridge/` + `scripts/`):

| Area | Files | What it is |
|---|---|---|
| Server channel (REST) | `nse_bridge/server/rest.py` (478), `enrolment.py` (410), `sync.py` (629), `manifest.py` (86), `queue.py` (97), `errors.py` (174), `uuid7.py` (31) | CONTRACT v1 REST client, enrolment state machine, sync engine, manifest builder, bounded queues, error contract |
| Server channel (WS) | `nse_bridge/realtime/channel.py` (763), `bus.py` (250) | Persistent WS channel (header auth, 15 s heartbeat, 45 s dead-session, reconnect/backoff, 128-message bound, chart frames, `action.result` delivery) + local dashboard event bus / pollers |
| Command path | `nse_bridge/server/commands.py` (860), `server/chart.py` (608) | §6.2 envelope, §6.3 validation order, 8-state lifecycle, replay cache (10,000), `action.result` (§6.5), reconciliation, `modify_pending` capability gate (closed); §17.1 chart request/subscribe/unsubscribe/data/tick |
| MT5 driver | `nse_bridge/mt5/driver.py` (851), `mt5/types.py` (508) | Official MetaTrader5 Python package, serialized worker thread, multi-terminal discovery, version detection, metrics ring, broker-UTC-offset detection |
| Local engine | `nse_bridge/engine/*.py` (~3,900 lines) | **Exactly 90 local commands** (verified: 90 `@command` decorators across 12 modules), incl. risk calculators 73–81, execution hardening (filling-mode resolution, stops/freeze pre-validation, retcode map, `TRADE_UNVERIFIED`, close idempotency) |
| Local API + GUI | `nse_bridge/api/app.py` (413), `api/management.py` (894), `gui/dashboard.py` (49), `gui/window.py` (25), `gui/static/index.html` | FastAPI on loopback `127.0.0.1:8788`, mandatory `X-API-Key`, management endpoints (settings, connection checks, terminals, diagnostics, updates, account/subscription); Control Center GUI with **6 screens** |
| Reliability | `nse_bridge/reliability/telemetry.py` (466), `self_healing.py` (449), `result_store.py` (209) | Structured JSON telemetry emitter, bounded diagnostic queue, `DiagnosticsSink` adapter interface (inert — see §2.12), self-healing registry (**15 scenarios**), durable JSONL result store |
| Security | `nse_bridge/secrets.py` (422), `config.py` (305), `logging_setup.py` (182) | keyring / Windows Credential Manager + DPAPI secret store, mandatory local API key, loopback guardrails, log redaction |
| Updates | `nse_bridge/updates.py` (718) | `releases/latest` polling, SHA-256 + Ed25519 verification before staging, download-only, install on restart |
| Future compute | `nse_bridge/tasks/*.py` (~565 lines) | Task envelope, validation mirroring §6.3, bounded worker, kind registry — **starts EMPTY**, not wired to the WS command path |
| Tests | `tests/unit/` (24 files), `tests/integration/` (3 files + conftest), `tests/e2e/` (12 files, 104 tests) | **311 unit + mock-integration tests pass** (verified by this audit). E2E is real-MT5-only, manual, never run in CI |
| Mock server | `scripts/mock_server.py` (502) | CONTRACT-v1 mock: all REST endpoints (`validate-key`, `register-device`, `sync/batch`, `heartbeat`, `session/refresh`, `session/revoke`, `manifest`, `releases/latest`) + WS (demo command, chart request/subscribe answers) |
| Docs | `docs/` (10 files) | ARCHITECTURE, API_PROTOCOL, COMMAND_REFERENCE, SECURITY, BUILD, RELEASE, TROUBLESHOOTING, COMPATIBILITY, ACCEPTANCE (17 criteria, honest verdicts), PLATFORM-GAPS, `openapi.json` (104 paths, CI freshness-checked) |
| CI / release | `.github/workflows/build.yml` (153), `bridge.spec` (65), `scripts/write_release_manifest.py`, `scripts/write_version_info.py` | 3 jobs: `test-linux`, `build-windows`, `release` (tags only). `console=False` exe, version metadata, SHA-256 + release manifest. **No `.ico` asset exists.** |

### Delivered per earlier phase (verified, not assumed)

- **Phase 0:** Linux unit-test CI job + pinned deps (`requirements.txt`, `requirements-dev.txt`).
- **Phase 1:** Secret store (keyring/DPAPI), mandatory local API key, loopback guardrails,
  log redaction, `mt5_password` removed, no plaintext secrets in `config.json`.
- **Phase 2:** CONTRACT v1 REST + WS, 3-op allowlist, 8-state lifecycle, chart handlers,
  task-boundary skeleton.
- **Phase 3:** Local AI product deleted; full 90-command registry kept (incl. risk 73–81).
- **Phase 4:** Control Center GUI, 6 screens, guided MT5 diagnostics with structured codes
  (`MT5_NOT_FOUND`, `MT5_NOT_RUNNING`, `MT5_IPC_FAILED`, `MT5_ACCOUNT_NOT_READY`,
  `MT5_UNSUPPORTED_VERSION`).
- **Phase 5:** Multi-terminal discovery, verified updater skeleton (`releases/latest`).
- **Phase 6:** Enrolment manager, telemetry pipeline + self-healing matrix (15 scenarios),
  test honesty (required-MT5 tests FAIL without a terminal), 8+ docs, release engineering.

### Known open items (verified where possible)

- **Windows CI fails** at the `build-windows` job's "Run unit + mock-integration tests on
  Windows" step at head `3f0950c`; `test-linux` passes. Root cause is **NOT diagnosed**:
  job logs return 403 from this environment, so diagnosis rests on step conclusions + code
  only. A Linux win32-platform simulation reproduced only DPAPI-availability artifacts,
  not the real failure — recorded here as an open diagnostic item, not as solved (§2.20, §6).
- **Release-signing public key is UNPROVISIONED.** No key file exists
  (`nse_bridge/update_public_key.b64` absent; no `NSE_BRIDGE_UPDATE_PUBLIC_KEY` configured);
  none was invented. The updater refuses installs with `NO_PUBLIC_KEY` (`updates.py:81,219`).
- **No `.ico` asset exists** (`bridge.spec:62-64` documents this explicitly; none fabricated).
- **Telemetry delivery gap** (CONTRACT v1 §4.2): no diagnostics/events upload endpoint exists;
  local pipeline + inert `DiagnosticsSink` adapter only. Platform-side dependency (§4, D1).
- **Platform PR #103 / PR #107 migration-0054 duplication**: external Platform-side
  dependency, not fixable here (§4, D11).
- **Live Platform endpoint `bridge.nexusverse.space` is unverified** from this environment;
  all integration tests run against the CONTRACT-v1 mock server (`scripts/mock_server.py`).

---

## §2 Section-by-section gap analysis vs spec §§2–20

### §2.2 Complete server-to-client communication — **Partial**

**Implemented.**

- REST client (`nse_bridge/server/rest.py`): generic `_request(method, path, ...)` with
  Bearer + `X-NSE-Device-Id` headers, TLS verification mandatory in production
  (`config.py:96-107` — `https` required, `http` only in dev/test environments),
  correlation via uuidv7 `batch_id`/`message_id`, structured serialization, error
  classification (`server/errors.py`, §3.6 codes), retry with exponential backoff
  (`retry_base_s * 2**attempt`, 429/`Retry-After` honored, 5xx/transport retried;
  401/403 raised immediately, never retried — `rest.py:251-326`), re-enrolment once
  after a 401 on a non-auth endpoint, connection pooling (one reused `httpx.AsyncClient`,
  `timeout=httpx.Timeout(30.0)`, `rest.py:205-217`), graceful shutdown (lifespan teardown
  in `api/app.py:130-230`). All seven contract REST endpoints are implemented:
  `validate-key`, `register-device`, `sync/batch`, `heartbeat`, `session/refresh`,
  `session/revoke`, `releases/latest`, plus `manifest` POST (§4.1, [client-v1.1]).
- Persistent WS (`nse_bridge/realtime/channel.py`): header auth, no `hello` frame,
  15 s heartbeat / 45 s dead-session (`HEARTBEAT_DEAD_S = 45.0`), automatic reconnection
  with 500 ms doubling → 30 s + jitter (`backoff_delay`, `backoff_with_jitter`),
  fresh session on reconnect (no resume payload), message validation, `action.result`
  acknowledgement, correlation IDs, duplicate detection (replay cache), command
  expiration, structured responses, clean `close()` on shutdown, connection-state
  reporting. Outbound bound 128, results first, chart frames second, heartbeats bypass
  the queue. Unknown frames silently ignored, never fatal. REST heartbeat loop runs as
  keep-alive when WS is down (`channel.py:341-387`).
- Protocol compatibility: the Bridge was corrected to CONTRACT v1 (no invented endpoints,
  frames, or ops — see §3).

**Partial / missing.**

- **REST verbs:** the transport accepts any method string, but only `POST` and `GET` call
  sites exist. No `PUT`/`PATCH`/`DELETE` call site exists in the Bridge. The contract
  defines no `PUT`/`PATCH`/`DELETE` endpoint, so this is a spec-§2 wording gap only —
  no contract capability is unserved — but a future contract endpoint using those verbs
  would need a new call site, not just transport support.
- **Request cancellation:** no cancellation API was found in `server/rest.py` (no
  cancel token / abort path on in-flight requests). Spec §2 lists "request cancellation
  where supported" — **missing**.
- **REST heartbeat → WS open:** the REST heartbeat loop reads `pending_commands` and logs
  it (`channel.py:378-382`), but whether `pending_commands > 0` triggers a WS
  open/refresh when no WS is live was **not verified** in this audit (the WS loop runs
  continuously once `channel.start()` is called from `api/app.py:180`).
- **Live proof:** no verification against the deployed Platform (mock server only);
  long-run soak/reconnect-under-load behaviour is unproven (§6).

### §2.3 Complete MT5 terminal discovery & management — **Partial**

**Implemented.**

- Discovery (`nse_bridge/mt5/driver.py:379-465`, `discover_terminals`): configured path
  (`settings.mt5_terminal_path` / `PUT /settings`), env vars (`NSE_MT5_TERMINAL_PATH` /
  `MT5_PATH`), standard install locations, App Paths registry (Windows, guarded),
  custom directories incl. portable installs (`settings.mt5_custom_terminal_dirs`),
  running terminal processes (Windows process list; Linux stub returns none).
  Per terminal: executable path, version (PE/Win32 version detection, `driver.py:207-358`),
  running/stopped state, `exists` flag (inaccessible/invalid paths surface as
  `exists=False`), source label. Discovery is read-only — it never overwrites user
  terminal config. Selection is user-driven via `PUT /settings mt5_terminal_path`
  (`api/management.py:748-808` lists terminals and opens the selected one).
- Lifecycle: `Mt5Driver.connect()` (`driver.py:667-700`) retries `mt5.initialize(path=...)`
  with backoff; connection state machine (`DISCONNECTED/CONNECTING/CONNECTED/
  TERMINAL_ERROR/AUTH_ERROR`), metrics ring + per-op diagnostics (`OpMetrics`,
  `CallDiagnostic`), `refresh_terminal()`, `disconnect()`, unexpected-shutdown and
  IPC-failure detection surfaced as guided diagnostic codes
  (`api/management.py:497-523`).
- Re-discovery after config changes: management endpoints re-run `discover_terminals`
  per request; the self-healing matrix wires a re-discovery callback
  (`self_healing.py:137,337-338`, `on_rediscover_terminals`).
- Wrong-terminal/account safety: commands are bound to device + account
  (`commands.py` validation steps 3–4); account-switch detection in the channel
  (`channel.py:590-600`) resets sync and triggers a fresh sync on login change.

**Partial / missing.**

- **Terminal data directories:** the spec requires identifying terminal data directories.
  `TerminalInfo` (`driver.py:360-377`) carries path/version/running/source/exists only —
  **no data-directory field was found**. Missing.
- **Independent connection state per terminal:** the architecture supports multiple
  *discovered* terminals but only one *attached* terminal: a single `Mt5Driver` instance
  connects to one path at a time (`_connect_job` uses `settings.mt5_terminal_path or
  auto_discover_terminal_path()`). There is no per-terminal connection-state table and no
  way to hold two terminals connected simultaneously. For the single-user/single-terminal
  v1 case this is adequate; the spec's "independent connection state" is **not
  implemented**.
- **Terminal restart detection as a distinct event:** covered only indirectly via the
  `MT5_CLOSE` / `MT5_RESTART` self-healing scenarios and the driver's state machine;
  no dedicated restart-detection probe was verified.

### §2.4 Broker & symbol intelligence — **Partial** (local side implemented; wire still missing — §4 D7)

**Implemented (local capability).**

- Symbol listing (`nse_bridge/engine/symbols.py:112-162`, `get_trading_symbols`):
  `mt5.symbols_get()` with substring filter, `tradeable_only`, `visible_only`, and a
  result `limit`; per-item name/description/visible/trade-mode/digits/profit-currency.
- Per-symbol metadata (`symbols.py:163-370`): `get_symbol_info` (full broker spec —
  digits, point, tick size/value, volumes, stop/freeze levels, …),
  `get_symbol_price`, `get_symbol_tick_size`, `get_symbol_point`, `get_symbol_digits`,
  `get_symbol_spread`, `get_symbol_trading_sessions`, `get_symbol_trade_constraints`.
  The metadata surface is the MT5 `symbol_info` record, so broker-provided values are
  returned verbatim, never invented.
- Symbol-resolution abstraction — IMPLEMENTED locally (audit P2-10 refresh):
  `SymbolResolver` (`nse_bridge/symbols/resolver.py:140`) maps a canonical instrument
  identifier to exact broker symbols over a `SymbolInventory`
  (`symbols/inventory.py:81`: all/visible/hidden/search/select/availability) with full
  broker metadata (`SymbolMetadata`, `symbols/metadata.py:82`), reports ambiguity
  instead of guessing, and never substitutes. 29 tests in
  `tests/unit/test_symbol_resolution.py`. What is still missing is only the wire half
  (§4 D7): no sync kind carries symbols and no endpoint accepts an inventory.
- No hardcoded trading defaults: `config.py:183-186` — `magic=None`, `bot_symbol=""`
  (magic/symbol come from the server per command or explicit device config).

**Missing.**

- **Hidden symbols:** the Market Watch select step now exists
  (`SymbolInventory.select_for_market_watch`, `symbols/inventory.py:194`),
  but there is still no evidence the list is the *complete* broker
  inventory (contrast contract §17.1.2, where the MCP action
  takes `include_hidden: true`). Whether hidden symbols are reachable was **not verified**.
- **Symbol availability / untradeable detection:** only via `trade_mode` in the list
  response; no dedicated availability probe or "symbol cannot currently be traded"
  reporting, and no self-healing scenario for symbol-unavailability (§2.13).
- **Minor spec deviation:** `driver.py:793` probes hardcoded symbols
  (`"EURUSD", "GBPUSD", "USDJPY"`, plus configured `bot_symbol`) to detect the broker
  UTC offset from live tick epochs. These are tick probes only — never trading defaults —
  but they are hardcoded symbol names in code, against the letter of spec §4 ("Do not
  hardcode symbols"). Recommend replacing the probe list with "first N visible symbols
  with fresh ticks" or documenting the probe list as an explicit exception.

### §2.5 Complete account information & synchronization — **Partial**

**Implemented.**

- Account acquisition (`nse_bridge/engine/account.py`): `get_trading_account_info`
  (login, broker/server, name, type, currency, balance, equity, margin, free margin,
  profit, credit, leverage, trade mode, margin mode), `get_account_balance/equity/
  margin/free_margin/margin_level/profit/currency`, `get_trading_permissions`
  (`trade_expert`, `trade_mode`, `margin_mode`). No credentials are exposed.
- Sync (`nse_bridge/server/sync.py`): `account`-kind batch is the authoritative
  account-identity message (§7.2); broker timestamps verbatim + UTC `produced_at`;
  checkpoint = max broker time, never backwards; page 500, 250 ms between pages;
  pending queue bounded at 32 (oldest evicted WITH a warning); `sync_request` handling
  re-sends `account` then `positions` then history (`sync.py:403-418`).
- Account identity: every account message is tagged by device binding; account-switch
  detection (`channel.py:590-600`) invalidates pending old-account batches
  (`reset_for_account_switch`, `sync.py:453-465`) and forces a fresh sync — never mixing
  accounts.
- Monitoring: account poller (1 s per `realtime/bus.py` docstring) feeds both the local
  dashboard and the `account` sync batch; no unnecessary polling beyond the configured
  pollers.

**Partial / missing.**

- **Sync checkpoint is in-memory only.** `SyncCheckpoint` (`sync.py:173-232`) has no
  persistence: no save/load calls exist in `server/sync.py`. A Bridge restart resets the
  checkpoint to epoch (`1970-01-01T00:00:00Z`) and re-runs a full history backfill.
  That is *safe* (server dedups by ticket id) but wasteful, and it contradicts spec §16
  ("persist … necessary synchronization checkpoints") and §7 ("safe recovery after
  restart"). The durable result store (`reliability/result_store.py`, JSONL, bounded)
  shows the persistence pattern to copy.
- **Demo-only gate (Bridge side):** the Bridge parses `trade_mode` (`sync.py:477-486`,
  0/1/2 → demo/contest/live) but enforces nothing on it. This matches CONTRACT v1 §7.4
  (enforcement is server-side in v1; a Connector-side gate is a defined follow-up), so
  it is recorded here as a deliberate contract-aligned omission, not a defect — but the
  spec §5 mission ("the server must be able to … inspect account status") plus the
  demo-only principle means a live account currently syncs until the *server* refuses it.
- **Event-driven updates:** MT5 offers no push channel through the Python package, so
  account monitoring is polling-only (see §2.11). No server-visible account-change event
  exists beyond the next `account` batch.

### §2.6 Complete order execution infrastructure — **Partial**

**Implemented (local capability — the full MT5 surface the Python driver exposes).**

- Market orders (`nse_bridge/engine/execution.py`, `trading.py:166-218`): BUY/SELL with
  requested volume, exact broker symbol, SL/TP, deviation, magic (per-command only),
  comment, filling-mode resolution, execution result; ambiguous fills reconciled
  against live positions.
- Position management (`engine/positions.py`, `engine/trading.py:312-385`): list/inspect
  positions, full close, **partial close via volume** (`ClosePositionRequest.volume`,
  `positions.py:82`), SL/TP modify (together or singly), close-by-ticket, verify
  resulting state via read-back.
- Pending orders (`engine/execution.py:23,108`, `engine/orders.py:354-460`):
  `trade_cancel_pending_order` (cancel by ticket), `trade_modify_pending_order`
  (price/SL/TP/volume/expiration via `TRADE_ACTION_MODIFY` — the Python driver *can*
  do what the contract records as MCP-unverified, see §3 §18.9.3/18.9.4 note).
  **Limitation:** `PENDING_TYPES` (`execution.py:23`) covers only four order types —
  `BUY_LIMIT, SELL_LIMIT, BUY_STOP, SELL_STOP`. **`BUY_STOP_LIMIT` / `SELL_STOP_LIMIT`
  are absent from the execution path** (they exist only as labels in
  `mt5/types.py:44-46`, `ORDER_TYPE_LABELS`). Spec §6 requires all six pending types.
- Execution safety: stops/freeze pre-validation, margin sufficiency, filling-mode
  resolution, retcode map (`retcodes.py`, `errors.py`), `TRADE_UNVERIFIED` on ambiguous
  results, close idempotency, never blind-retry a non-idempotent op.
- Broker-specific errors: structured, normalized errors preserving the original MT5
  retcode and diagnostic details (`engine/common.py`, `errors.py`).
- Order lifecycle monitoring (local): the 8-state lifecycle (§2.7) plus
  `get_order_status` / `get_order_execution_details` (fill price vs requested, slippage,
  fill time, deals, commission — `orders.py:440+`).

**Missing / contract-blocked (remote).**

- Remote execution is the contract 3-op allowlist only (`close_position`, `modify_sl_tp`,
  `modify_pending` capability-gated, gate closed). Market open, pending-order create,
  pending-order cancel, partial close via server, and close-by are **not sendable** —
  see §4 D2–D5. The local capability exists for most of these; the remote authorization
  does not, by contract design.
- **Partial close has no server wire shape:** the §6.2 envelope carries only
  `{type, ticket, sl?, tp?}` — no `volume` field — so even if the allowlist widened,
  a server-issued partial close could not be expressed without a contract change (§4 D5).
- **Pending-order price/expiration modify via server:** no contract op; locally it exists
  (`trade_modify_pending_order`), but the server channel maps `modify_pending` to SL/TP
  only (`commands.py:699-711` → `modify_pending_order_sltp`, `trading.py:287`).

### §2.7 Order safety, idempotency & recovery — **Partial**

**Implemented.**

- Lifecycle (`nse_bridge/server/commands.py:192-218`): exact states `RECEIVED →
  VALIDATED → AUTHORIZED → DISPATCHED → EXECUTING → COMPLETED | FAILED | UNKNOWN |
  EXPIRED`, with a legal-transition table (terminal states have no outgoing edges).
  `AUTHORIZED` = entitlement + server authorization confirmed locally; `UNKNOWN` =
  result uncertain after timeout/disconnect.
- Envelope: `command_id` (uuidv7) = `idempotency_key` in v1; `expires_at` enforced
  (default/max per contract); duplicate detection via bounded replay cache (10,000 ids,
  only a *successful* validation consumes an id); account + terminal/device binding;
  validation order exactly §6.3 (malformed → expired → wrong-device → wrong-account →
  allowlist → entitlement → replay).
- Reconciliation: `_reconcile` (`commands.py:800+`) inspects actual MT5 orders/positions/
  deals after an uncertain result; a lost response ends in `UNKNOWN` first and is
  promoted to `COMPLETED` only with proof the op landed — never a blind retry.
  `TRADE_UNVERIFIED` maps to `mcp_failed` with `retry_safe` semantics preserved.
- Recovery after restart: results persist in the bounded durable JSONL result store
  (`reliability/result_store.py`) and are delivered on reconnect, never dropped silently;
  `action.result` round-trip invariant (exactly one terminal state per command) is
  unit/mock-tested (`tests/unit/test_server_commands.py`, `tests/integration/`).

**Missing.**

- **`PARTIALLY_COMPLETED` — implemented LOCALLY (audit P2-10 refresh).**
  The state exists in the lifecycle (`CommandState.PARTIALLY_COMPLETED`,
  `server/commands.py:421`): a partial close (requested partial volume, or broker
  retcode 10010 `DONE_PARTIAL`) reconciles against live MT5 truth and reports the
  remaining volume. The wire representation is still missing: contract §6.5
  `action.result` statuses are exactly `rejected | executed | mcp_failed` — there is
  no partial status on the wire, so the wire status stays `executed` with the partial
  facts (`partial`, `closed_volume`, `remaining_volume`, `reconciled`) in the payload.
  Spec §7 requires the state; the contract has no place to put it. This still needs a
  contract decision (new `action.result` status or a `partial` payload marker) before
  the Bridge can report it truthfully on the wire (§4 D5, §6).
- Execution timeout handling exists per-command (driver timeouts, 30 s chart timeout),
  but no per-command execution-timeout value is carried on the command envelope itself
  (contract defines `expires_at` only).

### §2.8 Trading journal & execution event data — **Partial** (local collector implemented + wired; upload channel missing — §4 D6)

**What exists (local collector — implemented, audit P2-10 refresh).**

- Journal collector: `JournalCollector` (`nse_bridge/journal/collector.py:324`) collects
  the spec §8 event types as discrete events with the §8 field set (ticket/position/
  order/deal IDs, symbol, volume, price, SL/TP, commission, swap, profit, execution
  time, magic, broker return code, correlation + command IDs), derived from MT5 state
  deltas, never inventing fills — and is WIRED in production: the `create_app`
  lifespan constructs it and assigns it to `SyncEngine.journal`, pinned by
  `tests/unit/test_production_wiring.py`. 28 tests in `tests/unit/test_journal.py`.
- Event-level dedup exists: dedup keys (`journal/events.py:71`).
- History sync: `history_orders` (orders + deals) and `history_positions` (closed
  positions) batches with checkpoint/dedup/pagination (`sync.py:419+`); ticket-less
  records dropped client-side with a `data_quality` warning path; bounded ranges
  (`[checkpoint, now]`); server decides ranges via `sync_request`.
- Local forensics: `get_order_execution_details` (fill vs requested, slippage, deals,
  commission), `get_trading_deals_history`, `get_trading_history_orders/positions`.
- `trade_event` flows to the **local dashboard bus only** (`api/app.py:185-186`,
  `engine/common.py:60`); it is not a server channel.

**Missing.**

- No journal/event upload channel exists in CONTRACT v1 — the only sync kinds are
  `account | positions | history_orders | history_positions` (§3.3). Journal facts today
  can only be *derived server-side* from those batches. The local collector has nowhere
  to send events until a contract endpoint exists (§4 D6); define the upload endpoint
  as a contract dependency — do not invent a frame.

### §2.9 Accounting & financial data — **Partial**

**Implemented (raw facts only — correctly, no local accounting engine).**

- `account`-kind batch items (`sync.py:474-516`): balance, equity, margin, free margin,
  profit, **credit**, currency, trade mode, terminal context; `positions` batches carry
  per-position profit; history batches carry deals (with commission/swap where MT5
  reports them) and closed positions (realized-P/L source).
- No local accounting engine exists (correct per spec §9): no net-performance, profit
  factor, win/loss, drawdown, or equity-curve computation anywhere in the Bridge.
  Risk calculators (`engine/risk.py` 73–81) are pure-math helpers for local validation,
  not accounting.

**Partial / missing.**

- **MT5-supplied vs locally-derived labelling is absent.** No payload field marks which
  values came from the broker and which were derived locally (e.g. margin level is
  computed in `engine/account.py:134`, leverage is derived). Spec §9 requires the
  distinction to be explicit. The data is present; the labelling is not.
- **Per-deal commission/swap/fee:** flows through only if MT5 reports it on the deal
  record; there is no explicit handling, NULL-vs-zero rule, or "not reported by broker"
  surfacing on the Bridge side (the rule lives in contract §3.3/§18.7.5 for the Platform).
- **Deposits/withdrawals/balance adjustments/fees:** no dedicated surfacing — they are
  visible only implicitly as deal/history records. No explicit operation or field set
  exists for them.
- Floating vs realized P/L is not separated into labelled fields (profit on open
  positions vs realized profit from closed positions/deals must be inferred server-side).

### §2.10 Market data & symbol monitoring — **Partial**

**Implemented.**

- Local market commands (`nse_bridge/engine/market.py`): `get_market_tick` (bid/ask with
  **freshness + staleness assessment**), bid, ask, last price, depth (market book),
  spread history, tick volume, watchlist, `subscribe_market_ticks` (dashboard-local).
- Charts (`nse_bridge/engine/charts.py`): `copy_rates` / `copy_ticks` with explicit symbol
  + timeframe, count/range modes, broker timestamps preserved. Local chart request count
  allows up to 100,000 — note the WS `chart.request` path caps at 1,000 per contract
  (§17.1.3); the local API is a separate surface.
- Server-controlled market data via WS chart frames (`nse_bridge/server/chart.py`, see
  §3 §17.1): one-shot requests, subscriptions (max 20, poll 5–3600 s, default 15),
  deltas only, 60 frames/min budget, 30 s timeout, retry-once-with-new-`request_id`,
  duplicate-`request_id` cache, Market-Watch prerequisite chain
  (`add_marketwatch_symbol` first, cleanup skipped when blocked), error codes per
  §17.1.4. No tick is streamed to the server by default.
- Stale/missing/unavailable detection: tick freshness assessment (`market.py:86-102`),
  `symbol_not_found` / `symbol_not_in_market_watch` / `market_closed` chart errors.

**Partial / missing.**

- **Server subscription control beyond chart frames:** none exists and none is defined
  by the contract — e.g. there is no server channel for market-book subscription or for
  raw tick streaming (deliberately out of contract v1; chart frames are the control
  plane). Local `subscribe_market_ticks` is dashboard-only.
- No server-visible "symbol availability changed" event (see §2.4, §2.11).

### §2.11 Live-state monitoring — **Partial**

**Implemented.**

- Pollers (`nse_bridge/realtime/bus.py`): tick 250 ms, account 1 s, positions 2 s,
  orders 3 s, status 10 s — feeding the local dashboard bus and the REST sync engine
  (account/positions batches). Pollers never die on a stray MT5 error.
- Health/diagnostics: `GET /api/v1/diagnostics/summary` (`api/management.py:809`),
  driver metrics ring, connection-state reporting, account-switch detection, periodic
  reconciliation via `_reconcile` and sync checkpoints.

**Partial / missing.**

- **Event-driven updates do not exist** — and cannot exist through the Python MT5
  package, which offers no push channel. All monitoring is polling, exactly as
  `bus.py:1-18` documents. Spec §11 asks for "the most efficient supported combination
  of event notifications and controlled polling" — the Bridge uses controlled polling
  only, because no event source exists. This is a platform-capability limit, not an
  implementation omission, but it should be stated, not assumed.
- **No server-visible state-change channel:** positions/orders/deals reach the server
  only via periodic sync batches and `action.result` frames. A position opened manually
  in the terminal is visible to the server only at the next positions poll + batch flush
  (5 s flush loop, `api/app.py:183`). There is no "state changed" push, and the contract
  defines none.
- **Missed-event reconciliation** is checkpoint-based for history, not event-based for
  live state: there is no periodic full-state diff that would catch a missed position
  change between polls (the next poll simply re-reads full state, which bounds the gap
  to one poll interval — acceptable, but not a reconciler).

### §2.12 Centralized logging, telemetry & diagnostics — **Partial** (local: Implemented; delivery: Wired, live proof owed)

**Implemented (local pipeline).**

- Structured JSON telemetry (`nse_bridge/reliability/telemetry.py`): `TelemetryEmitter`
  with `error/warn/info/debug` levels, 9-field event shape (timestamp, severity,
  event_type, device_id, correlation_id, command_id, component, error_code,
  sanitized details). Level policy per spec §12: ERROR reported immediately when possible,
  WARN for important problems, INFO only for meaningful lifecycle events, DEBUG local
  diagnostic mode disabled by default (`--diagnostic` flag, `main.py:45-51`).
  INFO is never queued for delivery (`commands.py:564` comment).
- Bounded diagnostic queue (`DiagnosticQueue`, `telemetry.py:158-331`): retry, backoff,
  dedup, queue limits, expiration, safe cleanup after delivery, queue-health stats.
- Security: secret redaction on every sink (`logging_setup.py:35-140` — Bearer tokens,
  `X-API-Key`, registered secret values, key-shaped fields in payloads); exception
  messages sanitized before entering results.
- Event coverage: the required §12 categories are emitted across the codebase (startup/
  shutdown, auth, registration, REST/WS, heartbeat, reconnect, MT5 discovery/connection,
  account connection, symbol discovery, execution, position/pending management, sync,
  history, broker errors, validation, authorization, expiration, duplicates, config
  errors, security events, updates, unexpected exceptions) — via the emitter and the
  structured `loguru` logging. (Coverage was verified by category presence in code, not
  by an exhaustive per-category test.)

**Missing.**

- **Delivery — WIRED (audit P2-10 refresh).** D1 is closed on the Bridge side by P5a:
  the Platform defines the endpoint — `POST /api/v1/connector/diagnostics` (CONTRACT
  §4.2 amended) — and the Bridge ships `HttpDiagnosticsSink`
  (`nse_bridge/api/app.py:203–216`), attached at startup with the same Bearer <redacted>
  `X-NSE-Device-Id` auth as every other device call. ERROR drains immediately on emit;
  WARN/ERROR drain on the 60 s periodic drain (`DIAGNOSTICS_DRAIN_INTERVAL_S`).
  **Still owed:** live ingest proof against the deployed Platform (D11) — unit tests
  prove the sink against `httpx.MockTransport` only. Until that proof exists, spec §25
  acceptance item "ERROR and WARN events reach the server reliably" stays Not-proven.
- **Persistent delivery of important unsent events — WIRED.** The emitter's queue is the
  durable one (`DurableDiagnosticQueue` over `DurableDiagnosticStore`,
  `api/app.py:175–180`, `c27bf0b`): queued diagnostics survive a Bridge restart.

### §2.13 Automatic recovery & self-healing — **Partial** (15 of 20 scenarios)

**Implemented.**

- Self-healing registry (`nse_bridge/reliability/self_healing.py`): 15 scenarios, each
  with detect → classify → report → recover-safely → expose-final-state, wired to the
  real handlers where they exist (WS reconnect §5.4, replay cache §6.3, enrolment retry
  §3.2, result persistence §6.5) via `HealingContext` callbacks; where no handler is
  wired, the entry records "awaiting <action>" and stays `degraded` — never claims a
  recovery it did not perform. No fake fallbacks: unsupported fallback = explicit
  structured error. One unit test per scenario (`tests/unit/test_self_healing.py`).
- The 15: internet loss, Platform down, WS drop, server restart, MT5 close, MT5 restart,
  IPC loss, terminal-path change, auth expiry, registration invalid, lost execution
  response, duplicate commands, expired command, invalid local config,
  Bridge restart mid-operation.

**Missing — 5 of the spec §13 20 scenarios have no matrix entry:**

| # | Spec §13 scenario | Status |
|---|---|---|
| 9 | Account disconnects | **Missing as a scenario.** Account-connection loss is detectable (driver state, account poller) but has no self-healing entry, no classification, and no recovery action beyond the next poll/reconnect. |
| 10 | Broker connection changes | **Missing as a scenario.** No entry; broker-server change surfaces only as an account/state change. |
| 12 | Symbol becomes unavailable | **Missing as a scenario.** No entry (follows from the missing symbol-intelligence work, §2.4). |
| 17 | Local diagnostic queue becomes full | **Missing as a scenario.** The queue has limits/expiry, but queue-full has no healing entry (and currently drains by TTL — see §2.12). |
| 19 | Update download fails | **Missing as a scenario.** Download failure *is* handled as a structured `UpdateError` state in `updates.py` (nothing staged, error recorded), but it is not a self-healing matrix entry. |
| 20 | Update verification fails | **Missing as a scenario.** Same as 19: `verify_artifact` refuses (SHA-256 / Ed25519 / null-signature / `NO_PUBLIC_KEY`) with a structured error, but no matrix entry. |

(Scenario 18 "configuration becomes invalid" is covered by `INVALID_LOCAL_CONFIG`.)
Note the count: 15 implemented + 5 missing = 20, with update-download/verify failures
handled outside the matrix. The fix is small (add the 5 entries + wire the existing
updater error states and queue-full detection into the matrix); the account/broker/
symbol entries depend on §2.5/§2.4 work.

### §2.14 Security & device trust — **Implemented** (with two v1.1 items outstanding by contract)

**Implemented.**

- Secrets (`nse_bridge/secrets.py`): Windows Credential Manager via `keyring`, DPAPI
  file store (`CryptProtectData`) as fallback; on non-Windows without a real keyring
  backend this is a hard error, never silent plaintext. Plaintext `config.json` secrets
  are migrated and deleted on first run. `mt5_password` removed entirely — the user
  signs in through MT5 itself; the Bridge detects `MT5_ACCOUNT_NOT_READY` and guides.
- Token storage/rotation/revocation: API key + device credential + local API key in the
  secret store; rotation = new key via settings + `EnrolmentManager.reset_for_new_key()`;
  revocation honored (`key_revoked` → new key from the user; device revoke → 403 →
  re-enrolment flow; `session/revoke` implemented).
- Device registration/revocation: `register-device` idempotent per `(user_id, device_id)`;
  `max_devices` overflow returns 403 and never evicts another device silently.
- TLS: verification mandatory; `wss://`/`https://` required in production; `ws://`/`http://`
  only in dev/test environments (`config.py:89-107`, `rest.py`).
- Local API: loopback `127.0.0.1` hard-bound by default; `--host` kept with guardrails —
  a non-loopback bind requires explicit `allow_non_loopback` authorization AND the
  mandatory `X-API-Key` (`main.py:95-104`, `config.py`); mandatory `X-API-Key` on every
  local API request, no empty-key mode (`api/app.py:248-269`); `ensure_local_api_key`
  auto-generates on first run (`main.py:106-108`).
- Validation/replay/expiry/authorization: strict input validation (pydantic models on
  every command), replay cache, `expires_at`, §6.3 validation order, allowlist at the
  channel boundary. Secret redaction (§2.12). Secure update verification (§2.18).
- No arbitrary remote code execution: task kinds are explicit code registrations only
  (§2.19); no dynamic import/`eval`/`exec`/download-and-run.

**Deliberately not / outstanding (contract v1.1, not Bridge defects).**

- **Per-device credential exchange** (contract §2.2, delta D-AUTH-1): deferred to v1.1;
  the Bridge sends the API key as Bearer on every call, per contract. The
  `register-device` response is extensible, so the exchange is wire-compatible later.
- **Per-command signatures** (contract §6.2, delta D-CMD-3): no signature in v1; the
  `signature` field name is reserved for v1.1. Commands are authenticated by the
  mutually-authenticated WSS session + server audit trail.
- **Local kill switch:** the contract's kill-switch scopes (§6.4 step 1, §8.3) are
  Platform-side and MCP-transport concepts. The Bridge has **no local kill-switch
  implementation** (a grep for "kill" finds only a GUI diagnostic text about MT5's
  algo-trading button and unrelated "killed" log words). If the Platform ever needs the
  Bridge to enforce a local kill switch, that is new Bridge work, not a configuration item.

### §2.15 Client resource management & performance — **Partial**

**Implemented.**

- Lightweight by construction: no local database (no PostgreSQL, no server-style DB);
  no unnecessary background workers; bounded queues everywhere (WS outbound 128, sync
  pending 32, dashboard bus 500, replay cache 10,000, result store bounded by items +
  bytes); connection pooling (single reused `httpx.AsyncClient`); efficient serialization
  (pydantic + JSON); controlled polling (fixed poller intervals, no duplicate polling
  loops — one poller per data class); non-blocking GUI (pywebview window + uvicorn on a
  separate thread) and non-blocking communication (async channel, MT5 calls on a
  serialized worker thread); graceful shutdown (lifespan teardown, clean WS `close()`,
  staged-update install hook).
- No speculative AI/GPU engines; the future-compute boundary is a skeleton only (§2.19).

**Partial / unverified.**

- **No measured performance evidence exists.** No profiling, memory, CPU, or latency
  measurements were found in the repo or docs. The design is bounded, but "minimal
  memory overhead" and "efficient" are asserted by construction, not measured. A
  lightweight soak/profile run (steady-state memory, poller CPU, WS + sync under a
  busy terminal) is owed before the §25 acceptance claim "operational monitoring works"
  can be tied to resource behaviour.
- No unbounded-task-creation guard was separately verified beyond the bounded queues and
  the single worker thread; no task-count metric is exposed.

### §2.16 Local storage & database policy — **Partial**

**Implemented as policy.**

- Data minimization is honored in practice: no local database; persisted state is
  limited to config references (`config.json`, secrets in the credential store),
  device identity (device UUID), the durable result queue (JSONL, bounded), update
  state (staged release + state), and the sync checkpoint object. No market-tick
  history, no full trading history warehouse, no duplicate account snapshots, no AI
  analysis, no strategies, no redundant calculated statistics.

**Partial / missing.**

- **Sync checkpoint is not persisted** (see §2.5) — the one item spec §16 explicitly
  lists ("necessary synchronization checkpoints") that is currently memory-only.
- **No documented retention/expiry policy for local files.** Log retention/rotation
  (`logging_setup.py` — log directory, no rotation policy verified in this audit),
  the durable result store's bounds (items + bytes caps exist in code), and the staged
  update's lifetime are not written down as a storage policy doc. The behaviour is
  bounded; the policy is not stated.
- Diagnostic-queue persistence is absent (see §2.12) — acceptable today, required once
  a delivery endpoint exists.

### §2.17 GUI — Professional Connector Control Center — **Partial** (6 of 7 sections)

**Implemented — 6 screens** (`gui/static/index.html`, verified by element IDs):

| Screen | Contents (verified) |
|---|---|
| Connection (`page-connection`) | NSE connection, auth/key validation, device registration, session expiry, WS state, heartbeats, last sync, reconnect state, reconnect/validate actions |
| MetaTrader (`page-metatrader`) | Discovered terminals, selected terminal, build/version, running state, IPC status, account connection, broker/server info, guided diagnostics |
| Account (`page-account`) | Read-only, server-resolved identity, currency, balance, equity, margin, free margin, connection state, subscription/permissions |
| Diagnostics (`page-diagnostics`) | ERROR/WARN events, connection history, MT5 health, server sync, diagnostic report export (copy) |
| Updates (`page-updates`) | Installed vs latest (`releases/latest`), update status, release notes, download progress, verification status |
| Settings (`page-settings`) | Credentials entry, server URL, log level, GUI mode — secrets masked, stored via Credential Manager |

No trading dashboard, no positions/orders screens, no AI screens, no Backtest screen.
`tests/unit/test_gui_control_center.py` asserts exactly six screens
(`test_gui_html_has_exactly_six_screens`, per `docs/ACCEPTANCE.md` #10).

**Missing — 2 sections required by the new spec §17:**

- **Symbols (NEW):** available symbols, exact broker symbol names, search, symbol
  metadata, symbol availability, resolution ambiguity. *Nothing* for this screen exists
  in the GUI. The underlying local data exists (symbol list/info commands, §2.4), so
  this is presentation + wiring work — except "resolution ambiguity", which depends on
  the missing resolver (§2.4).
- **Execution (NEW):** recent command lifecycle, execution status, failures, pending
  operations, reconciliation state. *Nothing* for this screen exists. The underlying
  data exists (lifecycle states in `commands.py`, result store, self-healing registry
  state via `GET /api/v1/diagnostics/summary`), so this is also presentation + wiring.
- **Minor deviation:** spec §17 folds credentials/settings under Connection; the Bridge
  keeps Settings as its own screen. Harmless; keep it and note the deviation rather
  than reshuffling the GUI.

### §2.18 Software update & release engineering — **Partial**

**Implemented.**

- `releases/latest` polling (`rest.py:430`, `management.py:653-746`); version detection
  + stable/prerelease channels (tag-based: `v1.2.3` stable, `v1.2.3-rc.1` prerelease);
  release metadata; SHA-256 verification + Ed25519 signature verification **before**
  any staging (`updates.py:189-236` — order: size → SHA-256 → signature; a null
  signature refuses by definition; a bad signature refuses; bytes are never staged on
  failure); secure download; download integrity checks; download-only + install on
  restart via a staged script (`Updater._stage`, `write_apply_script`,
  `launch_restart_installer`); clear update status (`/updates/status`); version
  metadata in the exe (`bridge.spec:58-60`, `version_info.txt` generated by
  `scripts/write_version_info.py`, CI-freshness-checked); release manifest + SHA-256
  attached on tags (`scripts/write_release_manifest.py`).
- The Bridge is **stricter than the contract minimum**: contract §3.5 allows
  `signature: null`; the Bridge refuses null-signature releases. No conflict — the
  Bridge never trusts SHA-256 alone from an unauthenticated source (spec §18).

**Missing (audit P2-10 refresh: the items below are now done locally — only live proof is owed).**

- **Release-signing public key — PROVISIONED (P1-6).** The key is bundled
  (`nse_bridge/update_public_key.b64`) and the release job signs every artifact when the
  `NSE_BRIDGE_RELEASE_SIGNING_KEY` secret is set; a signed release installs end-to-end
  and an unsigned one still refuses fail-closed. **Still owed:** live signed-release
  install proof on Windows (P1-9) and the Platform D19 serve of the `.sig` sidecar
  (`v1.4.0` ships SHA-256 only, no `.sig` assets yet).
- **Rollback / recovery strategy — WRITTEN + IMPLEMENTED (P1-7).** `docs/ROLLBACK.md`
  records the strategy (previous-version retention + fallback launch,
  `tests/unit/test_rollback.py` — mock proof only). **Still owed:** live Windows proof
  (P1-9).
- **`.ico` asset — EXISTS AND IS BUNDLED.** `assets/icon.ico` (generated by
  `scripts/make_icon.py`), wired into `bridge.spec` (`datas` + exe `icon`) and the Inno
  installer (`SetupIconFile`); integrity gate `tests/unit/test_icon_asset.py`.
- **Installer — EXISTS.** Inno Setup installer (`windows/NSE-MT5-Bridge.iss`, compiled
  in CI on every PR) ships alongside the portable exe (`NSE-MT5-Bridge-Setup-1.4.0.exe`
  in the `v1.4.0` release). Open: whether the Inno installer (vs the portable exe
  alone) is the distribution (§6 item 7).
- **Tagged release — CUT.** `v1.4.0` was tagged and published 2026-11-26 with 15 assets;
  the release job has executed (see docs/ACCEPTANCE.md #13).

### §2.19 Future server-directed computation — **Partial** (boundary only — as specified)

**Implemented (boundary only, no engines — correct per spec).**

- `nse_bridge/tasks/`: generic envelope (`task_id` uuidv7, kind, params, `issued_at`,
  `expires_at`, `idempotency_key`, `resource_limits {max_cpu_s, max_memory_mb,
  timeout_s}`), validation mirroring §6.3 (malformed → expired → wrong-device →
  wrong-account → kind-allowlist → replay), bounded worker, result envelope mirroring
  `action.result` (`task.result`, `completed | failed | rejected`).
- **Kind registry starts EMPTY** (`tasks/registry.py`): registration is explicit code
  with a required approval reference; no dynamic import, no `eval`/`exec`, no
  download-and-run — arbitrary remote code execution is impossible by construction.
  Approved/trusted modules only.
- Not wired to the WS command path (correct — no wire format exists in CONTRACT v1).

**Partial / missing.**

- **Cancellation and resource-limit enforcement in the worker were not verified** in
  this audit (`tasks/worker.py`, 197 lines — envelope carries the limits; whether the
  worker enforces CPU/memory/timeout and cancellation was not read). Spec §19 requires
  task identification, authorization, input validation, execution/resource limits,
  cancellation, timeout, result serialization/delivery, and status reporting; the
  envelope + validation + worker skeleton cover most of these, but cancellation and
  limit enforcement need a verification pass before the boundary is claimed complete.
- The wire format itself is a contract dependency (§4 D8): until a future contract
  version registers kinds and a wire format, no server-assigned task can execute.

### §2.20 Testing & CI — real evidence required — **Partial**

**Implemented and verified.**

- **Unit tests: 311 passed** (`pytest tests/unit tests/integration -q`, run by this
  audit, 17.57 s, 0 failed). Coverage by file: REST client, WS lifecycle/backoff,
  auth, token handling, command validation + routing, idempotency, expiry, symbol
  getters/metadata, account mapping (sync), execution state machine, reconciliation,
  logging/redaction, diagnostic queue, retry policies, configuration, update
  verification, discovery (mocked paths/registry), version compat, task-boundary
  validation (unknown kind rejected), GUI screen assertions, test-honesty labelling.
- **Test honesty:** every test is labelled `unit` / `mock_integration` / `real_mt5` /
  `manual` (`tests/unit/test_test_honesty.py` enforces the labelling);
  `tests/conftest.py` FAILS loudly (never silently skips) when a required-MT5 test runs
  without a terminal. The 104 e2e tests collect on Linux but error at fixture setup —
  which proves honesty, not e2e success (docs/ACCEPTANCE.md #15, Not-proven).
- **Integration tests** vs the CONTRACT-v1 mock server (`scripts/mock_server.py` +
  `tests/integration/test_mock_server.py`, `test_enrolment.py`, `test_reliability.py`):
  REST + WS + heartbeat + reconnect + dedup + expiry + account-switch via
  `sync_request` + local API auth + chart handlers. Mock server implements all REST
  endpoints and WS chart answers.
- **CI** (`.github/workflows/build.yml`): `test-linux` (ruff on `tests/ + scripts/`,
  version-file freshness, OpenAPI freshness, unit + mock-integration tests, marker
  counts) → `build-windows` (same tests on Windows + PyInstaller exe + frozen-exe
  smoke check) → `release` (tags only: SHA-256 + manifest + attach). No slow Wine in
  mandatory checks (compliant with spec §20).

**Partial / missing.**

- **Windows CI fails at head `3f0950c`** at the "Run unit + mock-integration tests on
  Windows" step; root cause **undiagnosed** (job logs return 403 from this environment).
  The Linux win32 simulation reproduced only DPAPI-availability artifacts. This is the
  top blocking item for the PR: no green Windows run exists for this branch (§6).
- **Lint/format/static-analysis/security coverage is scoped, not whole-repo.** CI runs
  `ruff check tests/ scripts/` only. The workflow header documents the reason: `ruff
  check .` still reports **15 pre-existing findings** in `nse_bridge/engine`,
  `nse_bridge/mt5`, `nse_bridge/timeutil`, `nse_bridge/main.py` (see `docs/RELEASE.md`).
  There is **no formatting check** (no `ruff format --check` / black), **no static
  analysis** (no mypy/pyright job), and **no security check** (no bandit/pip-audit or
  dependency scan) in CI. Spec §20 requires all four.
- **Unit-test gaps for the new spec:** no tests exist for symbol resolution / ambiguity
  (nothing to test yet — §2.4), no test asserts `PARTIALLY_COMPLETED` (state does not
  exist — §2.7), no journal-event tests (§2.8), no Symbols/Execution GUI-screen tests
  (§2.17). Reconciliation and diagnostic-queue tests exist; diagnostic-*delivery*
  tests cannot exist until a delivery endpoint does (§2.12).
- **Integration gaps:** lost-acknowledgement and server-restart scenarios are covered
  only via the mock server's reconnect behaviour; there is no dedicated "server restart
  mid-session" integration test, and no live-Platform integration test exists (blocked
  by §4 D11).
- **MT5 tests (real):** the 104 e2e tests cover discovery (incl. multiple terminals),
  account/symbols/market/positions/orders/execution/modify/partial+full close/pending/
  history/reconciliation — but **none has ever run green** (no MT5 terminal on this VM
  or on GitHub-hosted runners; manual Windows-with-MT5 Demo run still owed). Demo-only,
  never real money — compliant by design.

**Headline per spec section (§§2–20):**

| Spec § | Verdict | Headline gap |
|---|---|---|
| §2 Comms | Partial | No request-cancellation API; no PUT/PATCH/DELETE call sites; no live-Platform proof |
| §3 Terminal discovery/mgmt | Partial | No data-directory field; single attached terminal (no independent per-terminal state) |
| §4 Broker & symbol intelligence | Partial | **No symbol-resolution abstraction, no ambiguity reporting, no mapping cache**; hardcoded probe symbols in driver |
| §5 Account info/sync | Partial | **Sync checkpoint is in-memory only** (lost on restart → full backfill); no Bridge-side demo gate (contract-aligned) |
| §6 Execution infra | Partial | Stop-limit pending types missing locally; open/cancel/pending-create/partial-close not sendable remotely (contract) |
| §7 Lifecycle | Partial | **`PARTIALLY_COMPLETED` state does not exist** (nor a wire status for it) |
| §8 Journal/events | Missing | **No journal subsystem at all**; no event stream; no upload channel in contract either |
| §9 Accounting source data | Partial | No MT5-vs-derived labelling; no explicit deposit/withdrawal/fee surfacing |
| §10 Market data | Partial | Server control exists only via chart frames; no book/tick subscription channel (contract has none) |
| §11 Live-state monitoring | Partial | Polling only (no MT5 push exists); no server-visible state-change channel |
| §12 Telemetry/logging | Partial | Local pipeline complete; **delivery impossible — no endpoint in CONTRACT v1 §4.2**; queue drains by TTL |
| §13 Self-healing | Partial | **15 of 20 scenarios**; account/broker/symbol/queue-full/update-fail entries missing |
| §14 Security | Implemented | v1.1 items outstanding only (per-device credential, per-command signature); no local kill switch |
| §15 Resources/performance | Partial | Bounded by construction; **no measured performance evidence** |
| §16 Storage policy | Partial | Checkpoint not persisted; no written retention policy for local files |
| §17 GUI | Partial | **Symbols + Execution screens missing** (6 of 7 sections) |
| §18 Updates | Partial | **Signing key unprovisioned (updater cannot install)**; no rollback strategy; no `.ico`; no installer |
| §19 Future compute | Partial | Boundary exists, kinds empty, not wired to WS; worker cancellation/limit enforcement unverified |
| §20 Tests & CI | Partial | 311 tests pass on Linux; **Windows CI fails (cause undiagnosed)**; lint scoped to tests/scripts; no format/static-analysis/security jobs; e2e never run green |

## §3 Full-contract coverage (CONTRACT v1, §§0–19)

One entry per contract section. "Bridge implements" = code in this repo;
"Deliberately not" = server-side by contract (not a gap); "Missing" = Bridge-side work owed.

### §0 Sources and verification log
- **Bridge:** n/a (contract provenance). The Bridge targets the CONTRACT v1 *document*
  (merged via Platform PR #102), not the Rust `NSE-Connector` code the contract was
  reconciled against, and not Platform PR #103/#107 branches. Recorded in
  `docs/PLATFORM-GAPS.md` §1 and `ARCHITECTURE-CONTRACT.md` §1.

### §1 Principles
- **Bridge implements:** thin-client rule (no strategy/AI/risk-decision/signal/journal
  logic in the Bridge — local AI product deleted in Phase 3; risk calculators are pure
  math for local validation only); delivery-is-not-execution (local re-authorization
  per §6.3); secure defaults (entitlements default disabled — `rest.py` `Entitlements`
  defaults `false`/`0`); no silent loss (bounded queues evict oldest-with-warning).
- **Deliberately not:** "MT5 MCP credential never leaves the machine" — n/a to the Bridge:
  the Bridge uses the MetaTrader5 Python package, not the MCP transport, so no MCP
  credential exists here (see §8).
- **Missing:** none at principle level. Note: demo-only enforcement is server-side (§7.4).

### §2 Identities and authentication
- **Bridge implements:** API key treated as an opaque Bearer token, never parsed, never in
  a body/query/log (redaction in `logging_setup.py`); device UUID generated once and
  persisted in the secret store; enrolment via `register-device`; session lifetime
  honoured via `session_expires_at` (`session/refresh` + `session/revoke` implemented);
  entitlements consumed from `validate-key` with secure defaults; 401/403 stop sync
  immediately with distinct user-facing states; 429 backoff (no tight loop).
- **Deliberately not:** key creation/listing/rotation UI is website-side (§3.5 management
  endpoints); MCP client credentials (§2.3) are for `POST /mcp` clients, not the Bridge;
  scopes are server-enforced (the Bridge does not parse scopes beyond entitlements).
- **Missing:** per-device credential exchange (v1.1, D-AUTH-1) — Bridge sends the API key
  on every call, per contract; key *rotation* UX is "enter a new key" (no dedicated
  rotate flow in the GUI beyond settings + `reset_for_new_key()`).

### §3 REST surface
- **Bridge implements:** all device endpoints — `validate-key` (§3.1), `register-device`
  (§3.2, idempotent, re-enrolment after any 401), `sync/batch` (§3.3 — flat wire shape,
  exactly the four kinds, broker timestamps verbatim, checkpoint never backwards, page
  500 / 250 ms, pending queue bound 32, ack handling), `heartbeat` (§3.4),
  `session/refresh` + `session/revoke` ([client-v1.1]), `releases/latest` (§3.5),
  `manifest` POST (§4.1, [client-v1.1]) as a *method*, error contract §3.6 (mapped
  codes; unknown 401/403 code = malformed, never success).
- **Deliberately not:** §3.5 management endpoints (api-keys CRUD/rotate, devices
  list/revoke, accounts status) — website session-auth, not device calls.
- **Missing / unwired:** **`RestClient.send_manifest()` is never called at runtime.**
  `build_manifest_payload()` exists on the channel (`channel.py:761-763`) but has no
  caller, and no `manifest.update` WS frame is ever sent. The manifest builder
  (`server/manifest.py`) and the POST method (`rest.py:451-459`) are dead code paths
  today. Wiring (post on enrolment + on manifest change, with unchanged-hash no-op
  semantics) is Bridge work, not a contract change. Also: `pending_commands > 0` → WS
  open/refresh behaviour was not verified (see §2.2).

### §4 Manifest and diagnostics
- **Bridge implements:** manifest builder with only truthfully mappable fields
  (`device_id`, `manifest_hash` over the local 90-command registry, `terminal_build`,
  `action_count`, action names — `server/manifest.py:71-86`); documented mapping/omission
  (the hash is over the *local registry*, not the MCP `tools/list` manifest — see §8).
- **Missing:** manifest POST wiring (see §3 above); `manifest.update` WS frame never sent.
- **Deliberately not / gap:** §4.2 diagnostics — the contract defines no diagnostics
  endpoint; the server derives health from `last_seen_at`, WS liveness, last batch, and
  manifest. Bridge-side: local pipeline + inert adapter only (§2.12). Contract
  dependency D1.

### §5 WebSocket
- **Bridge implements:** upgrade with `Authorization: Bearer` + `X-NSE-Device-Id`,
  `wss://` mandatory in production, no `hello` frame; server→client frames `command`,
  `sync_request`, `ack`, `heartbeat`, `chart.request/subscribe/unsubscribe` (§5.2);
  client→server frames `heartbeat` (15 s, `device_id`), `action.result`, `chart.data`,
  `chart.tick` (§5.3); unknown frames ignored, never fatal; reconnect = fresh session,
  backoff 500 ms doubling → 30 s + jitter, reset on success, clean `close()`; server
  re-sends only `pending` (client also dedups); outbound bound 128, results first,
  chart frames second, oldest chart frame dropped with a warning when full.
- **Deliberately not:** `ping`, `account.snapshot`, `account.event`, `command.ack`,
  `account.switch_required` frames — none exist in v1 (deltas D-WS-1/2/3/5).
- **Missing:** none in the WS protocol itself. Live behaviour against the real Platform
  is unproven (mock only).

### §6 Commands
- **Bridge implements:** allowlist exactly `close_position`, `modify_sl_tp`,
  `modify_pending` (§6.1) with the §17.4 capability gate starting CLOSED
  (`modify_pending_proven=False`, `commands.py:346-353,386-390`); envelope §6.2
  (uuidv7 `command_id`, `account_id` = MT5 login string, `device_id`, operation object,
  `issued_at`/`expires_at`, `idempotency_key == command_id`); validation order §6.3
  (malformed → expired → wrong-device → wrong-account → allowlist → entitlement →
  replay; only success consumes the replay id; replay cache 10,000); results §6.5
  (`MessageEnvelope`, `protocol_version "1.0"`, `schema_version "2026-11-17"`,
  statuses `rejected | executed | mcp_failed`, `correlation_id = command_id`,
  client-measured `duration_ms`, raw MT5 payload preserved, `error` with `retry_safe`
  only on failure; offline persistence in the bounded durable queue, delivered on
  reconnect); round-trip invariant (exactly one terminal state, never executes twice).
  Local mapping: `close_position` → close-by-ticket, `modify_sl_tp` → SL/TP by ticket,
  `modify_pending` → `modify_pending_order_sltp` (`trading.py:287`).
- **Deliberately not:** server-side authorization §6.4 (kill switch, entitlement/scope,
  Risk Engine, signal-execution setting, grants, AI-origin gates) — Platform-side.
  Per-command signatures — v1.1 (D-CMD-3).
- **Missing:** `PARTIALLY_COMPLETED` has no wire representation (§2.7); `modify_pending`
  close-out (live proof + published minimum version) is owed (§17.4); `open_order` /
  `cancel_order` are not sendable (contract B rows — §4 D2/D3).

### §7 Accounts
- **Bridge implements:** account identity from the local snapshot (login, server, broker,
  name, type demo/live/contest, currency, balance/equity/margin/margin_free/profit/credit,
  read-only/margin-mode flags, terminal context verbatim); device bound to exactly one
  active account; binding established by the latest `account`-kind batch; account-switch
  handling (§7.3: stop tagging old-account events, invalidate pending old-account
  commands, fresh sync `account → positions → history`, never execute an old account's
  command on the new account — enforced by the local `WrongAccount` check and the
  channel's switch detection).
- **Deliberately not:** `terminal_id` — optional and unset in v1 (D-ACC-1); `device_id`
  is the stable identity. Demo-only refusal — server-side (§7.4); the Bridge parses
  `trade_mode` but enforces nothing.
- **Missing:** no Bridge-side demo gate (defined follow-up, not v1 requirement).

### §8 MT5 local method — **deliberate transport difference (not a bug)**
- **The contract's method is the local MT5 MCP server** (`http://127.0.0.1:22346/mcp`,
  JSON-RPC 2025-06-18, 107 actions, tier model Tier 0–3, six Tier-3 actions, dispatcher
  gate chain, kill-switch scopes). **The Bridge does not use it.** The Bridge uses the
  official MetaTrader5 Python package (C++ IPC, serialized worker thread) and maps the
  contract ops onto it. This difference is documented in `AUDIT.md` §2 and
  `ARCHITECTURE-CONTRACT.md` §5, and is the correct standing decision (do not rewrite
  working IPC; do not ship an EA/file bridge).
- **Consequences (recorded, not hidden):**
  1. The Bridge's local action surface is the **90-command registry**, not the 107-action
     MCP manifest. The manifest hash is over the local registry, not `tools/list`.
  2. The **tier model does not exist** in the Bridge: there is no Tier 0/1/2/3
     classification, no `local.modify` / `advanced.execute` / `trading.execute`
     permission strings, and no dispatcher gate chain beyond §6.3 validation.
  3. **No local kill switch** exists in the Bridge (see §2.14).
  4. MCP-specific behaviours have no Bridge equivalent: `Mcp-Session-Id` handling,
     404 session recovery, JSON-in-text payloads, `tools/list` discovery, the
     Market-Watch prerequisite chain as an MCP concept (the Bridge has its own local
     equivalent in the chart handler), market-book subscribe/unsubscribe idempotency
     quirks (the Bridge's `market_book` equivalent is `get_market_depth` via the Python
     package).
  5. The contract's "known MT5 data gaps" (§8.3) are stated against the MCP surface.
     Against the Python driver, some differ: e.g. pending-order price/expiration
     modification **is** implemented locally (`trade_modify_pending_order`,
     `TRADE_ACTION_MODIFY`, `orders.py:404-460`), and by-ticket lookups **are**
     implemented as derived filters (`_fetch_order`, `_find_order_everywhere`). The
     contract's F rows (§18.14) must therefore not be read as Bridge limitations — see
     §3 §18 notes.
  6. The Bridge MUST NOT ship an MQL5 EA, write into any terminal data dir, or install
     anything into MT5 — complied with (discovery is read-only; no EA/file-bridge code
     exists).

### §9 Platform MCP server (`POST /mcp`)
- **Bridge:** not applicable. The Bridge must not use `POST /mcp` for sync or anything
  else — complied with (no MCP client exists in the Bridge). All eight tools
  (`nse.get_account` … `nse.request_trade`) are Platform-side.

### §10 Versioning and OpenAPI alignment
- **Bridge implements:** `protocol_version "1.0"` + `schema_version "2026-11-17"` in every
  `action.result` (`commands.py:152-153,470-471`); `/api/v1/` REST prefix; `connector_version`
  (`BRIDGE_VERSION = "1.0.0"`, `nse_bridge/__init__.py:10`) sent in `validate-key` /
  `register-device`; server minimum-version refusal (`feature_not_enabled`) surfaced via
  the error contract; no faked version.
- **Deliberately not / n/a:** MT5 MCP protocol pinning (`2025-06-18`) — no MCP transport;
  OpenAPI annotation of Platform routes — Platform-side.
- **Missing:** none. Note: any future allowlist widening must bump `schema_version` per
  contract — the Bridge has no version-bump procedure documented for that event (§6).

### §11 Platform implementation (migration 0054)
- **Bridge:** not applicable (Platform-side). Bridge impact is limited to §4 D11:
  the #103/#107 duplication blocks live integration testing, and the Bridge must not
  assume any Platform table, route, or migration behaviour beyond the contract document.
- **Note for the record:** contract §19 (as-built Platform record) states the Platform
  registry holds 72 operations with 9 Connector-allowlisted today and `open_order`
  hard-blocked (`open_order_not_approved`) — consistent with the Bridge's closed gates.

### §12 Connector (Windows app) — target behaviour
- **Bridge implements (adapted to the Python stack):** API-key screen with masked key +
  validate (GUI Settings/Connection + `POST /account/validate-key`); MT5 discovery
  (registry/App Paths/standard paths/custom/portable/running processes, `cfg(windows)`-
  gated with Linux stubs so CI compiles — discovery functions guard `winreg`/process
  APIs by platform); diagnostics screen with per-item states and guided codes;
  Run/Stop semantics (channel start/stop, clean WS close, stop does not revoke key/device);
  secrets via keyring/DPAPI (service entries for the API key, device id, local API key);
  login masking in diagnostics; auto-update via `releases/latest` with SHA-256 + Ed25519.
- **Deliberately different:** the GUI is a pywebview Control Center (6 screens), not the
  contract's eframe/egui wizard; the "MCP config / test / discover-tools" screen does
  not exist (no MCP transport — see §8). MT5 installer link / Download for missing
  terminals: not verified in the GUI (diagnostics guide the user; no installer download
  was found).
- **Missing:** installer (WiX/NSIS) documentation + build script (§2.18); `.ico` (§2.18);
  update public key provisioning (§2.18).

### §13 Testing (conformance)
- **Bridge implements (client side):** mock-verified suites for key validation error
  matrix, discovery (mocked paths/registry), diagnostics states, download checksum
  failure, Run/Stop, account switch, MT5 restart, disconnect/reconnect/resume,
  duplicate events, expired command, ack/result round-trip (per contract §13.1, as
  applicable to the Python Bridge). Required additions status: manifest POST shape —
  method implemented, **not wired** (§3 §4.1); `session/refresh` + `session/revoke` —
  implemented + mock-covered; `sync_request` handling — implemented + tested;
  pending-command replay on reconnect — implemented (replay cache + server re-send of
  `pending` only); heartbeat 15 s / 45 s death detection — implemented + tested;
  demo-account refusal surfacing — server 403 path mapped, **no live proof**;
  per-command `action.result` delivery over WS with the offline queue — implemented +
  mock-tested. §17 additions: chart handlers implemented + unit/mock-tested
  (`tests/unit/test_server_chart.py`); `modify_pending` allowlist extension — **not done**
  (gate closed; tests assert rejection, which is the correct current behaviour).
- **Deliberately not:** Platform-side §13.2 suites (migrations, tenant isolation, MCP
  auth, journal consistency) — not Bridge work.
- **Missing:** live-MT5 proof for all of the above (manual Windows + Demo run owed);
  a dedicated server-restart integration test; diagnostic-delivery tests (blocked by D1).

### §14 Deltas register (normative)
- **Bridge honours:** D-REST-1 (`/api/v1/connector/*`), D-REST-2 (`register-device`),
  D-SYNC-1 (flat batch wire shape), D-SYNC-2 (no per-batch `account_id`),
  D-CMD-2 (`CommandEnvelope`, not `action_name/parameters`), D-CMD-3 (no per-command
  signature), D-WS-2 (no `hello`), D-WS-3 (no `ping` / `account.switch_required`),
  D-WS-4 (heartbeat 15 s / death 45 s), D-WS-5 (fresh-session reconnect),
  D-MT5-1/2 (no EA/file bridge; MCP adapter redefined — Bridge equivalent: keep the
  Python driver, document the difference), D-ACC-1 (`terminal_id` unset),
  D-WS-6 (chart data over WS, §17.1), D-CMD-4 (`modify_pending` promoted,
  capability-gated), D-INV-1 (inventory is the target surface; D-CMD-1 survives as a
  sequencing rule), D-INV-2 (`open_order` defined, pending user decision),
  D-INV-3/4 (pending price/expiry unverified in MCP; partial close hedging-only).
- **Missing:** none in delta compliance. Note D-INV-3's MCP-coverage gap does not apply
  to the Python driver (see §8 consequence 5) — but the Bridge still must not expose
  pending price/expiry modification remotely until the contract defines the op (§4 D4).

### §15 Ambiguities and open items
- Still open on the Bridge side, carried forward: item 8 (manifest hash per terminal
  build — the Bridge hashes the local registry, so a terminal-build change does not
  change the Bridge manifest hash; the server must key availability by
  `(terminal_build, manifest_hash)` and must not compare against the contract's
  `b3df4d9fb2a3b7e0` MCP hash); items 9/10 (per-command signature, per-device credential
  — v1.1, both sides); item 11 (order placement — open user decision); item 12
  (`modify_pending` live proof); item 15 (§18 close-outs); item 16 (`open_order` user
  decision). Items 1–7 are resolved in the contract and honoured by the Bridge.

### §16 Verification and conformance notes
- **Bridge conformance status:** wire shapes in `rest.py` / `channel.py` / `commands.py`
  are byte-compatible with §3/§5/§6 (verified by inspection + mock tests); no EA/file
  bridge; GUI last (done, 6 screens). §17/§18 additions: chart handlers done;
  `modify_pending` allowlist extension + tests + live proof **pending** (§17.4 close-out);
  per-B-row allowlist extensions with market-open proof + published minimum version
  **pending** (§18.17); §18.16 unverified input schemas — the Bridge's Python-driver
  equivalents are known from the MT5 package, but the MCP `input_schema` values are
  not the Bridge's to record.
- **Not run/verified:** no live MT5 terminal on this VM; no live-Platform run; no
  Windows CI green run at head.

### §17 Chart data, AI Agent configuration, decision audit, `modify_pending`
- **§17.1 Chart protocol — Bridge implements.** `server/chart.py` (608 lines) +
  `channel.py:715-747` + `engine/charts.py` + `engine/market.py`: `chart.request`
  (M1..MN1, count ≤ 1000, 30 s timeout, retry once with a NEW `request_id`, duplicate
  `request_id` answered from the in-memory cache), `chart.subscribe` (max 20/device,
  poll 5–3600 s, default 15, deltas only, 21st refused with `subscription_limit`),
  `chart.unsubscribe` (unknown id = no-op), `chart.data` / `chart.tick` (≤ 1000 per
  frame, batched per poll cycle, broker time verbatim + UTC `received_at`),
  60 chart-frames/min sub-budget, error codes per §17.1.4 (all eight:
  `symbol_not_found`, `symbol_not_in_market_watch`, `unsupported_timeframe`,
  `market_closed`, `chart_timeout`, `rate_limited`, `subscription_limit`,
  `manifest_missing_action`), Market-Watch prerequisite chain (add first, cleanup
  optional/skipped when blocked), no indicator math in the client (raw candles/ticks/
  specs only). Subscriptions die with the WS session; re-subscribe is a fresh
  subscription (new id). **Unproven live** (mock only); the `manifest_missing_action`
  code path keys off the local registry manifest, not an MCP manifest (see §8).
- **§17.2 AI Agent configuration API — deliberately not (server-side).** No Bridge code;
  correct. The Bridge must never compute indicators or decide.
- **§17.3 Decision audit trail — deliberately not (server-side).** No Bridge code; correct.
- **§17.4 `modify_pending` — contract-defined, capability-gated, NOT sendable.**
  Bridge implements the gate (starts CLOSED, `modify_pending_proven=False`) and the
  local execution mapping (`modify_pending_order_sltp` via the Python driver's
  `TRADE_ACTION_MODIFY` equivalent for SL/TP). Close-out owed (§17.4 a–d): (a) allowlist
  extension — the op is already in `SERVER_ALLOWLIST` but gated, so (a) is effectively
  the gate flag; (b) tests — rejection tests exist, pass-tests do not (gate closed);
  (c) live proof at market open that a pending-order ticket is accepted — **not done**;
  (d) published minimum `connector_version` — **none published** (no version qualifies).
  Until (c)+(d), the server must not send it, and the Bridge correctly rejects it.
- **§17.5 `open_order` — OPEN DECISION, Bridge side correct.** The server must not send
  it; the Bridge's allowlist excludes it; local market/pending commands exist as *local*
  capability only. Unblocking requires, in order: user approval, allowlist addition +
  tests, Risk Engine + financial-consent proof on demo, `schema_version` bump.

### §18 Full operation inventory (123 rows)
Bridge-side mapping. Status letters are the contract's; "Bridge" says what this repo does.

| Contract rows | Count | Bridge position |
|---|---|---|
| A — Connector-allowlisted now (18.8.9, 18.9.1, 18.9.7) | 3 | **Implemented.** Via two envelope ops (`close_position`, `modify_sl_tp`); 18.9.7 (remove SL/TP) rides `modify_sl_tp` with null SL/TP — null-vs-0 removal semantics are live-pending per contract §18.9.7 and unproven in the Bridge. |
| B — Contract-defined, pending close-out | 21 | **Mixed — see below.** |
| C — Read-only, Connector-executable now | 42 | **Implemented locally** via the 90-command registry + sync batches + chart frames (see per-section notes). |
| D — Local-only terminal operations (18.13) | 44 | **Deliberately not exposed.** The Bridge's Python driver has no chart-UI / alerts / profiles / tester / file / service operations to expose; the contract's D rows are MCP actions with no Bridge equivalent. Nothing to do — except: the Bridge must never *add* such operations to the server channel. |
| E — Derived / Platform-side | 9 | **Deliberately not (server-side).** Bridge supplies the raw inputs (account snapshot, positions, deals, symbol specs, calc tools). Note: margin level and leverage are computed in `engine/account.py` for *local* display/validation — that is local derivation, not a second accounting engine. |
| F — Gap / not available | 4 | **MCP-surface gaps, not Bridge gaps.** 18.3.6 (symbol sessions — the Bridge's `get_symbol_trading_sessions` returns what MT5 reports; explicit session times are not in the symbol record), 18.7.5 (per-deal commission/swap — flows through only if MT5 reports it), 18.7.6 (SL/TP on history — absent by construction), 18.9.5 (pending volume change — MQL5 forbids; the Bridge's local `trade_modify_pending_order` *sends* volume in its `TRADE_ACTION_MODIFY` request, which the broker will reject or ignore for a volume change — **local correctness question, flag for the execution agent**: either drop `volume` from the modify request or document the broker behaviour). |

**B rows (21) — Bridge-side detail:**

- **Chart frames (5: 18.2.1–18.2.5): Implemented locally, unproven live.** Handlers, caps,
  errors, dedup, and prerequisite chain exist (`server/chart.py`); the B status persists
  until live proof + Platform send-side close-out.
- **`open_order` variants (8: 18.8.1–18.8.8): local capability exists, remote blocked.**
  Market BUY/SELL and 4 of 6 pending types exist locally (§2.6); stop-limit pending
  types are missing locally too. Remote send requires the §17.5 user decision.
- **Partial close (18.8.10): local capability exists, remote shape missing.** Local
  `close_position(volume=…)` works; the §6.2 envelope has no `volume` field (§4 D5).
  MQL5 constraints (hedging-only; oversize volume closes in full; netting = opposing
  deal) are **not enforced** by the Bridge — the server must check margin mode first,
  per contract.
- **Close-all (18.8.11): composite, Platform-orchestrated.** No Bridge change needed
  beyond per-ticket closes; the Bridge must report per-ticket outcomes (it does, one
  `action.result` per command).
- **Close-by (18.8.12): local capability not found.** No `close_by_position` /
  `TRADE_ACTION_CLOSE_BY` path was found in `engine/trading.py` or `positions.py`.
  **Missing locally** (in addition to the contract close-out).
- **Order modification (4: 18.9.2, 18.9.3, 18.9.4, 18.9.6):** 18.9.2 (`modify_pending`
  SL/TP) — local mapping exists, gate closed (§17.4). 18.9.3/18.9.4 (pending price /
  expiration) — **local capability exists** (`trade_modify_pending_order`,
  `TRADE_ACTION_MODIFY`) via the Python driver, but the contract records MCP coverage
  as unverified and defines no server op; remote exposure needs a contract op first
  (§4 D4). 18.9.6 (`cancel_order`) — **local capability exists**
  (`trade_cancel_pending_order`, `orders.py:354`); remote send blocked by contract.
- **Manifest POST (18.12.4): method implemented, never called** (§3 §4.1). Bridge wiring
  owed, not a contract change.

**§18.16 unverified items:** the six Tier-3 MCP input schemas, pending price/expiry MCP
coverage, calc/check full schemas, the 44 local-only action schemas, and the full
107-name manifest are MCP-surface items the Bridge cannot verify (no MCP transport).
The Bridge's Python-driver equivalents are known from the MetaTrader5 package, but
recording them as contract input schemas would be inventing contract surface — not done.

### §19 Platform as-built conformance
- **Bridge:** not applicable (Platform-side record). Consistent with the Bridge's
  closed gates: the Platform registry's `open_order` hard-block (§19.4) and
  `modify_pending` gate with minimum version `None` (§19.3) match the Bridge's
  `modify_pending_proven=False` default. No Bridge action arises from §19 except D11.

## §4 Contract dependencies (explicit list)

Every capability the spec requires that CONTRACT v1 does not expose. Nothing here invents
an endpoint, frame, payload, event name, or auth scheme. Each entry states the contract
section that does not define it, and the Bridge-side boundary/interface implemented instead.

| # | Required capability (spec ref) | Not in CONTRACT v1 | Bridge-side boundary implemented instead |
|---|---|---|---|
| D1 | **Diagnostics / events upload endpoint** — ship ERROR/WARN (and meaningful INFO) diagnostic events to the Platform (spec §12; acceptance items "ERROR and WARN events reach the server reliably", "diagnostic delivery survives temporary network failures") | **CLOSED on the Bridge side by P5a (audit P2-10 refresh).** CONTRACT §4.2 amended: the endpoint is `POST /api/v1/connector/diagnostics` (Platform `connector::diagnostics`, migration 0095; wire shape `{ "events": [...] }`, 1–100 events, server answers `{ "accepted": <n>, "stored": <n> }`; durable budget `diagnostics` 30/min per device). | Local pipeline complete + sink WIRED: `HttpDiagnosticsSink` (`nse_bridge/api/app.py:203–216`), ERROR immediate drain + 60 s periodic drain, over the durable queue (`api/app.py:175–180`). **Still owed:** live ingest proof against the deployed Platform (D11); unit tests prove the sink against `httpx.MockTransport` only. |
| D2 | **`open_order` sendable** — market BUY/SELL + all 6 pending types via the server channel (spec §6; contract §17.5/§18.8.1–18.8.8, status B) | **Not sendable in CONTRACT v1 §6.1.** `open_order` (`trade_send_market_order`, `trade_send_pending_order`) is explicitly NOT sendable; §17.5 makes it an OPEN DECISION for the user. No wire shape beyond the generic envelope is defined for it. | Local capability exists (`execution.py`, `trading.py`, `orders.py`); server channel allowlist excludes it (`commands.py:158`). **Required:** (1) user's explicit approval, (2) allowlist addition + tests, (3) Risk Engine + financial-consent gate proof on demo, (4) `schema_version` bump (§10). |
| D3 | **`cancel_order` sendable** — cancel a pending order by ticket (spec §6; contract §18.9.6, status B) | **Not sendable in CONTRACT v1 §6.1.** `cancel_order` (`trade_delete_order`) is explicitly NOT sendable in v1. | Local capability exists (`trade_cancel_pending_order`, `orders.py:354`); server channel excludes it. **Required:** allowlist extension + demo proof + published minimum version (same close-out pattern as D4). |
| D4 | **`modify_pending` close-out + pending price/expiration modification** (spec §6; contract §17.4, §18.9.2–18.9.4) | **§17.4 capability-gated, not sendable.** The op is contract-defined (SL/TP only) but no version qualifies (published minimum version: none). **§18.9.3/18.9.4 (price/expiration): no verified MT5 MCP action exists** in the live manifest ("possible gap", §18.16 item 2); no server op is defined for them. | SL/TP: gate starts CLOSED (`modify_pending_proven=False`); local mapping exists (`modify_pending_order_sltp`). Price/expiration: local capability exists via the Python driver (`trade_modify_pending_order`, `TRADE_ACTION_MODIFY`) but is **not** exposed remotely — the server channel maps `modify_pending` to SL/TP only. **Required:** (a) market-open live proof that a pending-order ticket is accepted, (b) published minimum `connector_version`, (c) for price/expiration, a contract op + MCP-coverage proof (or a contract statement that the Python-driver capability is the reference). |
| D5 | **Partial-close and close-by via the server channel + `PARTIALLY_COMPLETED` lifecycle state** (spec §§6–7; contract §18.8.10, §18.8.12) | **No wire shape.** The §6.2 envelope carries only `{type, ticket, sl?, tp?}` — no `volume` field for partial close, no `close_by_ticket` field for close-by. Contract §6.5 `action.result` statuses are exactly `rejected \| executed \| mcp_failed` — **no partial status exists on the wire**, so the Bridge's `PARTIALLY_COMPLETED` state (a local terminal state, `commands.py:421`) has nowhere to be reported. | Local partial close exists (`close_position(volume=…)`); close-by exists locally too (`trade_close_position_by`, `engine/positions.py:306`). Reconciliation (`_reconcile`) can prove a reduced volume on read-back. **Required:** a contract amendment adding `volume` (partial close) and `close_by_ticket` (close-by) to the envelope, and a wire representation for partial completion (new `action.result` status or a `partial` payload marker) — plus the user/Platform decision on hedging-only semantics (MQL5 `PositionClosePartial` constraints; netting = opposing deal). |
| D6 | **Journal / execution-event upload** — discrete events (order submitted/accepted/rejected, position opened/modified/closed, deal executed, broker error) with the spec §8 field set (spec §8) | **Not in CONTRACT v1.** No journal/event endpoint or frame exists. The only sync kinds are `account \| positions \| history_orders \| history_positions` (§3.3); WS client→server frames are only `heartbeat`, `action.result`, `chart.data`, `chart.tick` (§5.3). | Journal collector exists AND is wired in production (§2.8): `JournalCollector` (`journal/collector.py:324`) with event-level dedup keys (`journal/events.py:71`), constructed in the `create_app` lifespan and assigned to `SyncEngine.journal`, pinned by `tests/unit/test_production_wiring.py`. History batches carry the raw facts (orders/deals/closed positions) from which the Platform derives journal entries server-side. **Required:** a contract amendment defining a journal/event channel (or a formal statement that journal derivation from sync batches is the intended design, in which case the Bridge builds only the local event collector for GUI/diagnostics, not an upload path). |
| D7 | **Symbol inventory / metadata sync + symbol-resolution service** (spec §4) | **Not in CONTRACT v1.** No sync kind carries symbols; no endpoint accepts a symbol inventory, metadata snapshot, or resolution request/response. The only symbol data path is §17.1 chart frames (per-symbol, on request/subscription). | Resolver exists (§2.4): `SymbolResolver` (`symbols/resolver.py:140`, ambiguity reported, never guessed), `SymbolInventory` (`symbols/inventory.py:81`), `SymbolMetadata` (`symbols/metadata.py:82`); 29 tests in `tests/unit/test_symbol_resolution.py`. **Required:** a contract decision on how the server learns the broker's symbol inventory — e.g. a new sync kind, a chart-frame extension, or "on-demand only via chart frames" (in which case the resolver is still Bridge work, but the server never receives a full inventory). |
| D8 | **Accounting source-data channel beyond the four sync kinds** (spec §9) | **Partially in CONTRACT v1 §3.3.** Balance/equity/margin/credit/deals flow inside `account` / `history_*` batches, but there is no dedicated accounting snapshot kind, no deposit/withdrawal/adjustment/fee surfacing, and no MT5-vs-derived labelling rule on the wire. | Raw values flow in existing batches; no labelling, no dedicated surfacing (§2.9). **Required:** a contract statement on whether the four kinds suffice (likely yes, with a labelling convention added to the batch payloads) or a new kind/field set. |
| D9 | **Future server-assigned compute wire format** (spec §19) | **Not in CONTRACT v1.** No frame, endpoint, or kind registry for tasks exists anywhere in the contract. | Local boundary only: `nse_bridge/tasks/` (envelope, validation, bounded worker, EMPTY kind registry), not wired to the WS command path. **Required:** a future contract amendment registering task kinds and their wire format. Approved/trusted modules only — never arbitrary remote code execution. |
| D10 | **Per-device credential exchange + per-command signatures** (contract §2.2 D-AUTH-1, §6.2 D-CMD-3 — v1.1) | **Deferred in CONTRACT v1.** Both are defined as v1.1 follow-ups; the `register-device` response is extensible and the `signature` field name is reserved, but neither mechanism exists. | Bridge sends the API key as Bearer on every call; commands are authenticated by the WSS session + server audit trail. No action until the Platform defines both mechanisms. |
| D11 | **Live Platform deployment + migration 0054 resolution** (spec §§23–25 acceptance) | **Not a contract-surface item; a Platform delivery item.** Platform PR #103 (frozen) and PR #107 (open) both claim migration 0054; live `bridge.nexusverse.space` behaviour is unverified from this environment. | Bridge targets the CONTRACT v1 document only; all integration tests run against the mock server. **Required from Platform:** resolve the 0054 duplication, merge/deploy a build carrying the contract, then live verification (enrol → sync → command → result → reconnect → no-duplicate) against the deployed endpoint. |
| D12 | **Market-book / order-book data channel + terminal journal / economic calendar / news sync** (contract §18.2.8–18.2.10, §18.12.2/5/6, status C) | **Not in CONTRACT v1 as a server channel.** These are Connector-executable reads with no defined server delivery path (chart frames carry candles/ticks only; sync batches carry account/positions/history only). | Local capability exists (`get_market_depth`, diagnostics commands 82–90). **Required:** a contract decision on whether the server needs book/journal/calendar data at all; if yes, a defined channel. Until then, these stay local-only. |
| D13 | **Per-deal commission/swap/fee, SL/TP on historical trades, explicit symbol session times, by-ticket lookup** (contract §18.14 F rows) | **Not available in the MT5 MCP surface** (verified absent, §18.7.5/18.7.6, §18.3.6, §18.5.2/18.6.2). The contract records them as explicit gaps with workarounds (account-level aggregates, NULL + "not reported by broker", derived filters). | Partially different on the Bridge: by-ticket lookups exist as derived filters; per-deal commission/swap flows through only if MT5 reports it; SL/TP on history is absent by construction; session times are limited to what the symbol record carries. **Required:** nothing from the Platform — but the Bridge must not promise these fields, and any server request for them must receive the contract's stated workaround, not invented values. |

**What is deliberately NOT a dependency.** The following are sometimes mistaken for
contract gaps but are resolved inside the Bridge or by contract design, and need no
Platform action: the 3-op allowlist itself (§6.1 — sequencing, not a missing definition);
`terminal_id` (D-ACC-1, optional/unset by design); the MCP-vs-Python-driver transport
difference (§8 — documented standing decision); the §12 INFO policy (Bridge policy
applies; the Platform no-INFO rule does not — PLAN addendum #2); the task-boundary wire
format's *local* half (built; only the wire half is D9).

---

## §5 Re-sequenced implementation order (spec §21, 10 areas)

Sequencing principle: **contract-conformant foundation first, then the new-spec
capabilities in dependency order, with GUI and tests running alongside — never as a
final big bang.** Areas that touch disjoint files run in parallel; areas that share the
command path, the lifecycle, or the GUI are sequenced. "Area" numbers are spec §21's.

### Phase A — Unblock the PR (no new features; must land first)

| # | Area (spec §21) | Work | Depends on | Parallel with |
|---|---|---|---|---|
| A1 | 10 Testing & CI (+ 1 Architecture) | Diagnose + fix the failing Windows CI test step at head `3f0950c` (cause currently undiagnosed — see §6 Q1). Expand lint to the whole repo (clear the 15 pre-existing `ruff` findings or scope-waive them explicitly); add format check, static analysis (mypy), and a security check (bandit/pip-audit) to CI. | — | A2 |
| A2 | 8 Security (+ 1) | Provision the release-signing public key (operator action) and verify an end-to-end staged update refuses/installs correctly; add the `.ico` asset (operator-supplied); document the rollback/recovery strategy for updates (§2.18). | A1 (CI must be green to prove it) | A1 |

### Phase B — Foundation gaps that everything else builds on

| # | Area | Work | Depends on | Parallel with |
|---|---|---|---|---|
| B1 | 2 REST + WS (+ 1) | Wire the manifest POST (call `send_manifest()` on enrolment + on manifest change; unchanged-hash no-op) and the `manifest.update` WS frame; verify `pending_commands > 0` → WS open/refresh; add request-cancellation support (spec §2). Persist the sync checkpoint to disk (same JSONL/bounded pattern as the result store). | A1 | B2, B3 |
| B2 | 3 MT5 driver + terminal mgmt | Add terminal **data-directory** detection to `TerminalInfo`; close the driver probe-symbol deviation (§2.4 — replace hardcoded probe list or document the exception). | A1 | B1, B3 |
| B3 | 7 Logging / telemetry / diagnostics | Add the 5 missing self-healing scenarios (§2.13: account disconnect, broker connection change, symbol unavailable, diagnostic-queue-full, update download/verify failures) by wiring existing handlers into the matrix; add a durable backing for the diagnostic queue (needed the day D1 lands). | A1 | B1, B2 |

### Phase C — The new-spec capabilities (the core of this upgrade)

| # | Area | Work | Depends on | Parallel with |
|---|---|---|---|---|
| C1 | 4 Symbol discovery + broker compat | **Symbol-resolution abstraction — DONE locally (§2.4, audit P2-10 refresh):** `SymbolResolver` + `SymbolInventory` + `SymbolMetadata` exist (canonical-ID → broker-symbol mapping with metadata comparison, candidate listing, ambiguity reporting, mapping cache + refresh; hidden-symbol inventory handling and availability reporting; stop-limit pending types verified — `BUY_STOP_LIMIT` / `SELL_STOP_LIMIT` in `engine/execution.py:32–33`). Remaining is the D7 wire decision (how the server learns the inventory). | B2 (terminal data), B3 (symbol-unavailable scenario) | C2, C3 |
| C2 | 5 Order execution + recovery | `BUY_STOP_LIMIT` / `SELL_STOP_LIMIT` in the local execution path — DONE (`engine/execution.py:32–33`); local close-by (`TRADE_ACTION_CLOSE_BY`) — DONE (`trade_close_position_by`, `engine/positions.py:306`); `PARTIALLY_COMPLETED` in the lifecycle — DONE locally (`commands.py:421`); the wire representation is still gated on D5 (do not invent the wire status first); the local `trade_modify_pending_order` volume question is P2-8's area (merged as PR #47). | C1 (symbol resolution for exact broker symbols) | C1, C3 |
| C3 | 6 Account data + journal sync | **Journal-event collector — DONE locally (§2.8, audit P2-10 refresh):** `JournalCollector` built from MT5 state deltas (never inventing fills), with the spec §8 field set and event-level dedup, wired in production. MT5-vs-derived labelling for accounting payloads (§2.9) exists; explicit deposit/withdrawal/fee surfacing is local labelling only. Remaining is the D6 wire decision. | B1 (checkpoint persistence — journal ranges depend on it) | C1, C2 |
| C4 | 9 GUI integration | Add the **Symbols** screen (inventory, exact broker names, search, metadata, availability, resolution ambiguity) and the **Execution** screen (recent lifecycle, status, failures, pending ops, reconciliation state). Both consume data built in C1–C3; no new data acquisition. | C1, C2, C3 | — (after C1–C3) |

### Phase D — Contract-gated work (cannot complete without Platform/user decisions)

| # | Area | Work | Depends on | Parallel with |
|---|---|---|---|---|
| D-a | 2 REST + WS / 5 execution | `modify_pending` close-out (§17.4 a–d): (a)–(c) DONE — allowlist-gate, tests, **market-open live proof E-P2B-6 (2026-11-19)**; only (d) published minimum `connector_version` still owed. | Platform: published minimum version only (live proof done) | D-b |
| D-b | 5 execution | `open_order` / `cancel_order` allowlist extensions + demo proof + `schema_version` bump. | **User decision** (§17.5) + Platform Risk Engine proof (D2/D3) | D-a |
| D-c | 7 telemetry | Switch the `DiagnosticsSink` from inert to live once D1 defines the endpoint; backfill durable queue delivery + delivery tests. | Platform: D1 endpoint definition | D-a, D-b |
| D-d | 1 Architecture | Close out remaining §18 B rows per §18.17 (allowlist extensions with market-open proof + published minimum version per op); update §18.15 counts. | D-a, D-b, C2 | — |

### Phase E — Proof (runs throughout, gates the PR)

| # | Area | Work | Depends on |
|---|---|---|---|
| E1 | 10 Testing & CI | Keep the 311-test Linux gate green; add unit tests for C1 (resolver/ambiguity), C2 (partial-close lifecycle, close-by), C3 (journal events, labelling), C4 (new screens); add the dedicated server-restart integration test; measure steady-state resource usage (§2.15). | Each phase as it lands |
| E2 | 10 Testing & CI (+ 1) | Manual Windows-with-MT5 Demo run of the 104 e2e tests (never in CI); live verification against the deployed Platform once D11 is resolved (enrol → sync → command → result → reconnect → no-duplicate). | D11; C1–C4 |
| E3 | 1 Architecture | Re-run this gap analysis against the finished branch; verify every §25 acceptance item with evidence; update `docs/ACCEPTANCE.md` and `docs/PLATFORM-GAPS.md`. | All phases |

**Dependency graph (summary).** A1 → everything. B1 → C3, C4. B2 → C1. B3 → C1 (symbol scenario), C4 (diagnostics data). C1 → C2 (exact symbols), C4 (Symbols screen). C2 + C3 → C4 (Execution screen). D-a/D-b/D-c/D-d are gated on user/Platform decisions and run in parallel with C once unblocked. E1 runs continuously; E2/E3 gate the PR.

**What can run in parallel right now:** A1 + A2 (different people/files, both unblock the PR);
B1 + B2 + B3 (disjoint modules: `server/`, `mt5/`, `reliability/`); C1 + C2 + C3 (shared
interfaces must be agreed first — resolver API, lifecycle states, journal event shape —
then the three areas proceed in parallel with an integration review before merge, per
spec §21).

---

## §6 Risks / open questions (need the user's or Platform side's decision — not resolved here)

1. **Windows CI failure at head `3f0950c` — cause undiagnosed.** The `build-windows`
   job fails at "Run unit + mock-integration tests on Windows"; job logs return 403 from
   this environment, so the cause cannot be established from available evidence. The
   Linux win32 simulation reproduced only DPAPI-availability artifacts. **Needs:** a
   run with accessible logs (or a Windows machine) to read the actual failure. This is
   the top PR blocker: no green Windows run exists for this branch.
2. **`open_order` user decision (§17.5).** Until the user explicitly approves, the AI
   Agent cannot enter trades, `open_order` stays test-rejected, and 8 of the 21 B rows
   cannot close out. **Needs:** the user's explicit call (and, after it, the Platform
   Risk Engine + financial-consent proof on demo).
3. **`PARTIALLY_COMPLETED` wire representation.** Spec §7 requires the state; contract
   §6.5 has no partial status. **Needs:** a Platform/contract decision — new
   `action.result` status or a `partial` payload marker — for the wire half. The Bridge
   has implemented the state LOCALLY (`CommandState.PARTIALLY_COMPLETED`,
   `commands.py:421`; wire stays `executed` with the partial facts in the payload) —
   no wire surface was invented.
4. **Diagnostics endpoint (D1).** CLOSED on the Bridge side by P5a: the endpoint
   `POST /api/v1/connector/diagnostics` exists and the sink is wired. Two spec §25
   acceptance items still cannot be satisfied without live proof (D11). **Needs:**
   live ingest proof against the deployed Platform.
5. **Journal/event channel (D6) and symbol-inventory channel (D7).** **Needs:** a
   Platform decision — new channel(s), or a formal statement that journal derivation
   from sync batches and on-demand chart frames are the intended designs.
6. **Release-signing public key (operator action) — CLOSED by P1-6.** The key is
   provisioned (bundled `nse_bridge/update_public_key.b64` + release-job signing step).
   **Needs:** live signed-release install proof (P1-9) and the Platform D19 serve of
   the `.sig` sidecar.
7. **`.ico` asset + installer — icon CLOSED.** `assets/icon.ico` exists and is bundled
   (`bridge.spec`, Inno `SetupIconFile`); an Inno Setup installer exists
   (`windows/NSE-MT5-Bridge.iss`, shipped as `NSE-MT5-Bridge-Setup-1.4.0.exe`).
   **Needs:** only the decision whether the Inno installer (vs the portable exe alone)
   is the distribution — WiX/NSIS is no longer the question.
8. **Rollback strategy for updates — CLOSED by P1-7.** `docs/ROLLBACK.md` is written and
   implemented (previous-version retention + fallback launch). **Needs:** live Windows
   proof (P1-9).
9. **Multiple attached terminals.** Spec §3 wants independent connection state per
   terminal; the Bridge attaches one terminal at a time. **Needs:** a scoping decision —
   is single-attached-terminal acceptable for v1 (multi-terminal = discovery +
   selection only), or is true multi-attach required?
10. **Local `trade_modify_pending_order` volume semantics.** The local modify sends
    `volume` in its `TRADE_ACTION_MODIFY` request, but MQL5 states a pending order's
    volume cannot be changed (delete + replace instead). **Needs:** the execution
    agent to confirm broker behaviour and either drop `volume` from the request or
    document that the broker rejects/ignores it.
11. **Sync-checkpoint persistence design.** Restart currently triggers a full history
    backfill (safe, wasteful). **Needs:** agreement that the checkpoint is persisted
    (JSONL/bounded, same pattern as the result store) — a small change, but it is a
    storage-policy decision (spec §16 lists checkpoints as persist-worthy).
12. **Performance evidence.** No measurements exist for steady-state memory/CPU/latency.
    **Needs:** a lightweight soak/profile run before claiming §15/§25 resource criteria.
13. **Manifest hash semantics.** The Bridge hashes the local 90-command registry; the
    contract's reference hash (`b3df4d9fb2a3b7e0`) is an MCP `tools/list` hash for
    terminal build 6306. **Needs:** Platform confirmation that it keys availability by
    `(terminal_build, manifest_hash)` and never compares the Bridge hash against the MCP
    hash (contract §15 item 8 already requires this, but it should be confirmed when the
    manifest POST is wired).
14. **Live-Platform verification (D11).** Blocked on the #103/#107 migration-0054
    resolution and a deployed build. **Needs:** Platform-side resolution; no Bridge
    action can substitute for it.
15. **Kill switch (local).** The contract's kill-switch scopes are Platform-side; the
    Bridge has no local kill switch. **Needs:** a decision only if the Platform ever
    requires the Bridge to enforce one locally — not assumed.

---

## Appendix — evidence index (where each claim was verified)

- **Branch/head/tree:** `git status --short --branch`, `git log --oneline -1` in
  `~/workspace/NSE-Connector-Bridge-audit` (branch `refactor/complete-mt5-infrastructure`,
  head `3f0950c`, clean).
- **Spec:** `~/workspace/nse-bridge-refactor/SPEC-2026-11-18-COMPLETE-INFRASTRUCTURE.md`
  (1,398 lines, §§0–25).
- **Contract:** `~/workspace/NSE-Platform-connector/docs/NSE-CONNECTOR-CONTRACT.md`
  (2,863 lines, §§0–19, read completely).
- **Prior docs:** `~/workspace/nse-bridge-refactor/AUDIT.md`,
  `ARCHITECTURE-CONTRACT.md`, `PLAN.md`.
- **Test run (this audit):** `/tmp/nse-bridge-venv/bin/python -m pytest tests/unit
  tests/integration -q` → `311 passed in 17.57s`.
- **Registry count:** 90 `@command` decorators across `nse_bridge/engine/*.py`
  (verified by grep count).
- **Key code paths cited:** `server/rest.py`, `server/commands.py`, `server/sync.py`,
  `server/chart.py`, `server/manifest.py`, `realtime/channel.py`, `realtime/bus.py`,
  `mt5/driver.py`, `engine/symbols.py`, `engine/account.py`, `engine/execution.py`,
  `engine/orders.py`, `engine/positions.py`, `engine/trading.py`, `engine/market.py`,
  `engine/charts.py`, `reliability/telemetry.py`, `reliability/self_healing.py`,
  `reliability/result_store.py`, `tasks/registry.py`, `secrets.py`, `config.py`,
  `logging_setup.py`, `updates.py`, `api/app.py`, `api/management.py`, `main.py`,
  `gui/static/index.html`, `.github/workflows/build.yml`, `bridge.spec`,
  `scripts/mock_server.py`, `docs/ACCEPTANCE.md`, `docs/PLATFORM-GAPS.md`.
- **Negative greps (absence verified, not assumed):** `PARTIALLY_COMPLETED` — zero hits
  in `nse_bridge/`, `tests/`, `docs/`; `journal` — zero hits in `nse_bridge/`; symbol
  `resolv*/ambigu*/canonical` — zero hits in `nse_bridge/` (excluding timeframe/filling
  resolvers); `rollback` — zero hits in `nse_bridge/`/`updates.py`; `send_manifest` /
  `build_manifest_payload` — definition sites only, no runtime caller; local
  kill-switch — no implementation found (only unrelated log words).
- **Not verified in this audit (stated as such, not claimed):** live Platform behaviour;
  Windows CI failure cause; REST-heartbeat `pending_commands > 0` → WS-open behaviour;
  task-worker cancellation/resource-limit enforcement; hidden-symbol completeness of
  `symbols_get()`; per-category telemetry emission completeness (category presence
  checked, exhaustive per-category test not run); log rotation/retention policy;
  installer documentation (none found); MT5 e2e results (require a terminal).
