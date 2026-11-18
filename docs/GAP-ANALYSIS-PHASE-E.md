# NSE Connector Bridge — GAP-ANALYSIS Phase E Re-verification
(Follow-up branch refresh)

| Field | Value |
|---|---|
| Date | 2026-11-18 |
| Branch | `fix/bridge-real-mt5-defects` (follow-up to merged PR #2) |
| Head SHA | `67f8ff5` — `test(e2e): rewrite WS channel tests to the CONTRACT v1 protocol` |
| Prior audit re-verified | `docs/GAP-ANALYSIS.md` (1,331 lines, head `3f0950c`, 12 commits from main, 311 tests) — NOT edited |
| Governing spec | `SPEC-2026-11-18-COMPLETE-INFRASTRUCTURE.md` (§§0–25) |
| Contract | `NSE-CONNECTOR-CONTRACT.md` (CONTRACT v1) |
| Broker-compat audit re-verified | `~/workspace/bridge-broker-compat-audit.md` (head `b0eed5b`) |
| Working tree | Clean except this file (uncommitted, by design) |

Refresh note (2026-11-18): this document was first written against head
`cbfdeeb` on `refactor/complete-mt5-infrastructure` (PR #2). PR #2 was
MERGED 2026-11-18T10:05:35Z (merge commit `4f01068`, head `aae5075`, CI
green — see §2.20). The work moved to this follow-up branch from merged
main: commit `c27bf0b` closes the two new gaps N1/N2 found by the first
Phase E pass, and commits `28c83d2`/`ebc72b2`/`e66951c`/`67f8ff5` fix
the 4 real-MT5 defects found by live terminal testing (see §6 below).
All counts, statuses, and §25 rows below are refreshed to head
`67f8ff5`; the `cbfdeeb` values are kept in the "Old" columns where
the table has them.

## Method

1. Re-read `docs/GAP-ANALYSIS.md` §§2–6 in full, the broker-compat audit, and spec §13/§25.
2. Read the code itself for every claim — did not trust summaries or commit messages:
   module inventories, grep for every claimed capability, and read the decisive paths
   (REST client, WS channel, enrolment/manifest, sync + checkpoint store, resolver/
   inventory, journal collector + accounting, execution/close-by, commands lifecycle,
   self-healing registry, telemetry + diagnostic store, GUI, updater, tasks worker,
   CI workflow, `docs/ACCEPTANCE.md`).
3. Ran the test suite on the follow-up branch head:
   `~/.venvs/bridge/bin/python -m pytest tests/unit tests/integration -q`
   → **556 passed, 1 warning in 118.03 s** (verified by this
   re-verification, not taken from a report; an earlier run showed
   555 passed + 1 failed on a single timing-sensitive enrolment test
   under heavy VM load — it passes alone, as a file, and on the clean
   re-run, i.e. a load flake, not a regression; no test was weakened).
   The 104 e2e tests were NOT run: they require a live MT5 terminal,
   which does not exist on this VM.
4. Ran one live check: `build_position_model(..., volume="abc")` still raises `ValueError`
   (broker-compat R9, §R below).
5. Did NOT claim a real-terminal pass for the follow-up fixes (the
   defect fixes themselves were verified by unit tests executed here,
   both-ways via `git stash` where stated; live re-verification of
   the fixes against the Wine terminal is owed via the parent) and
   did NOT claim CI green for the follow-up branch (its runs start
   after push; see §2.20). CI green IS claimed for merged main at
   `aae5075` (runs 44274450546 + 44274443061, verified via the
   GitHub API by full head SHA).

**Verdict scale (same as the prior audit):** *Implemented* (present, wired, tested at
unit/mock level), *Partial* (substantially present but a named piece is missing or unwired),
*Missing* (no implementation), *Unchanged* (same status as at `3f0950c`).

## Headline counts

| Scope | Old (`3f0950c`) | New (`67f8ff5`) |
|---|---|---|
| Spec §§2.2–2.20 (19 sections) | Implemented 1, Partial 17, Missing 1 | **Implemented 9, Partial 10, Missing 0** |
| Contract dependencies D1–D13 | 13 open | **8 unchanged, 5 improved (D5, D6, D7, D8, D13 — local half done, contract half still owed)** |
| Broker-compat R1–R10 / A1–A10 (20 items) | 0 fixed | **12 fixed, 1 partial (R7), 7 unfixed** |
| Tests (unit + mock-integration) | 311 passed | **556 passed (verified here, 118.03 s)** |
| Local commands | 90 | **91** (new: `trade_close_position_by`, `engine/positions.py:293`) |
| GUI sections | 6 | **7** |

---

## §2 Section-by-section re-verification vs spec §§2–20

### §2.2 Complete server-to-client communication — Partial → **Implemented** (local; live proof owed via D11)

| Old gap | Status now | Evidence |
|---|---|---|
| No request-cancellation API | **Fixed** | `server/rest.py:194-198,242-267,305-308` — in-flight tasks tracked, `cancel_all()` cancels them, `CancelledError` propagates; `tests/unit/test_rest_cancellation.py` (4 tests) |
| REST heartbeat → WS wake unverified | **Fixed / verified** | `realtime/channel.py:222-224,319-333,347-363,476-511` — `pending_commands > 0` with no WS live sets the wake event, interrupting backoff; `tests/unit/test_channel_heartbeat_ws.py` (8 tests) |
| Manifest POST / `manifest.update` never sent | **Fixed** | `server/manifest.py:125-175` (`ManifestTracker.send_via_rest`, unchanged-hash no-op); `server/enrolment.py:331-347` (`_publish_manifest` on enrolment, best-effort); `realtime/channel.py:365-387,658-662,747-749` (`_maybe_send_manifest_update` on connect + heartbeat, `build_manifest_payload` at :932); `tests/unit/test_manifest_wiring.py` (6 tests) |
| No PUT/PATCH/DELETE call sites | **Unchanged** (not a gap) | `_request(method, ...)` accepts any method (`rest.py:296-300`) but only GET/POST call sites exist; contract defines no PUT/PATCH/DELETE endpoint |
| No live-Platform proof | **Unchanged** | Mock server only; blocked by D11 |

### §2.3 Terminal discovery & management — Partial → **Partial** (narrowed to 2 items)

| Old gap | Status now | Evidence |
|---|---|---|
| No terminal data-directory field | **Fixed** | `TerminalInfo.data_dir` (`mt5/driver.py:370,379`); `_portable_data_dir` (:391), `_appdata_terminal_data_dirs` (:401), wired in discovery (:446-483, 518, 524-528); `tests/unit/test_mt5_discovery.py` expanded (+338 lines in diff) |
| Re-discovery after config changes (unverified) | **Fixed / verified** | `api/management.py:185-211` — PUT /settings re-discovers when `mt5_terminal_path` / `mt5_custom_terminal_dirs` change, returns `terminals_rediscovered`; self-healing `on_rediscover_terminals` (`reliability/self_healing.py:181,408-417`) |
| Independent connection state per terminal | **Still Missing** | Single attached terminal only: one `Mt5Driver`, `_connect_job` uses `settings.mt5_terminal_path or auto_discover_terminal_path()` (`mt5/driver.py:753-757`); no per-terminal connection-state table. Needs the §6 Q9 scoping decision (single-attached acceptable for v1?) |
| Terminal restart detection as a distinct event | **Still Missing** | No restart probe in `mt5/driver.py` or `realtime/bus.py` (grep for `restart` finds nothing there). `MT5_RESTART` exists only as a self-healing matrix handler reacting to state (`self_healing.py:380-396`), not a detector |

### §2.4 Broker & symbol intelligence — Partial → **Implemented** (local; D7 channel still owed)

| Old gap | Status now | Evidence |
|---|---|---|
| No symbol-resolution abstraction | **Fixed** | New package `nse_bridge/symbols/` (`canonical.py` 169, `resolver.py` 402, `inventory.py` 279, `metadata.py` 425 lines): canonical-ID parse, verbatim-match wins, metadata comparison, candidate listing, ambiguity ALWAYS reported with full candidate list + differing fields (`resolver.py:28-33,91-120,296-333`), mapping cache with TTL + `refresh()` / `invalidate()` (`resolver.py:152-224`), refresh on broker-environment change via `HealingContext.on_refresh_symbols` (`resolver.py:45-47`); `tests/unit/test_symbol_resolution.py` (29 tests); `docs/SYMBOLS.md` |
| Hidden symbols | **Fixed (with caveat)** | `symbols/inventory.py:12-18,155-158,270-276` — `hidden_symbols()` lists hidden rows the driver reported; completeness caveat documented (driver/broker-dependent, unverified live) |
| Symbol availability / untradeable detection | **Fixed** | `inventory.availability()` (`inventory.py:210`); `get_trading_symbols(tradeable_only=...)` now matches the inventory rule — unreported trade mode = NOT tradable (`engine/symbols.py:19,131-134`, broker-compat A2 fix) |
| Hardcoded probe symbols in driver | **Fixed** | No `EURUSD`/`GBPUSD`/`USDJPY` remains in `mt5/driver.py` (grep: zero hits); offset detection now broker-discovered from live ticks (see A1) |

### §2.5 Account info & synchronization — Partial → **Implemented** (local)

| Old gap | Status now | Evidence |
|---|---|---|
| Sync checkpoint in-memory only | **Fixed** | New `server/sync_checkpoint.py` (143 lines, `SyncCheckpointStore`); restored at construction, persisted on every advance (`server/sync.py:252-253,268-282,306-319`); wired in both production constructions (`api/app.py:162`, `realtime/channel.py:415-424`); `tests/unit/test_sync_checkpoint.py` (11 tests) |
| Unreported `trade_mode` labelled `demo` (broker-compat R5) | **Fixed** | `server/sync.py:664-698` — `type` omitted when `trade_mode` is None/unknown; batch now carries `margin_level` / `leverage` / `swap` / `commission` (`sync.py:660-710` docstring + fields) |
| No Bridge-side demo gate | **Unchanged** (deliberate) | Contract §7.4: enforcement is server-side; Bridge parses `trade_mode` only |
| Event-driven updates | **Unchanged** | Polling only (no MT5 push exists) — platform-capability limit, not an omission |

### §2.6 Order execution infrastructure — Partial → **Implemented** (local; remote send still contract-gated)

| Old gap | Status now | Evidence |
|---|---|---|
| `BUY_STOP_LIMIT` / `SELL_STOP_LIMIT` absent from execution path | **Fixed** | `engine/execution.py:32-33,136-140,198-211` — both types in `PENDING_TYPES`, stop-limit price/stoplimit validation; `tests/unit/test_execution_c2.py` (34 tests) |
| Close-by missing locally | **Fixed** | New local command #91 `trade_close_position_by` (`engine/positions.py:293-296`); `TRADE_ACTION_CLOSE_BY` send + live-state verification (`engine/trading.py:493-565`); hedging-only documented, netting = opposing deal |
| Partial close has no server wire shape | **Unchanged** | Local `close_position(volume=…)` exists; §6.2 envelope still carries no `volume` / `close_by_ticket` field (grep `volume|close_by` in `server/commands.py` finds only payload/result handling, no envelope fields) — D5 |
| `trade_modify_pending_order` volume question (§3 §18 F-row) | **Fixed** | Volume removed from the modify request (`engine/orders.py:154-156` comment: MQL5 `TRADE_ACTION_MODIFY` cannot change pending volume; delete + replace instead) |
| MQL5 hedging-only constraints for partial close | **Still Missing** | Not enforced by the Bridge — server must check margin mode first, per contract (D5) |

### §2.7 Order safety, idempotency & recovery — Partial → **Implemented** (local; wire status still owed via D5)

| Old gap | Status now | Evidence |
|---|---|---|
| `PARTIALLY_COMPLETED` does not exist | **Fixed (local state)** | `CommandState.PARTIALLY_COMPLETED` (`server/commands.py:67-70,209,216-227` — terminal state, no outgoing edges); transitioned on partial payload (`commands.py:771-776`); telemetry `command.partially_completed` with closed/remaining volume (:647-658); tests `tests/unit/test_execution_c2.py:858-917`, `tests/unit/test_gui_data.py:301-318,523-526`; GUI class mapping (`gui/static/index.html:737`); `docs/COMMAND_REFERENCE.md:54-55` |
| No wire representation for partial | **Unchanged** | Deliberate: wire status stays `executed` (§6.5 has no partial status) with `partial: true` + `closed_volume` / `remaining_volume` in the payload (`commands.py:763-776`, docstring :74-76). Needs the contract decision (D5) — the Bridge did not invent wire surface |

### §2.8 Trading journal & execution event data — Missing → **Partial** (N1 CLOSED in `c27bf0b`; upload channel still owed via D6)

| Old gap | Status now | Evidence |
|---|---|---|
| No journal module / collector / event stream | **Fixed in code** | New `nse_bridge/journal/` (`collector.py`, `events.py`, `accounting.py`): 14 event types (`events.py:54-68` — order submitted/accepted/rejected, position opened/modified/partially/fully closed, pending created/modified/canceled, deal executed, execution-response, broker error, terminal-connection-lost, account-connection-changed); §8 field set; event-level dedup (bounded 10k) + store (1k); `tests/unit/test_journal.py` (28 tests, 604 lines); `docs/JOURNAL.md`; GUI endpoint `GET /journal/events` (`api/management.py:1147-1159`) |
| Journal hooks in sync | **Fixed in code** | `server/sync.py:274,297-301,506-526,599-619,621-625,642-645` — position-snapshot diff + history collection, guarded so journaling never breaks sync |
| **Runtime wiring** | **✅ CLOSED — N1 fixed in `c27bf0b`** | `JournalCollector` is now constructed in the `create_app` lifespan (`nse_bridge/api/app.py`) with the sync engine's checkpoint + persist callback and the broker UTC offset, and passed as `SyncEngine.journal` — the collector collects in the shipped app and the Execution screen's journal feed is fed. Pinned by `tests/unit/test_production_wiring.py` (3 tests, incl. a wired journal collecting a `position_opened` event). The `realtime/channel.py` construction builds its own `SyncEngine` without a journal (channel path has no GUI feed); noted, not a gap. |
| Journal/event upload channel | **Unchanged** | No upload endpoint/frame in CONTRACT v1 — D6 |

### §2.9 Accounting & financial data — Partial → **Implemented** (local; D8 statement still owed)

| Old gap | Status now | Evidence |
|---|---|---|
| No MT5-vs-derived labelling | **Fixed** | `journal/accounting.py:51,102-173` (`LabelledValue` with `provenance`: `mt5_supplied` / `locally_derived` / `not_reported`); wired onto the wire: account batch carries a `provenance` map (`server/sync.py:704-710`), history pages carry `provenance` + `balance_operations` on deals pages (:819-833); `tests/unit/test_accounting.py` (154 lines) |
| Deposits/withdrawals/balance adjustments/fees | **Fixed (surfacing)** | `surface_balance_operations()` (`journal/accounting.py:247`, called from `sync.py:831-833`) — balance ops surfaced from deals instead of being invisible |
| Per-deal commission/swap/fee | **Unchanged** | Flows through only if MT5 reports it (D13) |
| No local accounting engine | **Unchanged** (correct) | None exists, per spec §9 |

### §2.10 Market data & symbol monitoring — Partial → **Partial** (unchanged verdict, one bug fixed)

- Local market commands, chart frames (§17.1), tick freshness assessment: unchanged, still implemented.
- **Fixed since `3f0950c`:** chart-frame budget double-charge (Codex P2) — subscription poll pre-check consumed 2 budget entries per frame (4 for data+ticks), so the 60/min budget was really ~30/min. Fixed with a non-recording `would_allow()` pre-check; only `_enqueue_chart → allow()` records (`server/chart.py`, diff `3f0950c..cbfdeeb`).
- **Still missing/unchanged:** no server channel for market-book subscription or raw tick streaming (contract defines none); no server-visible "symbol availability changed" event.

### §2.11 Live-state monitoring — Partial → **Partial** (unchanged)

- Pollers (tick 250 ms, account 1 s, positions 2 s, orders 3 s, status 10 s), diagnostics summary, account-switch detection: unchanged.
- **Still missing/unchanged:** event-driven updates (no MT5 push exists); no server-visible state-change channel (positions/orders/deals reach the server only via sync batches + `action.result`); no periodic full-state diff reconciler (next poll re-reads full state, bounding the gap to one poll interval).

### §2.12 Logging, telemetry & diagnostics — Partial → **Partial** (N2 CLOSED in `c27bf0b`; D1 endpoint still owed)

| Old gap | Status now | Evidence |
|---|---|---|
| Durable backing for the diagnostic queue | **✅ CLOSED — N2 fixed in `c27bf0b`** | `reliability/diagnostic_store.py` (`DurableDiagnosticStore`, `DurableDiagnosticQueue`, bounded JSONL mirroring the in-memory queue); `tests/unit/test_diagnostic_store.py` (16 tests). Now WIRED: the `create_app` lifespan constructs `TelemetryEmitter(queue=DurableDiagnosticQueue(DurableDiagnosticStore(data_dir / "pending_diagnostics.jsonl")))`, so queued diagnostics survive a Bridge restart. The same commit fixed a latent falsy-queue bug in `TelemetryEmitter.__init__` (`queue or DiagnosticQueue()` silently replaced an injected empty queue — an empty `DiagnosticQueue` is falsy via `__len__`; now `queue if queue is not None else DiagnosticQueue()`). Pinned by `tests/unit/test_production_wiring.py`. The sink stays inert (`NullDiagnosticsSink`) — D1, not N2. |
| Delivery (diagnostics endpoint) | **Unchanged** | No endpoint in CONTRACT v1 §4.2; queue drains by TTL — D1 |
| Local pipeline | **Unchanged** (implemented) | Structured JSON telemetry, bounded queue (retry/backoff/dedup/limits/expiry), redaction |

### §2.13 Self-healing — Partial (15/20) → **Implemented** (local; 20 entries, one structural nuance)

- All 20 spec §13 scenarios now have registry entries with handlers: the 5 added are
  `account_disconnect` (§13.9), `broker_connection_change` (§13.10), `symbol_unavailable`
  (§13.12), `diagnostic_queue_full` (§13.17), `update_failure` (§13.19+§13.20)
  (`reliability/self_healing.py:68-96`; one `_handle_*` method per entry, :288-674;
  43 tests in `tests/unit/test_self_healing.py`, incl. `test_registry_covers_exactly_the_20_spec_scenarios`).
- **Nuance:** spec §13 items 19 (update download fails) and 20 (update verification fails)
  share ONE `UPDATE_FAILURE` entry that branches on `update_stage` (download may retry,
  verification must never retry/install the same bytes — module docstring). The matrix
  therefore has 20 entries covering 20 spec items, but 19+20 are not separate entries.
  `docs/ACCEPTANCE.md` #7's wording ("update failure split into download/verification")
  overstates this — it is one entry branching on stage, not two entries. Functionally
  covered; the wording should be corrected, not the code.
- **Note:** `server_restart` is a 21st registry entry with no spec §13 number (Platform-side
  restart handling, pre-existing). Live proof against a real terminal is still owed
  (all handlers are unit-proven with fakes).

### §2.14 Security & device trust — Implemented → **Implemented** (unchanged)

- Secrets (keyring/DPAPI), token rotation/revocation, device registration, TLS, loopback
  guardrails, `X-API-Key`, validation/replay/expiry: unchanged, still implemented.
- **Still outstanding (unchanged):** per-device credential exchange + per-command signatures
  (contract v1.1, D10); **no local kill switch** (grep for `kill_switch`/`kill.switch` in
  `nse_bridge/` finds only GUI diagnostic text about MT5's algo-trading button and
  unrelated log words) — needs a decision only if the Platform ever requires one locally.

### §2.15 Resource management & performance — Partial → **Partial** (unchanged)

- Bounded by construction (queues, single worker thread, pooling, controlled polling):
  unchanged.
- **Still missing:** no measured performance evidence — no profiling, memory, CPU, or
  latency measurements anywhere in the repo or docs; no soak/profile run. Spec §25
  "operational monitoring works" cannot yet be tied to resource behaviour.

### §2.16 Local storage & database policy — Partial → **Partial** (narrowed)

| Old gap | Status now | Evidence |
|---|---|---|
| Sync checkpoint not persisted | **Fixed** | See §2.5 |
| No documented retention/expiry policy | **Still Missing (doc); evidence improved** | Log rotation/retention EXISTS in code and is now verified: `logging_setup.py:161-172` (rotation 10 MB / retention 10; 20 MB / retention 5 — pre-existing since `22ad015`, previously unverified by the old audit). But no written storage-policy doc states retention/expiry for local files (logs, result store bounds, staged update lifetime) |
| Diagnostic-queue persistence | **Still Missing at runtime** | See §2.12 / N2 |

### §2.17 GUI — Partial (6/7) → **Implemented**

- **7 sections now:** Connection, MetaTrader, Account, **Symbols**, **Execution**,
  Diagnostics, Updates (`gui/static/index.html` — `page-connection`, `page-metatrader`,
  `page-account`, `page-symbols`, `page-execution`, `page-diagnostics`, `page-updates`).
  Symbols shows broker inventory with verbatim names, metadata, availability, resolver
  ambiguity; Execution shows recent command lifecycle incl. `PARTIALLY_COMPLETED`,
  pending results, journal events. Data endpoints in `api/management.py:1019-1159`
  (`/symbols/inventory`, `/executions`, `/journal/events`).
- Proven by `tests/unit/test_gui_control_center.py`
  (`test_gui_html_has_exactly_seven_screens`) + `tests/unit/test_gui_data.py` (550 lines).
- Settings/credentials live under Connection per spec §17 (the old "Settings as own screen"
  deviation is resolved).

### §2.18 Updates & release engineering — Partial → **Partial** (narrowed)

| Old gap | Status now | Evidence |
|---|---|---|
| Windows CI updater failure | **Fixed — CI green on merged main** | Two rounds: `cbfdeeb` made the apply-script template line-ending independent (CRLF); the real root cause was encoding — 4 em dashes (U+2014) in the template, written UTF-8 but read with `read_text()` defaulting to cp1252 on Windows. Fixed in `aae5075` (em dashes → `--`; CRLF normalization kept as defense-in-depth) + regression test `test_apply_script_template_is_ascii_and_survives_cp1252_read`. CI runs 44274450546 + 44274443061 at `aae5075` both SUCCESS (test-linux + build-windows); PR #2 merged at `4f01068` |
| Release-signing public key unprovisioned | **Still Missing (operator action)** | No `nse_bridge/update_public_key.b64` exists; updater still fail-closes with `NO_PUBLIC_KEY` (`updates.py:84,152,197,222`) — correct behaviour, but no update can install end-to-end until the operator provisions the key |
| No rollback / recovery strategy | **Still Missing** | Zero hits for `rollback` in `nse_bridge/` (only mentions are in `docs/GAP-ANALYSIS.md` itself) |
| No `.ico` asset | **Still Missing** | `bridge.spec:62-64` documents the absence; PyInstaller falls back to its default icon |
| No installer (WiX/NSIS) | **Still Missing** | Distribution is the PyInstaller single-file exe only; no installer doc/build script |
| No tagged release | **Still Missing** | `git tag` returns nothing; the tags-only release job has never executed |

### §2.19 Future server-directed computation — Partial → **Partial** (narrowed, boundary clarified)

- Envelope, validation mirroring §6.3, bounded worker, EMPTY kind registry (no dynamic
  import/`eval`/`exec`): unchanged, still implemented.
- **Now verified (was unverified):** the worker DOES enforce the envelope's `timeout_s`
  (hard, via future timeout — `tasks/worker.py:155-162`); `max_cpu_s` / `max_memory_mb`
  are RECORDED limits, explicitly not enforceable in-process
  (`tasks/envelope.py:91-98`, `tasks/worker.py:6` docstring).
- **Still missing:** no per-task cancellation API (only executor shutdown with
  `cancel_futures=True`, `worker.py:78-80`); the wire format itself is still a contract
  dependency (D9).

### §2.20 Testing & CI — Partial → **Partial** (narrowed; local gates now complete, Windows verdict pending)

| Old gap | Status now | Evidence |
|---|---|---|
| 311 tests pass on Linux | **Improved** | **556 passed, verified here** (`~/.venvs/bridge/bin/python -m pytest tests/unit tests/integration -q`, 118.03 s). New suites: `test_symbol_resolution.py` (29), `test_execution_c2.py` (34), `test_journal.py` (28), `test_accounting.py`, `test_gui_data.py`, `test_manifest_wiring.py`, `test_sync_checkpoint.py`, `test_diagnostic_store.py`, `test_rest_cancellation.py`, `test_channel_heartbeat_ws.py`, `test_broker_compat_fixes.py`, expanded `test_self_healing.py` (43), `test_mt5_discovery.py`, `test_updater.py`, `test_production_wiring.py` (3, N1/N2 wiring) + 7 new real-MT5 defect regression tests (D1 ×3, D2 ×1, D4 ×3) |
| Windows CI fails, cause undiagnosed | **✅ Fixed — green on merged main and on this branch** | CI run 44271103244 (head `6a13be5`): `test-linux` SUCCESS; `build-windows` failed on exactly 1 test (`test_apply_script_replaces_dummy_target`). Root cause found (em-dash/cp1252 encoding, see §2.18) and fixed in `aae5075`. Runs **44274450546** (PR #2) and **44274443061** (push) at `aae5075` both SUCCESS — test-linux + build-windows green, incl. "Build single-file exe" + "Smoke-check the frozen exe" (artifact `NSE-MT5-Bridge-windows`, 45,377,692 bytes). PR #2 merged. **This branch:** run **44286483717** (PR #3) at `79a2948` SUCCESS — test-linux + build-windows green, artifact 45,378,534 bytes. This head differs by docs only; its own run is being watched before PR #3 is marked ready |
| Real-MT5 defects (found live 2026-11-18, terminal build 6323, MetaQuotes-Demo) | **Fixed on this branch** | D1 retcode labels + NO_CHANGES idempotency (`28c83d2`); D2 account trade_mode labels (`ebc72b2`); D4 `/health` broker connectivity (`e66951c`); D3 WS e2e test rewritten to the contract protocol (`67f8ff5`). See "Real-MT5 defects D1–D4" below. Live re-verification of the fixes is owed via the parent (Wine terminal run). |
| Lint scoped to tests/scripts; no format/static-analysis/security jobs | **Fixed** | `.github/workflows/build.yml:17-19,53-83,146-150` — `ruff check .` (whole repo) + `ruff format --check .` + `mypy nse_bridge` (default and `--platform win32`) + `bandit -r nse_bridge` + `pip-audit`, on both jobs; junit upload + `scripts/annotate_test_failures.py` (readable failures without log access) |
| No dedicated server-restart integration test | **Unchanged** | Still no dedicated server-restart integration test (mock reconnect behaviour only) |
| E2E never run green | **Unchanged** | 104 e2e tests (verified count) collect on Linux, fail loudly at fixture setup without a terminal — honest, not proven; manual Windows-with-MT5 Demo run still owed |

---

## §3 Contract coverage (§§0–19) — changes since `3f0950c`

| Contract § | Old | New | Evidence |
|---|---|---|---|
| §3/§4.1 Manifest | Method implemented, never called | **Implemented + wired** | See §2.2 (enrolment POST + WS `manifest.update`, unchanged-hash no-op, shared `ManifestTracker`) |
| §6 Commands | `PARTIALLY_COMPLETED` absent; close-by absent locally | **Local state + close-by implemented** | See §2.6/§2.7; `modify_pending` gate still CLOSED (`commands.py:367,412`), allowlist unchanged (`commands.py:169` — exactly `close_position`, `modify_sl_tp`, `modify_pending`) |
| §6.5 `action.result` | 3 statuses | **Unchanged (correct)** | Wire statuses still exactly `rejected \| executed \| mcp_failed` (`commands.py:474`); partial facts ride the payload, not a new status |
| §17.1 Chart | Implemented | **Implemented + budget fix** | See §2.10 (Codex P2 double-charge fix) |
| §17.4 `modify_pending` | Gate closed, close-out owed | **Unchanged** | Live proof at market open + published minimum `connector_version` still owed |
| §17.5 `open_order` | Open decision | **Unchanged** | Still excluded from the allowlist; no user decision recorded |
| §18 B rows | Close-by missing locally; manifest POST unwired | **Improved** | Close-by local capability now exists (row 18.8.12, local half); manifest POST wired (18.12.4); `open_order` variants (18.8.1–18.8.8), partial-close wire (18.8.10), close-all orchestration (18.8.11) unchanged — contract-gated |
| §18 F rows | `trade_modify_pending_order` volume question open | **Resolved** | See §2.6 (volume dropped from modify request) |
| §8 MT5 method | Python driver, not MCP | **Unchanged** | Standing decision; no EA/file bridge |

All other contract sections (§0–§2, §5, §7, §9–§16, §19): unchanged from the prior audit.

---

## §4 Contract dependencies D1–D13 — re-verification

| # | Old status | New status | Evidence / what changed |
|---|---|---|---|
| D1 Diagnostics/events upload endpoint | Missing | **Unchanged (Missing)** | No endpoint in CONTRACT v1 §4.2. Local pipeline complete; durable queue now WIRED in production (`c27bf0b`, N2 closed) — the sink stays inert by design until the endpoint exists. Blocks spec §25 "ERROR and WARN events reach the server reliably" + the delivery half of "diagnostic delivery survives temporary network failures" |
| D2 `open_order` sendable | Missing | **Unchanged (Missing)** | Still excluded; needs user approval + Risk Engine/financial-consent proof on demo + `schema_version` bump |
| D3 `cancel_order` sendable | Missing | **Unchanged (Missing)** | Local capability exists (`trade_cancel_pending_order`); server channel still excludes it |
| D4 `modify_pending` close-out + price/expiration | Missing | **Unchanged (Missing)** | Gate closed; local price/expiration capability exists; live proof + published minimum version owed |
| D5 Partial-close/close-by wire + `PARTIALLY_COMPLETED` | Missing | **Partial (local half done)** | Local state exists; partial facts ride the `action.result` payload (`partial: true`, `closed_volume`, `remaining_volume`) with wire status `executed`. Still owed from Platform: envelope `volume` / `close_by_ticket` fields + a wire representation for partial completion + hedging-only semantics decision |
| D6 Journal/event upload | Missing | **Partial (local half done and WIRED, upload channel still undefined)** | Local collector exists and is now instantiated in production (`c27bf0b`, N1 closed); upload channel still undefined. Platform decision still needed: new channel, or formal statement that journal derivation from sync batches is the intended design |
| D7 Symbol inventory / resolution service | Missing | **Partial (local half done)** | Local resolver + inventory exist (see §2.4). Still owed: contract decision on how the server learns the broker's symbol inventory (new sync kind, chart-frame extension, or on-demand only) |
| D8 Accounting source-data channel | Missing | **Partial (local half done)** | Provenance labelling now rides the wire (see §2.9). Still owed: Platform statement on whether the four sync kinds suffice with the labelling convention, or a new kind/field set |
| D9 Future compute wire format | Missing | **Unchanged (Missing)** | Local boundary only; timeout enforcement now verified, CPU/memory limits recorded-not-enforced, no per-task cancel |
| D10 Per-device credential + per-command signatures | Missing (v1.1) | **Unchanged (Missing, v1.1)** | No action until the Platform defines both mechanisms |
| D11 Live Platform + migration 0054 | Missing | **Unchanged (Missing)** | Live `bridge.nexusverse.space` behaviour still unverified from this environment; all integration tests run against the mock server |
| D12 Market-book / journal / calendar / news channel | Missing | **Unchanged (Missing)** | Local capability exists; no defined server channel; needs a contract decision on whether the server needs this data at all |
| D13 Per-deal commission/swap/fee, SL/TP on history, session times, by-ticket lookup | Partial | **Partial (improved within)** | `trade_execution` field fixed (see R7). Remainder unchanged: commission/swap flow-through only if MT5 reports it; SL/TP on history absent by construction; session times limited to the symbol record; by-ticket lookups exist as derived filters |

**Dependency counts:** 8 unchanged (D1, D2, D3, D4, D9, D10, D11, D12), 5 improved (D5, D6, D7, D8, D13) — in every improved case the Bridge-side local half is done and the Platform/contract half is still owed. Nothing in D1–D13 is fully closed.

---

## Broker-compat audit re-verification (R1–R10, A1–A10)

Fixed in merge `a95ca31` (`aa35615`, "Fix broker-compatibility defects R1–R6, R10, A1–A4") plus C2/C3 work; all with tests in `tests/unit/test_broker_compat_fixes.py` (20 test functions):

| Item | Status | Evidence |
|---|---|---|
| R1 (HIGH) Checkpoint mis-parses broker-local epochs as UTC | **Fixed** | `server/sync.py:112-135` — `parse_broker_time` subtracts the broker offset for epoch ints; tests `test_r1_*` (3) |
| R2 (HIGH) `validate_pending` rejects all pending orders when `volume_max` omitted | **Fixed** | Unreported bounds are now "no constraint"; tests `test_r2_*` (2) |
| R3 (MEDIUM) `resolve_filling_mode` crashes on non-numeric `filling_mode` | **Fixed** | Strings map by name, garbage defaults to IOC; parametrized test `test_r3_*` |
| R4 (LOW) `Entitlements.from_dict` crashes on non-numeric counts | **Fixed** | `_safe_count` degrades to 0 (`server/rest.py:110`); test `test_r4_*` |
| R5 (HIGH) Unreported `trade_mode` labelled `demo`; batch omits margin fields | **Fixed** | See §2.5; tests `test_r5_*` (4) |
| R6 (MEDIUM) `_reconcile` 1e-9 tolerance reports landed mods as UNKNOWN | **Fixed** | Tolerance now half a tick (+1e-9 float noise) for `modify_sl_tp` and `modify_pending`; tests `test_r6_*` (2) |
| R7 (MEDIUM) Builders invent silent zeros; `trade_execution` reads wrong field | **Partial** | `trade_execution` FIXED — now reads `trade_exemode` (`mt5/types.py:418-419`), `trade_calc_mode` separate (:420-421). **Remainder unfixed:** `build_position_model` still forces `volume`/`price_open` to 0.0 (:473), `build_order_model` same (:382), tick bid/ask default 0.0 (`build_tick_model`, :382 area), missing login → 0 |
| R8 (LOW) Currency invented as `"USD"` in 8 places | **Unfixed** | All 8 sites still present: `engine/risk.py:224,235,354,366,479,526`, `engine/account.py:170`, `engine/execution.py:525` (`or "USD"`) |
| R9 (LOW, defensive) Non-numeric strings crash builders | **Unfixed — verified live** | `build_position_model(..., volume="abc")` still raises `ValueError: could not convert string to float: 'abc'` (run during this re-verification) |
| R10 (LOW, contained) `parse_envelope` `TypeError` escapes `validate()` | **Fixed** | `validate()` now catches `TypeError` too (`server/commands.py:392-393`); test `test_r10_*` |
| A1 Stale ticks corrupt broker-offset detection | **Fixed** | Ticks older than 5 min are stale evidence; configured offset kept (`mt5/driver.py`, `_detect_broker_offset`); tests `test_a1_*` (2) |
| A2 Two different "tradable" answers | **Fixed** | `get_trading_symbols(tradeable_only=...)` now excludes unreported trade mode, matching the inventory rule; test `test_a2_*` |
| A3 Zero-price tick reaches the broker | **Fixed** | `market_order` / `close_position` refuse zero/negative price (`engine/trading.py:164,379` — `if not price > 0`); tests `test_a3_*` (2) |
| A4 `last_error()[0]` indexing after failed connect | **Fixed** | Guarded: `if last_err:` before indexing (`mt5/driver.py:774-795`); test `test_a4_*` |
| A5 `get_order_execution_details` fetches 365 days of deals per call | **Unfixed** | `engine/orders.py:484` still fetches 365 days; `_find_order_everywhere` still runs 1/30/365-day scans (:192) |
| A6 `get_symbol_tick_size/point/digits` fabricate 0.0/0 | **Unfixed** | `engine/symbols.py:230-262` still return `float(... or 0.0)` / `int(... or 0)` |
| A7 `get_trading_history_positions` drops `position_id` 0/None deals | **Unfixed** | `engine/orders.py:287-290` — deals with `position_id` 0/None are still excluded from grouping; a broker that never reports `position_id` still yields an empty closed-positions list |
| A8 Closed-position summary with `volume_in=0` emits `POSITION_PARTIALLY_CLOSED` | **Unfixed** | `journal/collector.py:309` — `fully_closed = vol_out >= vol_in > 0` is False for `vol_in=0`, so the degenerate summary still emits `POSITION_PARTIALLY_CLOSED` |
| A9 `trade_modify_pending_order` defaults missing price/volume to 0.0 | **Fixed** | Volume removed from the modify request (`engine/orders.py:154-156`); price handling resolved in C2 |
| A10 `_recover_ambiguous` fingerprint can false-match manual positions | **Unfixed** | `engine/trading.py:221-260` — fingerprint still type + magic + 30 s window; magic 0 still matches manually opened positions |

**Broker-compat totals: 12 fixed (R1–R6, R10, A1–A4, A9), 1 partial (R7), 7 unfixed (R8, R9, A5–A8, A10).**
The 7 unfixed items are all LOW/MEDIUM defensive or performance/edge-case items; none is a
high-severity correctness defect. R7's remainder (silent-zero volume/price_open) is the most
consequential of them.

---

## New gaps / drift found since `3f0950c`

### N1 — JournalCollector is implemented and tested but never instantiated in production (HIGH — §2.8 is Partial, not Implemented) — ✅ CLOSED in `c27bf0b`

`nse_bridge/journal/collector.py` + `accounting.py` exist with 28 unit tests, sync hooks exist
in `server/sync.py`, and the GUI endpoint `GET /journal/events` exists — but no production
code path ever constructs a `JournalCollector`, and both production `SyncEngine`
constructions omit `journal=` (`api/app.py:162`, `realtime/channel.py:415-424`). The sync
hooks early-return when `self.journal is None` (`sync.py:517,602`). Net effect in the shipped
app: no journal events are ever collected, and the Execution screen's journal feed is always
empty. Fix is small (construct one collector in the app lifespan / channel `_ensure_components`
and pass it to `SyncEngine`), but it is a real wiring gap, not a test gap.
Spec §25 item "Journal events are delivered without duplication" is therefore unmet at runtime,
not just unproven.

**Closure (follow-up branch, `c27bf0b`):** the `create_app` lifespan now constructs
`JournalCollector(checkpoint=sync_engine.checkpoint, persist_checkpoint=sync_engine._persist_checkpoint,
broker_offset_minutes=...)` and assigns it to `sync_engine.journal`. Pinned by
`tests/unit/test_production_wiring.py` (wired journal collects a `position_opened` event).
The `realtime/channel.py` `SyncEngine` construction still builds without a journal (channel
path has no GUI feed) — noted, not a gap. §2.8 and the §25 journal row are updated above.

### N2 — DurableDiagnosticQueue is implemented and tested but never instantiated in production (MEDIUM — §2.12) — ✅ CLOSED in `c27bf0b`

Same pattern as N1: `reliability/diagnostic_store.py` (`DurableDiagnosticStore` /
`DurableDiagnosticQueue`, bounded JSONL) exists with 16 unit tests, but `api/app.py:136-148`
constructs `TelemetryEmitter` with the default in-memory `DiagnosticQueue`, and nothing in
`nse_bridge/` instantiates the durable classes outside their own module. A Bridge restart
still loses queued diagnostic events. Wiring it is a precondition for the D1 delivery work,
not a substitute for it (the sink stays inert until a contract endpoint exists).

**Closure (follow-up branch, `c27bf0b`):** the `create_app` lifespan now constructs
`TelemetryEmitter(queue=DurableDiagnosticQueue(DurableDiagnosticStore(data_dir /
"pending_diagnostics.jsonl")))`. The same commit fixed the latent falsy-queue bug in
`TelemetryEmitter.__init__` (`queue or DiagnosticQueue()` silently replaced an injected
empty queue). Pinned by `tests/unit/test_production_wiring.py`. §2.12 and the §25
diagnostic-delivery row are updated above.

### N3 — OpenAPI path-count drift in `docs/ACCEPTANCE.md` (LOW — docs)

`docs/openapi.json` now has **110 paths** (verified by count), but `docs/ACCEPTANCE.md` #3
and #16 still say "105 paths". The prior audit recorded 104 paths at `3f0950c`. The registry
grew 90 → 91 commands and management endpoints were added; the doc was not re-counted.
Trivial fix (re-export / recount), but it is exactly the class of drift the CI freshness
check is meant to catch — the check verifies the file matches the registry, not that the
prose counts match the file.

### N4 — `docs/ACCEPTANCE.md` #7 overstates the update-failure matrix split (LOW — docs)

ACCEPTANCE #7 says the self-healing matrix covers "update failure split into
download/verification". In code, spec §13 items 19 and 20 share ONE `UPDATE_FAILURE` entry
branching on `update_stage` (`reliability/self_healing.py`). Functionally covered; the
wording should say "one entry branching on update stage", not "split".

### N5 — Chart-frame budget double-charge fixed (POSITIVE drift, §2.10/§17.1)

Not a gap: the Codex P2 fix (`would_allow()` pre-check in `server/chart.py`) corrects a
real budget bug found after `3f0950c` — the 60/min chart-frame budget was effectively ~30/min.
No action; recorded so the fix is not mistaken for an unreviewed change.

### N6 — `docs/PLATFORM-GAPS.md` and `docs/ACCEPTANCE.md` were updated during Phase E, but two claims need the corrections above

`git log 3f0950c..cbfdeeb -- docs/PLATFORM-GAPS.md docs/ACCEPTANCE.md` shows updates landed
in the C2/C4 merges and the coordinator's Phase E pass. ACCEPTANCE #14 correctly refuses a
Windows-green claim (runs in flight). The two corrections owed are N3 (105 → 110 paths)
and N4 (update-failure wording). No other ACCEPTANCE claim was found false in this
re-verification — but note ACCEPTANCE #7/#10 describe the journal and GUI as proven by
unit tests, which is true of the code and false of the runtime wiring (N1): the tests
construct their own collectors, so they cannot catch the missing production wiring.

**Follow-up note:** N1 is now closed (`c27bf0b`), so the ACCEPTANCE #7/#10 wiring
caveat no longer applies to the journal feed; the N3/N4 doc corrections are still owed.

## Real-MT5 defects D1–D4 (found live 2026-11-18, fixed on this branch)

Live test: real MT5 terminal build 6323 under Wine, DEMO MetaQuotes-Demo
login 10011755849, Bridge at `aae5075` on port 8788. Harness: 46 REST calls
(43 ok, 3 failed — all 3 resolved in targeted retest); WS via mock server:
real `close_position` executed (BUY 0.01, ticket 152923734203 closed,
`action.result` executed). Evidence: `~/workspace/mt5-test-results-run2.json`,
`mt5-retest-results.json`, `mt5-ws-test-results.json`. No real money; demo
minimum volume, closed. Each defect was verified to still reproduce at the
follow-up branch head before fixing; each fix has regression tests verified
both ways via `git stash` (new tests fail without the fix).

| # | Defect | Fix (commit) | Tests |
|---|---|---|---|
| D1 | `retcodes.py` mislabels six codes (10025 NO_CHANGES as UNKNOWN_SYMBOL, 10027 CLIENT_DISABLES_AT as LONG_ONLY, 10026 SERVER_DISABLES_AT as ORDER_LOCKED, 10028 LOCKED as SHORT_ONLY, 10030 INVALID_FILL as UNSUPPORTED_FILLING, 10031 CONNECTION as NO_CONNECTION); codes 10032–10045 entirely missing (real LONG_ONLY = 10042). Real effect: a combined SL/TP modify with duplicate values returned NO_CHANGES, was read as UNKNOWN_SYMBOL, retried 3× and failed while the desired state already held. | `28c83d2` — six labels corrected; 10032–10046 added (10046 added to complete the official table; **10037 is unassigned in the official enum and stays unmapped**); `classify_retcode` returns IDEMPOTENT for 10025; `modify_position_sltp` and `trade_modify_pending_order` return success (`status: done`, `no_changes: True`) on NO_CHANGES with no retry. | Label test incl. 10037-unknown, IDEMPOTENT assertion, both NO_CHANGES paths assert a single dispatch. |
| D2 | Account `trade_mode` labelled with the symbol enum: demo account (trade_mode=0) got DISABLED; ENUM_ACCOUNT_TRADE_MODE says 0=DEMO. | `ebc72b2` — new `ACCOUNT_TRADE_MODE_LABELS` (0=DEMO, 1=CONTEST, 2=REAL) used only in `build_account_model`; symbol side untouched. | Demo/contest/real labels + symbol 0 still DISABLED. |
| D3 | `tests/e2e/test_ws_channel.py` stale vs the contract: expected hello → command_result → subscribe/tick; ServerChannel sends no hello, `action.result`, ignores subscribe. The WS command path itself works (proven live). | `67f8ff5` (test-only) — rewritten to the contract protocol: no hello, `action.result` envelopes, contract chart subscription. hello/command_result NOT reintroduced (would violate CONTRACT v1 D-WS-2, §6.5). Handlers record frames + answer heartbeats, never assert (an assert would kill the connection and mask the failure as a reconnect timeout). Fresh ungated test context, `environment=test` for the mock's http URL. | 3 tests collect; all ERROR loudly without MT5. Command + chart legs validated against a live ServerChannel on Linux; executed-close + live-tick assertions need the Wine terminal run. |
| D4 | `/health` reported `mt5_connected:true` while the terminal's broker link was dead 4+ minutes (ticks stale up to 244 s, fresh initialize Authorization failed) — it only measured the IPC link. | `e66951c` — `/health` probes via existing `refresh_terminal()` and returns `broker_connected` (True/False/None) + `broker` dict alongside `mt5_connected`; `ok`/`status` stay process liveness, not broker health. | Broker down when IPC up; broker up; broker unknown when IPC down (real probe, no mock). |

Still unverified (do not claim otherwise): the real Platform server
(`bridge.nexusverse.space`) is still not deployed — deploy prep is done
(nginx vhost file written but NOT enabled, LE cert issued, DNS →
2.31.201.91; gateway = Platform PR #107, still running), and
`modify_pending` via WS is deliberately gated (§17.4) and was not
live-tested.

---

## Spec §25 final acceptance — status at `67f8ff5` (refreshed from the `cbfdeeb` first pass)

| §25 item | Status | Basis |
|---|---|---|
| REST works against the actual platform contract | Not-proven | Mock server only (D11) |
| Persistent WebSocket works correctly | Partial | Unit/mock-proven; live soak owed (D11). D3: the WS e2e tests now match the contract protocol (`67f8ff5`); the executed-close + live-tick assertions need the Wine terminal run |
| Auth and device registration implemented | Implemented (mock-proven) | Enrolment state machine + manifest wiring; live proof owed |
| Connection recovery works | Partial | Reconnect/backoff + 20-scenario matrix unit-proven; live proof owed |
| MT5 terminal discovery works | Partial | Discovery + data-dir + re-discovery unit-proven; no live terminal |
| Multiple terminal handling is safe | Partial | Discovery/selection safe; single-attached only (§2.3) |
| Account information collected correctly | Implemented (mock-proven) | Checkpoint persistence + R5 fix; live proof owed. D2 fixed on this branch: account `trade_mode` now uses `ACCOUNT_TRADE_MODE_LABELS` (0=DEMO) instead of the symbol enum (`ebc72b2`); live re-verification owed |
| Broker-aware symbol discovery works | Implemented (mock-proven) | Resolver + inventory; hidden-symbol completeness unverified live |
| Symbol metadata available | Implemented (mock-proven) | Full broker spec via `get_symbol_info` + inventory |
| Symbol ambiguity handled safely | Implemented (mock-proven) | Ambiguity always reported, never guessed |
| Existing MT5 command infrastructure preserved | Implemented | 91-command registry intact (90 → 91, additive only) |
| Supported remote execution operations work | Partial | 3-op allowlist + lifecycle + reconciliation unit/mock-proven; live execution owed. D1 fixed on this branch: NO_CHANGES is now idempotent success, not a retried error (`28c83d2`) |
| Position management reliable | Partial | Local close/partial/close-by unit-proven; live proof owed |
| Pending-order management reliable | Partial | Stop-limit + modify SL/TP unit-proven; `modify_pending` gate closed, live proof owed. D1: `trade_modify_pending_order` returns idempotent success on NO_CHANGES (`28c83d2`) |
| Order execution states accurate | Partial | `PARTIALLY_COMPLETED` local state + reconciliation; wire status owed (D5). D1: retcode labels corrected (10025 NO_CHANGES, 10042 LONG_ONLY, …) so execution-state classification no longer misreads the broker (`28c83d2`) |
| Duplicate and expired commands handled | Implemented (mock-proven) | Replay cache 10k, expiry enforced, §6.3 order |
| Uncertain executions reconciled | Implemented (mock-proven) | `_reconcile` + R6 tick-tolerance fix; live proof owed |
| Trading history and deal data synchronized | Implemented (mock-proven) | Durable checkpoint + R1 offset fix |
| Journal events delivered without duplication | Partial | N1 CLOSED (`c27bf0b`): the collector is now wired in production and the journal feed is fed. Dedup unit-proven; event delivery to the server still awaits the D6 channel decision |
| Accounting source data available to the server | Partial | Provenance labelling + balance-ops surfacing on the wire; D8 statement owed |
| Operational monitoring works | Partial | Pollers + diagnostics + 20-scenario matrix; no perf measurements (§2.15). D4 fixed on this branch: `/health` now reports `broker_connected` + `broker` alongside `mt5_connected` (`e66951c`) |
| ERROR and WARN events reach the server reliably | **Blocked** | D1 — no endpoint; queue drains by TTL. N2 is closed (durable queue wired, `c27bf0b`), but the sink stays inert until the D1 endpoint exists |
| Diagnostic delivery survives temporary network failures | Partial | N2 CLOSED (`c27bf0b`): `DurableDiagnosticQueue` wired as the emitter's queue, so diagnostics survive a Bridge restart; retry/backoff/dedup/TTL survive disconnects. End-to-end delivery still needs the D1 endpoint |
| Credentials stored securely | Implemented | Keyring/DPAPI, no plaintext; signing key is an operator item, not a code gap |
| Local API protected | Implemented | Mandatory `X-API-Key`, loopback guardrails |
| GUI reflects real infrastructure state | Implemented (mock-proven) | 7 sections, real data endpoints; N1 closed — the journal feed is now fed in production (`c27bf0b`) |
| Secure update verification implemented | Partial | SHA-256 + Ed25519 before staging, stricter than contract; key unprovisioned, no rollback |
| Automated tests run in CI | Implemented | 556 pass locally (verified here, 118.03 s); test-linux + build-windows green at `aae5075` (runs 44274450546 + 44274443061, merged main) and on this branch at `79a2948` (run 44286483717, PR #3). This head differs from `79a2948` by docs only; its own run is being watched before PR #3 is marked ready |
| Windows EXE builds successfully | Implemented | Proven green at `aae5075` (runs 44274450546 + 44274443061: "Build single-file exe" + "Smoke-check the frozen exe", artifact `NSE-MT5-Bridge-windows` 45,377,692 bytes) and on this branch at `79a2948` (run 44286483717: all 14 build-windows steps green, artifact 45,378,534 bytes). This head differs by docs only; its own run is being watched |
| No fake tests or swallowed failures remain | Implemented (mock-proven) | Test-honesty labelling enforced; e2e fails loudly without a terminal |
| No unnecessary local DB / duplicated server engine | Implemented | No local DB; journal/accounting are labelling/collection only, no engine |
| Future compute has a clean extension boundary | Partial | Boundary + timeout enforcement verified; CPU/memory recorded-not-enforced; no per-task cancel; wire format owed (D9) |
| One integrated PR created, all CI failures addressed | Partial | PR #2 MERGED (`4f01068`, head `aae5075`, CI green). Follow-up PR #3 is open (draft, `fix/bridge-real-mt5-defects` → main); run 44286483717 at `79a2948` green. Marking PR #3 ready awaits this head's own run (docs-only change since `79a2948`) |
| PR remains unmerged pending explicit authorization | Holding | PR #3 is a draft; no merge has happened on it (PR #2 merged only on the user's explicit order) |

**§25 totals at `67f8ff5` (refreshed for the PR #3 head — see note): Implemented 16, Partial 15, Not-proven 1, Blocked 1, Not-met-at-runtime 0, Holding 1.**
(Correction: the first-pass totals line read "Implemented 10, Partial 14,
Not-proven 3, Blocked 2, Not-met-at-runtime 1, Holding 1" (= 31, not 34) —
a counting error; recounting that table's rows gives Implemented 13,
Partial 15, Not-proven 2, Blocked 2, Not-met-at-runtime 1, Holding 1.
The refresh moves journal Not-met→Partial, GUI Partial→Implemented,
diagnostic delivery Blocked→Partial, Windows EXE Not-proven→Partial.
The PR #3 head refresh moves CI tests + Windows EXE Partial→Implemented:
run 44286483717 at `79a2948` is green; this head differs by docs only.)
(No §25 item is fully closed by Bridge code alone while D11/D1 are open — live proof and the
diagnostics endpoint are Platform-side gates, not Bridge defects.)

---

## Verdict

The branch has moved decisively since `3f0950c`: **every §5 Phase B/C item is now implemented
in code** (manifest wiring, heartbeat→WS wake, request cancellation, durable sync checkpoint,
terminal data-dir, broker-discovered offset probes, 20-scenario self-healing, symbol resolver
+ inventory, stop-limit pending, close-by, `PARTIALLY_COMPLETED`, journal collector +
accounting provenance, 7-section GUI, whole-repo lint/format/mypy/security CI gates), the
test suite grew **311 → 556 passing (verified here)**, **12 of 20 broker-compat items
are fixed with tests**, the two new wiring gaps **N1/N2 are closed** (`c27bf0b`), the
Windows EXE build is **proven green on merged main** (`aae5075`), and the **4 real-MT5
defects are fixed with regression tests**.

**What still blocks a "complete" verdict — in priority order:**

1. **Follow-up branch CI (green at `79a2948`, this head's run pending):**
   run 44286483717 (PR #3) is SUCCESS — test-linux + build-windows,
   incl. exe build + frozen-exe smoke check. This head differs from
   `79a2948` by docs only; its run is being watched before PR #3 is
   marked ready.
2. **Live re-verification of the 4 defect fixes:** unit-verified here (both ways via
   `git stash`); the Wine terminal re-run (NO_CHANGES modify, demo trade_mode label,
   `/health` broker state, WS e2e executed-close + live tick) is owed via the parent.
3. **Operator items (unchanged):** provision the release-signing public key, supply or
   waive the `.ico`, decide the rollback strategy, cut the first tagged release.
4. **Platform/user decisions (unchanged):** D1 diagnostics endpoint, D5 wire representation
   for partial completion + envelope `volume`/`close_by_ticket`, D2/D3/D4 allowlist
   extensions + live proofs, D6/D7/D8 channel decisions, D11 live Platform + migration
   0054, `open_order` user decision (§17.5). Deploy prep for the gateway is done
   (vhost file written but NOT enabled, LE cert issued, DNS → 2.31.201.91) but the
   endpoint is still not serving; the holding-vhost option was declined by the user.
5. **Leftover broker-compat (7 items, all LOW/MEDIUM):** R8 (`"USD"` invention, 8 sites),
   R9 (builder `ValueError` on non-numeric strings — verified live), R7 remainder
   (silent-zero `volume`/`price_open`), A5 (365-day deal fetch), A6 (fabricated 0.0
   tick size/point/digits), A7 (`position_id` 0/None dropped), A8 (degenerate
   `POSITION_PARTIALLY_CLOSED`), A10 (ambiguous-fill fingerprint false-match risk).
6. **Still missing by design/scope (unchanged, need decisions not code):** independent
   per-terminal connection state, dedicated MT5-restart probe, local kill switch,
   measured performance evidence, written storage-retention policy, installer.

**What was NOT verified in this re-verification (stated, not claimed):** live Platform
behaviour; live MT5 behaviour of the 4 defect fixes (no terminal on this VM); the
follow-up branch's CI runs (not yet started); hidden-symbol completeness of
`symbols_get()`; per-category telemetry emission completeness.

---

## Appendix — evidence index

- **Branch/head/tree:** `git log --oneline main..HEAD` (43 commits from main to
  `67f8ff5`), `git rev-parse HEAD` = `67f8ff5281728f96b887fde575bfe8e9928229ec`,
  `git status --short` clean except this file. Follow-up branch commits:
  `c27bf0b` (N1/N2 wiring), `28c83d2` (D1), `ebc72b2` (D2), `e66951c` (D4),
  `67f8ff5` (D3); base is merged main `4f01068` (PR #2).
- **Test run (this re-verification):** `~/.venvs/bridge/bin/python -m pytest tests/unit
  tests/integration -q` → `556 passed, 1 warning in 118.03s` (clean re-run; an
  earlier run under heavy VM load showed 555 passed + 1 failed on
  `test_channel_connects_only_after_registered` — passes alone, as a file, and
  on re-run, i.e. a load flake; no test weakened). Defect test files alone:
  58 passed. E2E `tests/e2e/test_ws_channel.py`: 3 tests collected, all ERROR
  loudly at the fixture without MT5 (by design).
- **Live check (this re-verification):** `build_position_model(..., volume="abc")` →
  `ValueError: could not convert string to float: 'abc'` (R9 unfixed).
- **Wiring greps (N1/N2 closure verified, not assumed):** `JournalCollector(` —
  production hit at `nse_bridge/api/app.py:190` (lifespan); `DurableDiagnosticQueue(` /
  `DurableDiagnosticStore(` — production hit at `nse_bridge/api/app.py:155`.
  `rollback` — zero hits in `nse_bridge/`; `kill_switch` — zero hits;
  `EURUSD|GBPUSD|USDJPY` — zero hits in `mt5/driver.py`; `PUT|PATCH|DELETE` — zero call sites in `server/rest.py`;
  `git tag` — empty.
- **Counts verified:** `@command` decorators = 91; e2e `def test` = 104 (3 in the
  rewritten `test_ws_channel.py`); OpenAPI paths = 110;
  self-healing `_handle_*` methods = 20; journal event types = 14.
- **CI evidence (merged main + this branch, verified via GitHub API by full head SHA):**
  runs 44274450546 (PR #2) + 44274443061 (push) at `aae5075` both SUCCESS
  (test-linux + build-windows; release skipped — tags only). Follow-up branch:
  run 44286483717 (PR #3) at `79a2948` SUCCESS — test-linux success,
  build-windows success (all 14 steps, incl. "Run unit + mock-integration
  tests on Windows", "Build single-file exe", "Smoke-check the frozen exe";
  artifact `NSE-MT5-Bridge-windows`, 45,378,534 bytes), release skipped.
  This head differs from `79a2948` by docs only; its own run is being watched.
- **Key code paths cited:** `server/rest.py`, `server/commands.py`, `server/sync.py`,
  `server/sync_checkpoint.py`, `server/manifest.py`, `server/enrolment.py`,
  `server/chart.py`, `realtime/channel.py`, `realtime/bus.py`, `mt5/driver.py`,
  `mt5/types.py`, `symbols/{canonical,resolver,inventory,metadata}.py`,
  `journal/{collector,events,accounting}.py`, `engine/{symbols,execution,orders,
  positions,trading,account,risk}.py`, `reliability/{telemetry,self_healing,
  diagnostic_store,result_store}.py`, `tasks/{envelope,worker}.py`, `api/app.py`,
  `api/management.py`, `gui/static/index.html`, `updates.py`, `logging_setup.py`,
  `bridge.spec`, `.github/workflows/build.yml`, `scripts/mock_server.py`,
  `scripts/annotate_test_failures.py`, `docs/ACCEPTANCE.md`, `docs/PLATFORM-GAPS.md`,
  `docs/JOURNAL.md`, `docs/SYMBOLS.md`.
