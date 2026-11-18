# Acceptance — verdict per criterion (user spec §15)

Honest verdicts for the client-only refactor, branch
`refactor/client-only-bridge`, Phase 6. **Pass** = proven by an
executed test, a CI gate, or a verified artifact on this branch.
**Partial** = implemented and unit/mock-tested, but a named piece is
still owed. **Not-proven** = cannot be proven from this VM; the
evidence that would prove it is named. Nothing here is inflated.

| # | Criterion | Verdict | Evidence / what is still owed |
|---|---|---|---|
| 1 | Client/server separation | **Pass** | Local 91-command registry stays intact; the server channel enforces the §6.1 3-op allowlist at the boundary (`server/commands.py`, `SERVER_ALLOWLIST`). Local-only ops can never arrive from the server (validation step 5). Proven by `tests/unit/test_server_commands.py` + `test_registry.py`. |
| 2 | Server-owned logic removed | **Pass** | `nse_bridge/agent/` deleted; `api/agent_routes.py`, GUI AI tab, `test_agent.py`, AI config fields, hardcoded magic 888101 / `bot_symbol="XAUUSD"` / `default_timeframe="M1"` all removed. Grep for `llm\|coach\|router9\|promptmaster\|gpt-4o\|agent/` returns only the `ai_coach` contract entitlement field, the legacy-key migration list, and a "no coaching" docstring. Risk calculators 73–81 KEPT locally per the correction (local capability; server channel excludes them). |
| 3 | MT5 commands available via documented APIs | **Pass** | 91 local commands, `POST /api/v1/{name}`, documented in docs/COMMAND_REFERENCE.md (generated table from the live registry) + `docs/openapi.json` (105 paths, re-exported, CI freshness-checked) + Swagger. Registry tests assert exactly 91, numbers 1–91. |
| 4 | Secure auth | **Pass** | Secrets in Credential Manager/DPAPI, never plaintext config.json (migration + deletion on first run); mandatory `X-API-Key` on the local API; loopback-only bind with guardrails; Bearer + `X-NSE-Device-Id` on the server channel; `wss://` mandatory; log redaction. Proven by `test_secrets.py`, `test_local_api_auth.py`, `test_host_guardrail.py`, `test_redaction.py`. Residual: the release-signing public key is unprovisioned (updater fail-closed, not a bypass). |
| 5 | Persistent comms | **Partial** | WS session (15 s heartbeat, 45 s dead-after), REST heartbeat, enrolment state machine, bounded outbound queue (128, results first). Proven unit + mock-integration (`test_enrolment.py`, `test_mock_server.py`, `test_channel_backoff.py`). **Owed:** a live long-run soak against the real Platform (unverified endpoint — docs/PLATFORM-GAPS.md §6). |
| 6 | Reconnection / sync | **Partial** | Reconnect 500 ms doubling → 30 s + jitter, fresh session, no resume payload; server re-sends only `pending`; sync/batch is the only sync channel (page 500, 250 ms, checkpoint never backwards, queue bound 32); account-switch reset. Proven unit + mock-integration. **Owed:** live reconnect/resume proof against the deployed Platform. |
| 7 | Discovery / diagnostics | **Pass** | Multi-terminal discovery (env → standard paths → App Paths → custom/portable → running processes), version detection, guided diagnostics with structured codes (`MT5_NOT_FOUND`, `MT5_NOT_RUNNING`, `MT5_IPC_FAILED`, `MT5_ACCOUNT_NOT_READY`, `MT5_UNSUPPORTED_VERSION`), retry + copy-diagnostics; 15-scenario self-healing matrix with one unit test per scenario; telemetry 9-field shape. Proven by `test_mt5_discovery.py`, `test_self_healing.py`, `test_telemetry.py`, `test_gui_control_center.py`. |
| 8 | Command execution + result reporting | **Partial** | §6.2 envelope, §6.3 validation order, lifecycle (RECEIVED → … → COMPLETED/PARTIALLY_COMPLETED/FAILED/UNKNOWN/EXPIRED), `action.result` (§6.5), replay cache (10,000), reconciliation of `UNKNOWN` (never blind-retry), results persisted across reconnects. Partial closes reconcile against live MT5 truth (requested partial volume or retcode 10010 `DONE_PARTIAL`, never retried); wire status stays `executed` with the partial facts in the payload (Platform dependency D5). Proven unit + mock-integration. **Owed:** live execution of `close_position` / `modify_sl_tp` against a real terminal with `action.result` reconciliation, and the `modify_pending` §17.4 close-out (live proof + published minimum version). |
| 9 | Backtest | **Intentionally excluded** | Per clarification #2: NO backtest work in this phase. Zero backtest code, no screen, no status. This is a deliberate scope exclusion, not a failure — recorded here so it is never mistaken for an oversight. |
| 10 | GUI relevant controls only | **Pass** | Exactly 6 screens (Connection, MetaTrader, Account & Subscription read-only, Diagnostics, Updates & Releases, Settings). No trading dashboard, no positions/orders screens, no AI screens, no Backtest screen. Proven by `test_gui_control_center.py` (`test_gui_html_has_exactly_six_screens`). |
| 11 | Server-driven subscription | **Partial** | Subscription/entitlements are server-resolved via `validate-key` (`user_id`, `subscription`, `entitlements`), read-only in the GUI; `session/refresh` + `session/revoke` implemented ([client-v1.1]); `manifest` sends only truthfully mappable fields. **Owed:** live proof against the deployed Platform, and the telemetry delivery endpoint (CONTRACT v1 §4.2 defines none — docs/PLATFORM-GAPS.md §2). Telemetry delivery itself is therefore partial by contract, not by implementation. |
| 12 | Build compatibility documented / tested | **Partial** | x64 primary, documented (docs/COMPATIBILITY.md, docs/BUILD.md); Linux gate proven here; Windows job defined in CI. **Owed:** an actual green `build-windows` run (never executed from this ref) and the frozen-exe smoke check; ARM64 explicitly unsupported (no build published). |
| 13 | Releases publish verified artifacts | **Partial** | Release engineering complete in code: `console=False` + `--diagnostic`, version metadata + manifest generated from `BRIDGE_VERSION`, SHA-256 + `release-manifest.json` via `write_release_manifest.py`, tags-only release job, stable/prerelease split. **Owed:** the first real tagged release (no tag exists yet), the provisioned release-signing public key (updater cannot install without it), and a real `.ico` asset. |
| 14 | CI tests pass | **Partial** | Linux gate proven here: `pytest tests/unit tests/integration -q` exit 0 (302 passed at baseline; 311 with the 9 Phase-6 tests — see docs/RELEASE.md §4), `ruff check tests/ scripts/` clean. **Owed:** a green CI run on the PR (Linux + Windows jobs) — the workflow is written but has not executed. |
| 15 | E2E Demo tests pass | **Not-proven** | The 104 e2e tests collect cleanly on Linux but **cannot run here** (no MT5 terminal) and are **never run in CI** (no terminal on GitHub-hosted runners). On Linux they fail loudly by design (104 errors at fixture setup, 0 passed) — which proves test honesty, not e2e success. Real proof requires a manual Windows-with-MT5 run on Demo accounts. The earlier "104/104 e2e" claim in Platform PR #107 was never verified by this team. |
| 16 | Docs match implementation | **Pass** | All 9 docs were written in Phase 6 against the code (not the plan): command table generated from the live registry, endpoint lists from the route tables, constants quoted from the modules (backoff, queue bounds, chart limits, retry policy), verification status stated as executed. `docs/openapi.json` re-exported (105 paths) with a CI freshness check; `docs/PLATFORM-GAPS.md` kept and cross-referenced, not duplicated. |
| 17 | PR reviewed / merged only after explicit approval | **Pass (process)** | No merge has happened; no PR has been created in this phase; the coordinator pushes and the user approves. This criterion is satisfied by process, not by code — it stays open until the user explicitly approves the merge. |

## Summary

- **Pass:** 1, 2, 3, 4, 7, 10, 16, 17 (process).
- **Partial:** 5, 6, 8, 11, 12, 13, 14 — implemented and
  unit/mock-tested; each names the live/Windows/external proof still
  owed.
- **Not-proven:** 15 (e2e Demo tests — Windows-with-MT5-only, manual).
- **Intentionally excluded:** 9 (backtest).

The PR is **not** production-proven until the Windows CI job goes
green, the first tagged release publishes a verified exe, the e2e
suite runs against a real Demo terminal, and the live Platform
round-trip is observed. None of those are claimed here.
