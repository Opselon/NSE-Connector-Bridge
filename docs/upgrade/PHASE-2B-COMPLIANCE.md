# Phase 2b — W0 baseline & compliance matrix (2026-11-19)

Recorded before any implementation change, per the Phase 2b mission
(W0). Branch `feat/bridge-phase-2b`, cut from `main` at `ebc345f`
(PR #5 merge). Nothing in this file is carried over from the
historical claims — every number below was re-measured on this
machine, today.

## Baseline (measured 2026-11-19, this host)

| Check | Command | Result |
|---|---|---|
| Unit + mock-integration tests | `pytest tests/unit tests/integration -q` | **536 passed, 20 failed** (53.8 s) — see the failure analysis below |
| Lint | `ruff check .` | pass |
| Format | `ruff format --check .` | pass (149 files) |
| Typing (CI gate) | `mypy nse_bridge` and `mypy nse_bridge --platform win32` | pass (66 source files) |
| Typing (whole tree) | `mypy .` | **72 errors in 9 files** — all in `tests/unit` + `scripts/mock_server.py`; CI does not check these. Latent debt, listed as an action, not a regression |
| Security (CI gate) | `bandit -r nse_bridge --skip B101,B110,B311,B404 -q` | pass |
| Security (raw) | `bandit -r nse_bridge -q` | 27 findings, all B101/B110/B404/B311 — the four classes CI documents and skips by name |
| Dependency audit | `pip_audit -r requirements.txt -r requirements-dev.txt` | no known vulnerabilities |
| Version metadata | `python scripts/write_version_info.py --check` | pass (exit 0) |
| OpenAPI freshness | `scripts/export_openapi.py` + diff | fresh (110 paths) |
| Windows build | CI `build-windows` on `main` | green at this commit (PR #5 run); local EXE build re-proven in W10 |
| Installer identity | `windows/NSE-MT5-Bridge.iss` | AppId `b80bf455-6a18-41e5-bfa2-03a04736ee8f` confirmed present, unchanged |
| Releases | `gh release list` | `v1.0.0` (2026-11-18, Latest). No new tag is created by this task |

### The 20 test failures — environment, not regressions

Every failure is one class: **this host has a real MT5 terminal
installed (`C:\Program Files\MetaTrader 5\terminal64.exe`,
v5.0.0.6323, running) plus real AppData data directories, and the
discovery/diagnostics tests fake an environment WITHOUT one.** The
real terminal leaks into `discover_terminals()` results that the
tests expect to contain only their fixtures.

- 19 × `tests/unit/test_mt5_discovery.py` — e.g.
  `test_discover_terminals_appdata_data_dir_ambiguous_two_dirs`
  asserts `len(found) == 1` and finds 2: the fixture terminal AND the
  real `C:\Program Files` terminal.
- `tests/unit/test_gui_control_center.py::test_diagnostics_summary_mt5_not_found`
  — expects `MT5_NOT_FOUND`; the real terminal IS found.
- `tests/unit/test_test_honesty.py::test_required_mt5_test_fails_loudly_without_a_terminal`
  — asserts e2e errors loudly when no terminal exists; on this host
  one exists, so the precondition is false.

CI (Ubuntu runner + Windows runner, neither with MT5) passes the same
tests at the same commit — `build.yml` runs 44274450546/44580667045
green. The failures therefore prove discovery works against a real
install; they also expose that these tests are not hermetic on an
MT5-equipped host. Required action (implemented in this PR): patch
the standard-path + process-discovery sources in the affected tests
so they are deterministic on any host — this strengthens the tests
(no assertion is loosened; the loud-failure honesty suite is
preserved).

### Live execution environment (W11 availability)

- Official MetaTrader5 Python API `initialize()` succeeds against the
  running terminal.
- Account `MetaQuotes-Demo`, `trade_mode: 0` (**DEMO**), balance
  reported, `margin_mode: 2` (RETAIL_HEDGING). Secrets are not
  recorded here.
- Fresh EURUSD tick observed at probe time — **market open** (weekday
  session).
- Conclusion: live Demo verification (W11) is available on this host;
  every live result is recorded in `docs/upgrade/PHASE-2B-VERIFICATION.md`
  with retcodes and before/after broker state.

## Compliance matrix

Classifications: **Implemented+verified** (proven by an executed test
or CI gate), **implemented-not-verified** (code exists, live proof
owed), **partial**, **missing**, **platform-owned**, **blocked-user**
(user/Platform decision outstanding), **blocked-infra**
(infrastructure unavailable).

| Requirement | Current State | Evidence | Required Action → Status in this PR |
|---|---|---|---|
| Contract v1 wire compliance (§6.2 envelope, §6.3 validation order, §6.5 `action.result`) | Implemented+verified (offline) | `server/commands.py` (parse/validate in exact §6.3 order, replay cache 10k, lifecycle) + `tests/unit/test_server_commands.py` (22+ tests), `tests/integration/test_mock_server.py` | Keep green; extend tests for new ops |
| Execution lifecycle (RECEIVED→…→COMPLETED/PARTIALLY_COMPLETED/FAILED/UNKNOWN/EXPIRED) | Implemented+verified (offline) | `CommandState` machine + legal transitions (`commands.py:220`), pinned by unit tests | Keep green |
| MT5 IPC serialization (single worker thread, no MT5 from handlers) | Implemented+verified | `Mt5Driver.run()` jobs queued to one worker thread (`mt5/driver.py:619,634`); handlers use `run_in_threadpool` around synchronous engine calls; test honesty suite pins no-direct-MT5 imports | Keep — new code (order_check, policy executor) goes through the same worker |
| `modify_pending` (§17.4) | Implemented, gate CLOSED (blocked-user for (d), live proof (c) owed) | `SERVER_ALLOWLIST` includes it; `CommandValidator.modify_pending_proven=False` rejects as `UnsupportedOperation`; routing to `trade_modify_pending_order` (SL/TP only) implemented; tests cover gate closed+open, wrong device/account, expiry, replay | W1: live proof run at market open on the Demo terminal (done in this PR, recorded); (d) published minimum connector_version stays Platform-owned → **gate stays closed by default** |
| Mandatory `order_check` before every financial send (§17.5/CONTRACTS §7) | Partial | Official `mt5.order_check` exists only in the local `validate_trade_request` risk command (`engine/risk.py:587`); the server execution path does NOT run it before `order_send` | **W2: integrate into the gateway execution path** (close/modify/open), preserve raw check result, refuse on failure |
| Partial completion / reduce-volume (D5 wire gap) | Partial (local complete, wire blocked) | `close_position(ctx, ticket, volume)` reconciles DONE_PARTIAL against live truth; `trade_close_partial_position` local command 68; `PARTIALLY_COMPLETED` local state; wire has no `volume` field (§6.2) and no partial status (§6.5) | W3: server-path volume support behind a gate + exact wire-shape proposal in the PR body; wire activation stays platform-owned |
| Uncertain execution reconciliation (None / retcode 10031) | Partial | `TRADE_UNVERIFIED` → `_reconcile` inspects positions/orders before any retry (`commands.py:938`); mock tests prove lost-response recovery. **Gap found in W0 review:** `close_position` retries after `None` (spec §7 + W4 forbid re-send before reconciliation); retcode 10031 is classified TRADE_REJECTED (broker rejection) instead of uncertain | **W4: stop re-send paths; reconcile on None AND 10031; truthful UNKNOWN; tests** |
| `open_order` (§17.5) | Missing on the server path (local `trade_send_market_order`/`trade_send_pending_order` exist and are test-rejected at the server boundary) | `SERVER_ALLOWLIST` excludes it; §6.1 notes its tests | **W5: implement model/validation/execution/reconciliation behind an inactive gate; default path still rejects** |
| Policy envelope (D-5, CONTRACTS §4.10) | Missing | No envelope parse/validate/freshness/WS-gate anywhere in `nse_bridge/` | **W6: parse+validate envelope, ≤5 min freshness, WS-health gate, fail-closed, trailing/breakeven only, every move traceable (rule_id/decision_id/envelope version)** |
| Broker-aware symbol discovery | Implemented-not-verified (live) | `SymbolResolver`/`SymbolInventory`/`SymbolMetadata` + 29 unit tests; ambiguity never guessed | W7: live broker-inventory proof on the Demo terminal (available here) |
| Chart protocol (§17.1) | Implemented+verified (offline) | `server/chart.py`: 20-subscription cap, 60 frames/min budget, market-watch prerequisite, bounded queue, timeouts, error codes; unit+integration tests | W7: live gateway-protocol proof where infra allows; mock proof otherwise |
| Telemetry 9-field pipeline | Implemented+verified (offline) | `reliability/telemetry.py` 9 fields, bounded queue, retry/backoff/dedup/expiry, 19 tests; durable queue wired in production (`test_production_wiring.py`) | W8: preserved; no INFO-noise added to Platform layers; diagnostics upload stays inert (no §4.2 endpoint — D1, platform-owned) |
| Raw broker facts & traceability | Partial | `action.result` carries payload/actual_mcp_result/retcode/timing; W9 tests pinning full fact preservation for a close result are owed | **W9: add explicit fact-preservation tests + document trace fields** |
| Windows packaging (v1.1.0) | Implemented, version bump owed | BRIDGE_VERSION 1.0.0; version_info + manifest generated; installer .iss present; CI builds+smoke-checks | **W10: bump to 1.1.0, regenerate artifacts, local EXE build + smoke, AppId immutable** |
| Release signing | Blocked-user (fail-closed by design) | `updates.py` refuses installs without a provisioned Ed25519 public key (`NO_PUBLIC_KEY`) | Keep fail-closed; key provisioning stays with the authorized operator (`~/.nse-secrets` holds a local copy — NOT read or embedded by this task) |
| Icon asset | Missing (blocked-user) | `windows/` holds only the manifest; no approved .ico source | Do not fabricate; keep documented |
| Live Platform round-trip (D11) | Blocked-infra | `bridge.nexusverse.space` vhost written but not enabled (PLATFORM-GAPS §6) | Report as unavailable; mock integration remains the executed proof |
| Demo probation thresholds | Blocked-user | Not configured anywhere | Stays a proposal in the PR body, not policy |
| `open_order` activation | Blocked-user | Gate default inactive | Status of all four activation requirements tracked in the PR body |

## How this PR is organized

One branch, one PR, corrective commits pushed to the same branch:

1. W0 — this matrix (no code change).
2. W1–W6 — execution-safety core (order_check, reconciliation,
   partial close, open path behind gates, policy envelope).
3. W7–W9 — verification hardening + tests.
4. W10 — version 1.1.0 + packaging.
5. W11 — live Demo verification records
   (`docs/upgrade/PHASE-2B-VERIFICATION.md`); every unproven scenario
   is listed with its blocker; nothing mocked is reported as live.

## Post-implementation status (same PR, recorded after W1–W11)

The matrix above is the W0 snapshot. Deltas delivered by this PR:

| Item | W0 state | Now |
|---|---|---|
| Mandatory `order_check` | Partial | **Implemented+verified** — official pre-send check in the gateway execution path (close/modify/pending/open), raw result preserved, refusal never reaches `order_send`; live-verified (E-OC-1, E-P2B-2..6) |
| Uncertain-result reconciliation | Partial (re-send paths existed) | **Implemented+verified** — None/10031 stop the send loop and reconcile against live truth; live proof with a real lost response (E-P2B-5); 20 unit tests pin no-re-send |
| Partial close / reduce-volume | Local only | Server path accepts the proposed `volume` field (absent = v1 full close); live partial close with distinct requested/filled/remaining facts (E-P2B-4); wire shape proposed in the PR body |
| `open_order` | Missing on the server path | Technical path implemented behind the CLOSED §17.5 gate (market + pending kinds); default still `UnsupportedOperation`; reconciliation wired |
| Policy envelope (D-5) | Missing | `server/policy.py`: §4.10 validation (≤5 min window), fail-closed gate (freshness + WS health + rule/bounds/account checks), trailing/breakeven executor with rule_id/decision_id/version traceability; 19 tests; dormant until the Platform adopts the proposed frame |
| 20 environment-contaminated test failures | Failing on MT5-equipped hosts | **0 failures** — discovery/GUI fixtures are hermetic (host sources isolated, no assertions loosened) |
| e2e suite | Never executable (401 on every call) | **104/104 PASSED live on the Demo terminal** (E-P2B-7) — first real execution in project history |
| `modify_pending` §17.4 (c) | Owed | **Live proof delivered** (E-P2B-6); (d) stays Platform-owned; gate closed |
| Real-MT5 defect D5 | Undetected | **Fixed** — close-by hedging check inverted (1 vs 2) + `MARGIN_MODE_LABELS` swapped; verified against package constants live |
| Version | 1.0.0 | **1.1.0** — version_info/manifest/OpenAPI regenerated; EXE built + smoke-tested locally (44.2 MB single-file) |
