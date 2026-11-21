# Desktop Application

The Bridge's default experience is a native Windows desktop
application: `NSE-MT5-Bridge.exe` opens the **NSE Connector Control
Center** — a professional multi-screen UI served by the Bridge's local
API and displayed in the operating system's native web view.

## Technology decision (audited, not fashionable)

Mission §C1 required an explicit evaluation of the frontend stack.
The result, with reasons:

| Option | Verdict | Why |
|---|---|---|
| **FastAPI + PyWebView + bundled vanilla SPA (kept)** | **chosen** | zero build pipeline (no Node on dev machines or CI for the app itself), tiny bundle cost, the whole UI is one static dir PyInstaller already ships, offline-safe (no CDN fonts/frameworks), and the API-first architecture means the UI is a thin client over the same local REST/WS API the CLI and tests use |
| TypeScript/React + Vite build | rejected for now | would add a node toolchain to CI and ~2 orders of magnitude more frontend dependency surface for a UI whose complexity is dominated by API state, not DOM state; revisitable if the UI grows a component ecosystem |
| Electron / Tauri / Qt | rejected | heavyweight runtime (Electron) or new native toolchain (Tauri/Qt) contradicts the single-file distribution requirement; PyWebView already gives the native window + real browser engine |

The user-visible consequence: the app must feel like a commercial
desktop client (design tokens, themes, keyboard-focusable controls,
skeleton loading states) without shipping a framework runtime.

## Architecture

```
NSE-MT5-Bridge.exe (console=False windowed exe, branded icon)
   ├─ uvicorn 127.0.0.1:<port>      local API (X-API-Key mandatory)
   │    ├─ /ui                      Control Center (bundled static SPA)
   │    ├─ /api/v1/*                91 commands + management routes
   │    └─ /ws/ui                   authenticated live feed (push)
   ├─ pywebview window              native shell → /ui
   ├─ ServerChannel (outbound WSS)  Platform commands/sync (§5/§6)
   └─ MT5 serialized worker         all broker IPC on one thread
```

Live updates are **push-first**: the Control Center opens `/ws/ui`
(authenticated with the same HttpOnly cookie as the REST calls) and
receives `status_update` / `account_update` / `positions_update` /
`tick` / `trade_event` frames as they happen. A slow 30 s REST refresh
runs only while the socket is down, and the UI says so explicitly
("live feed disconnected — polling"). No screen polls the heavy
diagnostics endpoint every few seconds anymore.

## Screens

| Screen | Purpose | Data source |
|---|---|---|
| Overview | unified status: bridge, MT5, Platform WS, sync, update badge | `/ws/ui` push + initial REST |
| Platform | endpoint/API key config (masked), auth + device + session state, WS health, reconnect, pre-flight checks | settings + `run_connection_checks` + `server_channel/restart` |
| MetaTrader | terminal discovery/selection, process + broker + IPC status, Algo Trading availability, structured diagnostics with recovery steps | `mt5/terminals`, `diagnostics/summary` |
| Account & Subscription | read-only server-driven entitlements vs API key | `account/subscription` |
| Symbols | broker symbol inventory + canonical resolution | `symbols/*` |
| Execution | recent command lifecycle + journal feed | `execution/recent`, `journal/events` |
| Diagnostics | component health grid, recent errors, telemetry/self-healing summaries, export/copy report | `diagnostics/summary` |
| Updates | installed vs latest, check/download, integrity states, restart banner | `updates/*` |
| Settings | platform credentials (masked), log level, GUI mode, trading identity, terminal paths, diagnostic mode | `settings` |

Design qualities implemented: dark AND light themes (persisted,
respects `prefers-color-scheme`), consistent spacing/typography
tokens, inline SVG icons (no CDN), loading/empty/error states,
restrained transitions, accessible controls. Secrets are always
masked; error messages arrive pre-redacted from the API.

## Onboarding

First run (not enrolled / no key): a guided flow walks through
initialization → MT5 discovery → platform endpoint → API key entry →
key validation → device registration → connection → initial sync →
health verification. Every step shows its REAL result — buttons stay
disabled until prerequisites pass, failures are actionable, nothing
is simulated. The flow is skippable for standalone (no Platform) use.

## Lifecycle

- The window IS the app: closing it ends the main thread, which
  drains uvicorn (graceful lifespan teardown: channel stop, enrolment
  cancel, sync flush stop, REST client close), disconnects the MT5
  driver, and exits — no orphaned processes.
- Only one instance per Windows session (named mutex, decided before
  any MT5 attach or port bind; a second launch exits 2 with guidance).
- Browser fallback (`--gui auto` when webview is unavailable, or
  `--gui browser`) keeps serving until killed — the Bridge is a
  service even when the window is gone.
- Crashed instance leaves no locks behind: the mutex is an OS handle
  released on process exit.

## Verification

- `tests/unit/test_gui_control_center.py` + `tests/unit/test_gui_data.py`
  pin the UI structure's meaningful invariants (screens present, WS
  usage, theme toggle, masked secrets, no password field, onboarding
  present, honesty of empty states).
- CI's Windows job boots the frozen exe headless and asserts: `/ui`
  answers 200 (asset integrity), `/health` ready, CLI JSON contract
  works against the running instance, second launch refuses.
- Real-window E2E (pywebview interaction) requires a Windows machine
  with a display and is executed manually per docs/RELEASE.md — the
  CI runner covers the API/asset/CLI surface of the same binary.
