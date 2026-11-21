# NSE MT5 Bridge

**Live platform: [https://subscribe.nexusverse.space](https://subscribe.nexusverse.space)** — the NSE Platform server this Bridge connects your MetaTrader 5 terminal to. Sign in with GitHub to get the free models, analyse an MT5 History report in Trading Lab, or analyse a chart screenshot in AI Chart Lab.

**Secure, reliable, persistent communication infrastructure between the
user's MetaTrader 5 terminal and the NSE Platform server.** The Bridge is
the local communication gateway and execution agent; the server owns all
intelligence, orchestration, and decisions. This is infrastructure, not a
trading product: there is no local AI, no autonomous trading, no backtest,
and no indicator/risk engine in the client.

- **Local capability:** 91 commands over a loopback REST API (see
  [docs/COMMAND_REFERENCE.md](docs/COMMAND_REFERENCE.md)).
- **Server channel:** exactly 3 operations in CONTRACT v1
  (`close_position`, `modify_sl_tp`, `modify_pending` — the last
  capability-gated) — see [docs/API_PROTOCOL.md](docs/API_PROTOCOL.md).
  The full 91-command registry stays available **locally**; the server
  channel enforces the 3-op allowlist at the boundary. Never the reverse.
- **Packaged as a single-file Windows `.exe`** (x64), built by CI.

> Contract source of truth: `NSE-CONNECTOR-CONTRACT.md` (CONTRACT v1,
> NSE-Platform, merged via PR #102). Where this repo and the contract
> disagree, the contract wins. Platform-side gaps are tracked in
> [docs/PLATFORM-GAPS.md](docs/PLATFORM-GAPS.md).

```
┌──────────────────────────── Client PC ────────────────────────────┐
│  MetaTrader 5 terminal64.exe                                      │
│      ▲ official MetaTrader5 C++ IPC driver (serialized worker)    │
│  NSE-MT5-Bridge.exe (single file, console=False)                  │
│   ├─ FastAPI 127.0.0.1:8788 — POST /api/v1/{command} + /docs      │
│   ├─ /ui               — Connector Control Center (6 screens)     │
│   ├─ /ws/ui            — live GUI feed (ticks, account)           │
│   ├─ outbound WSS      — NSE server channel (no inbound ports)    │
│   └─ logs/bridge.log + bridge.jsonl (structured, redacted)        │
└───────────────────────────────────────────────────────────────────┘
```

- **All broker calls run on a dedicated worker thread** (the MT5 IPC
  channel is process-global and not thread-safe) — the async API never
  blocks the event loop.
- **Execution safety (kept):** official retcode map (10004–10031),
  `retcode 0` = IPC failure → `TRADE_UNVERIFIED` (never blind-retry),
  ambiguous-fill recovery + MT5-state reconciliation, close idempotency,
  filling-mode resolution from the broker bitmask, stops/freeze-level
  pre-validation. A lost execution response ends in `UNKNOWN` first and
  is reconciled against live MT5 state before any retry.
- **Security:** loopback-only bind by default, **mandatory** `X-API-Key`
  gate (auto-generated on first run, stored in the OS secret store),
  secrets in Windows Credential Manager / DPAPI — never plaintext
  `config.json`, never in logs; outbound-only authenticated WSS to the
  Platform (Bearer API key + `X-NSE-Device-Id` headers, no `hello`
  frame); TLS mandatory (`wss://`).

## Control Center GUI (6 screens)

**Connection** (Platform auth, device registration, WS status, last
heartbeat, sync status, reconnect) · **MetaTrader** (detected terminals,
path, version, running status, selected terminal, IPC status, account
status; discover/select/open/connect/disconnect/retry) · **Account &
Subscription** (READ-ONLY, server-driven) · **Diagnostics** (bridge/MT5/
network/WS health, last errors, report, copy) · **Updates & Releases**
(installed vs latest from `releases/latest`, notes, progress, status,
restart requirement) · **Settings** (credentials entry, server URL, log
level, GUI mode — secrets masked, stored via Credential Manager).

No trading dashboard, no positions/orders screens, no AI screens, no
Backtest screen. Guided MT5 diagnostics use structured codes
(`MT5_NOT_FOUND`, `MT5_NOT_RUNNING`, `MT5_IPC_FAILED`,
`MT5_ACCOUNT_NOT_READY`, `MT5_UNSUPPORTED_VERSION`) with retry +
copy-diagnostics — no raw stack traces for ordinary users.

## Quick start (development)

```bash
py -3.13 -m venv .venv          # Python 3.12/3.13
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m nse_bridge            # bridge + GUI on http://127.0.0.1:8788
.venv/Scripts/python scripts/mock_server.py   # CONTRACT-v1 mock NSE server (WS :9099, REST :9098)
.venv/Scripts/python scripts/export_openapi.py docs/openapi.json   # re-export schema
```

- Control Center: http://127.0.0.1:8788/ui
- Swagger: http://127.0.0.1:8788/docs
- OpenAPI schema: http://127.0.0.1:8788/openapi.json
  (also committed as `docs/openapi.json`, re-exported from the registry)

## Single-file .exe

```bash
.venv/Scripts/python -m PyInstaller bridge.spec --noconfirm --clean
# → dist/NSE-MT5-Bridge.exe  (CI also builds it on every push — see Actions)
dist\NSE-MT5-Bridge.exe                  # desktop application (default experience)
dist\NSE-MT5-Bridge.exe --console        # foreground console service (no GUI)
dist\NSE-MT5-Bridge.exe --gui none       # headless service mode
dist\NSE-MT5-Bridge.exe --diagnostic     # diagnostic console mode (DEBUG telemetry + logs)

# Machine-readable operations against the RUNNING bridge (loopback API,
# never a second instance):
dist\NSE-MT5-Bridge.exe status --json
dist\NSE-MT5-Bridge.exe health --json
dist\NSE-MT5-Bridge.exe diagnostics --json
dist\NSE-MT5-Bridge.exe mt5 list --json
```

Full CLI contract (envelope, exit codes): [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md).

Releases are cut from tags only (`v1.2.3` stable, `v1.2.3-rc.1`
prerelease) and ship the installer + exe + `.sha256` + `release-manifest.json`.
Direct install from GitHub Releases (SHA-256 verified, idempotent):

```powershell
$script = "$env:TEMP\nse-bridge-install.ps1"
Invoke-WebRequest "https://raw.githubusercontent.com/Opselon/NSE-Connector-Bridge/main/scripts/install.ps1" -OutFile $script
& $script -Channel Stable
```

See [docs/INSTALLATION.md](docs/INSTALLATION.md), [docs/BUILD.md](docs/BUILD.md) and [docs/RELEASE.md](docs/RELEASE.md).

## Configuration

`%LOCALAPPDATA%\NSEBridge\config.json` (dev: `~/.nse-bridge/config.json`),
env overrides with `NSE_BRIDGE_` prefix, or live via the GUI Settings
screen / `PUT /api/v1/settings`. Key fields:

| Field | Default | Meaning |
|---|---|---|
| `port` | 8788 | local API port |
| `server_token` | (secret store) | **API key issued on the NSE Platform site** (Bearer) — never in config.json |
| `local_api_key` | (secret store) | mandatory `X-API-Key` for /api/v1, auto-generated on first run |
| `server_base_url` | `https://bridge.nexusverse.space` | REST base; WS is derived (`wss://…/api/v1/connector/ws`) |
| `server_url` | empty | explicit WS URL override (empty = derived from base; must be `wss://`) |
| `device_id` | auto | persistent bridge identity |
| `magic` | none | NO default magic — stamped only when supplied per command or set explicitly |
| `bot_symbol` | (empty) | NO default symbol — callers pass it per command |
| `strict_magic_guard` | false | refuse to touch positions/orders with a foreign magic |
| `diagnostic_mode` | false | DEBUG telemetry events (`NSE_BRIDGE_DIAGNOSTIC_MODE=1`, or `--diagnostic`) |
| `update_public_key` | (none) | base64 Ed25519 release-signing public key — **not yet provisioned**; without it the updater can check but never install |
| `broker_utc_offset_minutes` | 0 | broker server UTC offset for history windows |

There is deliberately **no `mt5_password`** field: the user signs in
through MT5 itself; the Bridge never asks for or stores the password.
A terminal with no logged-in account surfaces `MT5_ACCOUNT_NOT_READY`
with guidance instead.

## Command surface

Local capability (91 commands, `POST /api/v1/{name}`) vs server channel
(3 ops, CONTRACT v1 §6.1) — listed separately, never mixed, in
[docs/COMMAND_REFERENCE.md](docs/COMMAND_REFERENCE.md). Every local
response: `{"ok": true, "data": …}` or
`{"ok": false, "error": {code, message, retcode?, details?}}`. Broker
rejections carry the official MT5 retcode (10004–10031) + label.

Bridge-management endpoints (same `/api/v1` prefix, same `X-API-Key`
auth): `GET/PUT /settings`, `POST /run_connection_checks`,
`POST /server_channel/restart`, `GET /account/subscription` (read-only),
`POST /account/validate-key`, `GET /updates/latest|status`,
`POST /updates/check|download`, `GET /mt5/terminals`,
`POST /mt5/terminals/open`, `GET /diagnostics/summary`,
`GET /health` (no auth — plain liveness probe).

## Server channel protocol (bridge ⇄ NSE Platform, CONTRACT v1 §5/§6)

Outbound `wss://bridge.nexusverse.space/api/v1/connector/ws`, auth in the
upgrade headers (`Authorization: Bearer <api_key>` +
`X-NSE-Device-Id`). **No `hello` frame.** Client heartbeat every 15 s;
session dead after 45 s. Server → client: `command` (§6.2 envelope),
`sync_request`, `ack`, `heartbeat`, `chart.*`. Client → server: only
`heartbeat`, `action.result` (§6.5), `chart.data`, `chart.tick`.
Snapshots go over REST `POST /api/v1/connector/sync/batch` (§3.3) —
the ONLY sync channel. Reconnect: fresh session, 500 ms doubling
backoff → 30 s + jitter; the server re-sends only `pending` commands.

`scripts/mock_server.py` speaks CONTRACT v1 and is the integration-test
server. Full protocol reference: [docs/API_PROTOCOL.md](docs/API_PROTOCOL.md).

## Tests

Every test is labelled `unit` / `mock_integration` / `real_mt5` /
`manual` (enforced by `tests/unit/test_test_honesty.py`).

```bash
python -m pytest tests/unit tests/integration -q   # Linux-runnable, no MT5 (CI gate)
python -m pytest tests/e2e --collect-only -q       # 104 tests collect on Linux; they RUN only on Windows with a live MT5 terminal
```

A required-MT5 test **fails loudly** when no terminal is available —
never silently skips or passes (`tests/conftest.py`). The e2e suites
execute real orders on the connected account (0.01 lots, Demo only,
auto-cleanup) and require Algo Trading ON in the terminal. They are
manual, Windows-with-MT5-only, and are never run in CI (GitHub-hosted
runners have no MT5 terminal). See [docs/RELEASE.md](docs/RELEASE.md)
for the honest verification status.

## Documentation

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data flow, state machines |
| [docs/API_PROTOCOL.md](docs/API_PROTOCOL.md) | REST + WS contract (§3/§5/§6/§17.1), frames, retry policy |
| [docs/COMMAND_REFERENCE.md](docs/COMMAND_REFERENCE.md) | all 91 local commands + the 3 server ops, separately |
| [docs/SECURITY.md](docs/SECURITY.md) | secret store, auth, redaction, threat model |
| [docs/BUILD.md](docs/BUILD.md) | local build, PyInstaller spec, versioning |
| [docs/RELEASE.md](docs/RELEASE.md) | release process, verification status, known gaps |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | diagnostics codes, common failures |
| [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) | Windows/x64, Python, MT5 builds, ARM64 |
| [docs/PLATFORM-GAPS.md](docs/PLATFORM-GAPS.md) | what must change on the Platform side |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | verdict per acceptance criterion (§15) |
| [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) | serve modes, CLI JSON envelope, exit codes |
| [docs/DESKTOP_APPLICATION.md](docs/DESKTOP_APPLICATION.md) | Control Center architecture, screens, onboarding |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | installer, direct GitHub install, portable exe |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | measured baselines, methodology, before/after |
| [docs/openapi.json](docs/openapi.json) | OpenAPI schema, re-exported from the registry |

## Known environment notes

- **The hidden AutoTrading killer (MT5 build 6306+)**: *Tools → Options → Experts →*
  ***"Disable algorithmic trading via external Python API"*** *— when this is CHECKED the*
  *toolbar button looks green but every Python-API order is rejected with retcode 10027*
  *"AutoTrading disabled by client".* Uncheck it. The GUI's diagnostics
  surface this failure with the fix. (Found by fixing `Api=1` → `Api=0`
  in `common.ini [Experts]` + terminal restart.)
- **Broker server time offset is auto-detected at connect** (from the freshest live
  tick epoch vs local UTC): history windows (`history_deals_get`, `copy_ticks_range`,
  `copy_rates_range`) and all display timestamps are shifted into the broker timebase
  automatically — set `broker_utc_offset_minutes` manually only if detection is
  impossible (all probe symbols closed).
- **Python 3.14 on dev machines with broken numpy/pip**: use a clean 3.12/3.13.
- Some brokers report unreliable `trade_tick_value` — the bridge computes value-
  per-unit via broker-native `order_calc_profit` instead.
