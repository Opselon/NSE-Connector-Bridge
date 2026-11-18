# Troubleshooting

Start here: open the Control Center → **Diagnostics** screen
(`GET /api/v1/diagnostics/summary`), or run the pre-flight checks
(`POST /api/v1/run_connection_checks`). The Diagnostics screen shows
bridge/MT5/network/WS health, last errors, and the self-healing
states; every failure carries a structured code, not a raw stack
trace. For verbose output, restart with `--diagnostic`
(DEBUG telemetry + DEBUG logs).

## 1. Guided MT5 diagnostics codes

| Code | Meaning | Fix |
|---|---|---|
| `MT5_NOT_FOUND` | No terminal discovered at the configured path or anywhere searched | Install MT5, or set the terminal path in Settings → MetaTrader (`mt5_terminal_path`), or add directories to `mt5_custom_terminal_dirs` (custom/portable installs). Discovery never overwrites the stored path automatically. |
| `MT5_NOT_RUNNING` | Terminal not running (or its process died) | Start the terminal (MetaTrader screen → Open), then Retry. No execution is attempted while MT5 is down. |
| `MT5_IPC_FAILED` | Terminal process is up but the Python IPC channel fails | Restart the terminal; if it persists, restart the Bridge. Check the terminal is not blocked by antivirus/firewall from loading its Python API. |
| `MT5_ACCOUNT_NOT_READY` | Terminal attached but no account is logged in | **Sign in through the MT5 terminal itself.** The Bridge never asks for your MT5 password and there is no password field anywhere. |
| `MT5_UNSUPPORTED_VERSION` | Terminal build is too old/new for the driver | Update the terminal (or the Bridge, if a newer minimum is published). |
| `MT5_NOT_CONNECTED` (local API error) | A local command ran while the driver was disconnected | `connect_mt5` first (Connection screen or `POST /api/v1/connect_mt5`). |

## 2. "AutoTrading disabled" — the hidden killer (MT5 build 6306+)

Symptom: every Python-API order is rejected with retcode **10027**
("AutoTrading disabled by client") even though the toolbar button
looks green.

Cause: *Tools → Options → Experts → **"Disable algorithmic trading
via external Python API"*** is CHECKED. Uncheck it. (The underlying
setting is `Api=1` → `Api=0` in `common.ini [Experts]` + terminal
restart.) The Connection Checks tab detects this failure and prints
the exact fix.

Also confirm Algo Trading is ON in the terminal (`Ctrl+E` until the
toolbar button is green — the Options checkbox alone is not enough)
before running the e2e trade suites.

## 3. Server channel problems

| Symptom | Likely cause | Fix |
|---|---|---|
| `key_invalid` / `key_revoked` (401) | Dead API key | Enter a **new** key in Settings. Re-enrolment cannot fix these; the Bridge never retries with the same key. |
| `subscription_expired` / `device_limit_exceeded` / `account_not_authorized` / `feature_not_enabled` (403) | Server-side entitlement problem | Fix it on the Platform side (subscription, device allowance, account authorization, feature flag). The Bridge will not evict another device or fall back to unauthenticated mode. |
| `rate_limited` (429) / 5xx | Server busy/down | Wait — the client backs off (max 3 retries, 0.5 s doubling, honours `Retry-After`). If it persists, the Platform is down; see self-healing scenario `platform_down`. |
| WS never connects, REST works | WS endpoint down or URL wrong | Check `server_base_url` (production default `https://bridge.nexusverse.space`) and that `server_url`, if set, is `wss://`. The REST heartbeat keeps liveness while WS is down. |
| WS connects, then drops repeatedly | Network / server restart | Expected handling: fresh session, 500 ms doubling → 30 s + jitter; pending results re-sent on reconnect; the server re-sends only `pending` commands. Check Diagnostics → self-healing `ws_drop` / `server_restart`. |
| `session_expired` | Session lapsed | Automatic: re-enrol via `register-device`. If the state is `failed_auth`, a new key is required (see above). |
| Commands rejected `WrongAccount` | MT5 logged into a different account than the command names | Switch the terminal to the bound account, or re-bind the device. The Bridge never executes an old account's command on a new account. |
| Commands rejected `Expired` | Clock skew or a stale re-issue | Check the machine clock (UTC). The server must re-issue with a new `command_id` if still wanted. |
| Commands rejected `Replayed` | Duplicate `command_id` | The original already executed (or is executing). Do not re-send the same id; the server must issue a new one. |
| `modify_pending` rejected `UnsupportedOperation` | Capability gate closed (§17.4) | Expected until live proof + published minimum version land (docs/PLATFORM-GAPS.md §4). The server must not send it before then either. |

