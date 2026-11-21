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

### Terminal data directories (`data_dir` in `GET /api/v1/mt5/terminals`)

Each discovered terminal reports a `data_dir` when it can be determined, `null`
otherwise — a path is never guessed. Portable installs resolve to the folder
next to the executable once it carries live data markers (`MQL5` + `Logs`);
standard installs live under `%APPDATA%\MetaQuotes\Terminal\<hash>`, and that
folder is attributed to a terminal only when exactly one terminal and exactly
one data directory exist on the machine. A `null` data directory means "not
determinable from this machine", not "no data directory exists". Changing the
terminal path or custom terminal directories in Settings re-runs discovery
immediately (the fresh list is in the `PUT /settings` response); changing the
terminal path while connected disconnects the driver from the old terminal —
reconnect to attach to the new one.

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
| `rate_limited` (429) / 5xx | Server busy/down | Wait — the client backs off (max 3 retries, 0.5 s doubling with ±15% jitter, honours `Retry-After`). If it persists, the Platform is down; see self-healing scenario `platform_down`. |
| Enrolment (`validate-key`) hangs or fails with a transport error while the server itself is up | **Possible server-side vhost outage.** Verified 2026-11-21: `subscribe.nexusverse.space` answered 200 while `bridge.nexusverse.space` hung — the two hostnames are served independently, so a bridge-vhost outage can look like a dead key or a local network problem | First check the vhost directly (e.g. `curl -m 10 https://bridge.nexusverse.space/` vs another NSE host). If the bridge vhost hangs while others answer, it is a Platform-side outage — report it; the Bridge's own backoff and self-healing (`platform_down`) keep retrying meanwhile. |
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
| `GET /health` works but everything else 401s | Normal: `/health` is the unauthenticated liveness probe; all API routes (and the `/ws/ui` upgrade — header or loopback cookie) require the key. |
| `/health` says `broker_connected: null` where it used to give true/false | Expected since the light-probe change: `/health` is a light probe by default (cached driver state only, `probe: "light"`) — an MT5 IPC round-trip in a liveness probe made the endpoint slow and could 500. The full IPC probe is opt-in: `/health?probe=full`. `broker_connected: null` means honestly unknown, not disconnected. |
| Settings changes do not apply | `PUT /settings` applies live; a bad value is skipped, not fatal (check the response for what was applied). Secrets are never accepted in plaintext via config.json — they are migrated to the store. |

## 4a. Launching and querying the Bridge (single instance + CLI)

| Symptom | Meaning / fix |
|---|---|
| A new Bridge launch exits immediately with code **2** and prints guidance about an instance already running | Expected — single-instance. The process holds a Windows named mutex (`Local\NSE-MT5-Bridge-SingleInstance`, `nse_bridge/single_instance.py:25`) acquired BEFORE MT5 attach and port bind, so the second copy can never race a live one for the port or the terminal. To inspect the running instance instead of launching a duplicate: `NSE-MT5-Bridge.exe status --json` (or `health --json`, `diagnostics --json`, `mt5 list --json`). Close the running instance first if you really want a second one (e.g. a different `--host/--port` — the mutex is machine-wide regardless of port). |
| `status --json` exits with code **3** and a structured `BRIDGE_NOT_RUNNING` envelope (`{"ok": false, "error": {"code": "BRIDGE_NOT_RUNNING", ...}}`) | The CLI queried the loopback API and found no live Bridge (`nse_bridge/cli.py:90`). Nothing is wrong with the CLI — start the Bridge, then re-run. Exit-code map (`cli.py:36–41`): 0 ok · 1 usage · 2 another instance already running · 3 bridge not running · 4 operation failed / auth refused · 5 server error. |
| Query subcommands "boot a second bridge" | They never do: `main.py` dispatches `status|health|diagnostics|mt5 list` BEFORE any serve setup (`main.py:145–149`), so a query cannot acquire the mutex, touch MT5, or bind the port. If you see a boot from a query, you are running an outdated build. |

## 5. Updates

| Symptom | Meaning |
|---|---|
| Update check says "no public key configured" / download refuses `NO_PUBLIC_KEY` | Expected today: the NSE release-signing public key is not provisioned. Set `NSE_BRIDGE_UPDATE_PUBLIC_KEY` (base64 raw 32-byte Ed25519 public key) once the operator publishes it. The updater refuses to install an unverifiable binary — do not work around this. |
| `SIGNATURE_MISSING` / `SHA256_MISSING` / `SHA256_MISMATCH` / `SIGNATURE_INVALID` | The release metadata or bytes are not authentic. Nothing is staged. Report it; do not install manually. |
| `MIN_VERSION_NOT_SATISFIED` | The release needs a newer connector than the one installed. Update the connector first (or wait for a compatible release). |
| `not_frozen_no_target` on install | A verified update is staged, but this is not a packaged exe run (Linux/dev). Restart install only applies to the frozen Windows exe. |
| Update download fails (`DOWNLOAD_FAILED`, `NO_DOWNLOAD_URL`) | Nothing was staged, so a retry is safe: run the update check again. If it keeps failing, check the network / server reachability (see §3) — the download is idempotent and a failed attempt never leaves a half-installed update behind. |

## 8. Self-healing scenarios (Diagnostics screen)

The Diagnostics screen lists the last handling outcome of each
self-healing scenario (`GET /api/v1/diagnostics/summary`). `ok` means
the fault was detected and handled correctly — not that nothing
happened. `degraded` / `failed` mean the Bridge stopped rather than
guessed or faked a recovery.

| Scenario | Meaning | Operator action |
|---|---|---|
| `account_disconnect` | Terminal is up but no account is logged in (or the account dropped) | Sign in through the MT5 terminal itself (see `MT5_ACCOUNT_NOT_READY` above). No execution happens while the account is down. |
| `broker_connection_change` | The terminal re-connected to a different broker server | Check the account/broker on the MetaTrader screen. Nothing executes until a fresh sync confirms the new account; symbol mappings are refreshed, never carried over. If the broker changed unexpectedly, check the terminal's server setting. |
| `symbol_unavailable` | A symbol a command names is not currently tradable/visible on this broker | The command was rejected, not re-routed to a similarly named symbol. Check the symbol on the Symbols screen (exact broker name, availability); if the broker renamed it, the server must send the exact new name. |
| `diagnostic_queue_full` | The bounded diagnostic queue (128 events) hit its limit | Oldest events were evicted with a warning (counted, never silent). No action unless it recurs constantly — that means ERROR/WARN events are being produced faster than they can be delivered, and the underlying fault (not the queue) needs fixing. Note: with no diagnostics endpoint yet (D1), queued events age out by TTL; the queue is now durable across restarts (`pending_diagnostics.jsonl` in the data dir), so a restart no longer loses them. |
| `update_failure` (download) | Update download failed | Retry the update check — safe, nothing staged. |
| `update_failure` (verification) | Downloaded bytes failed SHA-256 / Ed25519 verification, or no signing key is configured | Nothing staged, nothing installed. Do **not** retry the same download or install manually. Report the code (`SHA256_MISMATCH`, `SIGNATURE_INVALID`, `NO_PUBLIC_KEY`, …); a new release — or the published signing key — is required. |

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