## 4. Local API problems

| Symptom | Fix |
|---|---|
| `401 Missing or invalid X-API-Key` | Present the key from the secret store (Settings screen shows its status masked; the key itself is never displayed). The key is auto-generated on first run — if it was lost, delete it from the secret store (`NSEBridge` service) and restart the Bridge to generate a new one. |
| Bridge refuses to start: `refusing non-loopback bind host` | Either bind loopback (default `127.0.0.1`) or set `allow_non_loopback=true` explicitly (config or `NSE_BRIDGE_ALLOW_NON_LOOPBACK=1`) — the mandatory `X-API-Key` still applies. |
| `GET /health` works but everything else 401s | Normal: `/health` is the unauthenticated liveness probe; all API routes require the key. |
| Settings changes do not apply | `PUT /settings` applies live; a bad value is skipped, not fatal (check the response for what was applied). Secrets are never accepted in plaintext via config.json — they are migrated to the store. |

## 5. Updates

| Symptom | Meaning |
|---|---|
| Update check says "no public key configured" / download refuses `NO_PUBLIC_KEY` | Expected today: the NSE release-signing public key is not provisioned. Set `NSE_BRIDGE_UPDATE_PUBLIC_KEY` (base64 raw 32-byte Ed25519 public key) once the operator publishes it. The updater refuses to install an unverifiable binary — do not work around this. |
| `SIGNATURE_MISSING` / `SHA256_MISSING` / `SHA256_MISMATCH` / `SIGNATURE_INVALID` | The release metadata or bytes are not authentic. Nothing is staged. Report it; do not install manually. |
| `MIN_VERSION_NOT_SATISFIED` | The release needs a newer connector than the one installed. Update the connector first (or wait for a compatible release). |
| `not_frozen_no_target` on install | A verified update is staged, but this is not a packaged exe run (Linux/dev). Restart install only applies to the frozen Windows exe. |

## 6. Logs and diagnostics

- Log dir: `<data_dir>/logs` (`%LOCALAPPDATA%\NSEBridge\logs`
  frozen, `~/.nse-bridge/logs` dev). Structured JSON lines in
  `bridge.jsonl`; human-readable `bridge.log`. Secrets are redacted.
- `GET /api/v1/get_bridge_logs` (local command 83) and
  `GET /api/v1/diagnostics/summary` expose the same state to the GUI.
- Telemetry events are structured JSON with exactly 9 fields
  (docs/ARCHITECTURE.md §5). `DEBUG` events appear only in
  diagnostic mode (`--diagnostic`).
- Copy-diagnostics (Diagnostics screen) copies the structured
  summary — paste that, not screenshots of stack traces, when
  reporting a problem.

## 7. Tests failing on your machine

- `tests/unit` / `tests/integration` failing on Linux: read the
  failure — these need no MT5 and must pass. Check the venv matches
  `requirements-dev.txt` (Python 3.12/3.13).
- `tests/e2e` failing on Linux with `MT5 terminal unavailable`:
  **expected.** E2E requires a Windows machine with a live MT5
  terminal (Demo account, Algo Trading ON). They fail loudly by
  design — never silently skip or pass. See docs/RELEASE.md §3.
- `tests/e2e` failing on Windows with a terminal: read the first
  failure — the session fixture fails fast if `driver.connect()`
  fails, and the error names the cause (`MT5_ACCOUNT_NOT_READY`,
  account mismatch, initialize failure).
