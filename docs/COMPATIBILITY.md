# Compatibility

## 1. Operating system

| Platform | Status |
|---|---|
| Windows 10 / 11 x64 | **Primary release target — the only trading build.** The packaged exe (`NSE-MT5-Bridge.exe`) + Inno Setup installer are built on `windows-latest` in CI. |
| Windows 10 / 11 x64, other builds (8.1+) | Declared in the exe manifest (`supportedOS` 8.1/10), but **untested** — Windows 10/11 is what CI builds on. |
| Windows 11 ARM64 | **Core-only native build** (portable exe, no installer). The bridge runs, but the MetaTrader5 driver has no ARM64 build, so terminal attach reports `MT5_WINDOWS_REQUIRED`. **To trade on ARM64 Windows, run the x64 build** — it runs under emulation and CAN attach to the terminal. |
| macOS 13+ x64 (Intel) / arm64 (Apple Silicon) | **Core-only.** `.app` in a `.dmg`, built on `macos-15-intel` / `macos-latest` in CI. Dashboard, platform channel, settings, diagnostics run; MT5 attach reports `MT5_WINDOWS_REQUIRED`. |
| Linux x64 / arm64 (glibc) | **Core-only.** Tarball, built on `ubuntu-latest` / `ubuntu-24.04-arm` in CI. Same scope as macOS. Unit and mock-integration tests, the mock server, and the OpenAPI export also run on Linux. |
| Linux / macOS (development) | The Bridge runs from source for development, but MT5 (and therefore all trading and e2e) is Windows-only. |

No remote-terminal mode exists on any platform: a non-Windows build
never pretends an MT5 terminal is attached.

## 2. CPU architecture

- **x64 (amd64): primary.** The release exe is `windows-x64`
  (see `release-manifest.json`, `platform: "windows-x64"`); macOS
  x64 and Linux x64 ship as `macos-x64` / `linux-x64`.
- **ARM64: core-only, never trading.** The bridge core (server, UI,
  platform channel) builds and runs natively on Windows ARM64
  (`windows-arm64`), macOS arm64 (`macos-arm64`), and Linux arm64
  (`linux-arm64`) — all proven in CI on every PR. The blocker is
  upstream, not the bridge: the official MetaTrader5 package ships
  `win_amd64` wheels only (no `win_arm64` wheel, no macOS/Linux
  wheel), and the MT5 terminal itself is x64. A native ARM64 process
  cannot load the IPC driver, so terminal attach reports
  `MT5_WINDOWS_REQUIRED` on all three ARM64 targets. On ARM64
  Windows the supported trading path is the x64 build under
  emulation. Proving native-ARM64 trading (terminal + wheel + frozen
  exe + e2e on a real ARM64 Windows machine with MT5) is a separate,
  explicitly scoped work item; it is not part of this release.

## 3. Python

- **3.12 / 3.13 supported.** CI uses 3.13. `pyproject.toml`
  requires `>=3.12`.
- **3.14 untested** — dev machines with broken numpy/pip on 3.14
  should use a clean 3.12/3.13.
- Pinned dependencies (`requirements.txt` / `pyproject.toml`):
  fastapi 0.163.2, uvicorn[standard] 0.54.0, pydantic 2.14.3,
  pydantic-settings 2.16.3, websockets 17.4, httpx 0.28.1,
  cryptography 50.0.2, loguru 0.8.0, numpy 2.5.5, pywebview 6.2.1,
  keyring 25.7.0 (win32 only), MetaTrader5 5.0.6231 (win32 only),
  PyInstaller 6.22.3. Dev adds pytest 9.2.0, pytest-asyncio 1.5.0,
  ruff 0.17.5.

## 4. MetaTrader 5

- **Terminal:** any recent MT5 build; the driver detects the
  version and reports it in diagnostics. Known floor: builds
  before 6306 predate the "Disable algorithmic trading via external
  Python API" option — on 6306+ that option must be UNCHECKED or
  every Python-API order is rejected with retcode 10027 (see
  docs/TROUBLESHOOTING.md §2).
- **Discovery:** env `NSE_MT5_TERMINAL_PATH` / `MT5_PATH` →
  standard install locations (`C:\Program Files\MetaTrader 5\…`,
  `C:\Program Files (x86)\MetaTrader 5\…`) → Windows App Paths
  registry → custom/portable directories
  (`mt5_custom_terminal_dirs`) → running processes. Multi-terminal
  selection is user-driven (Settings → MetaTrader); the stored path
  is never overwritten automatically.
- **Account:** Demo-only in v1 (contract §7). The Bridge binds to
  exactly ONE active MT5 account; an account switch invalidates
  pending old-account commands and triggers a fresh sync.
- **Algo Trading must be ON** in the terminal for any order
  dispatch (including the e2e trade suites).
- **The Bridge never asks for the MT5 password.** The user signs in
  through MT5 itself; a terminal with no logged-in account reports
  `MT5_ACCOUNT_NOT_READY`.

## 5. NSE Platform / contract

- **CONTRACT v1** (`NSE-CONNECTOR-CONTRACT.md`, merged to Platform
  main via PR #102). The Bridge targets the contract document, not
  any PR branch.
- **Production endpoint:** `https://bridge.nexusverse.space`,
  `wss://bridge.nexusverse.space/api/v1/connector/ws`. Live
  verification is still owed (Platform PR #107 unmerged;
  docs/PLATFORM-GAPS.md §1, §6).
- **Schema version:** `protocol_version "1.0"`, `schema_version
  "2026-11-17"` in `action.result` envelopes. Future ops arrive
  through contract versions + `schema_version` bumps — never by
  local improvisation.
- **Server allowlist (v1):** exactly `close_position`,
  `modify_sl_tp`, `modify_pending` (capability-gated). `open_order`
  / `cancel_order` are NOT sendable in v1.

## 6. What was tested where

| Suite | Linux (this VM) | Windows CI | Windows + MT5 (manual) |
|---|---|---|---|
| `tests/unit` (302 pre-Phase-6 + 9 Phase-6) | ✅ pass | ✅ (job `build-windows`) | — |
| `tests/integration` (mock server) | ✅ pass | ✅ (job `build-windows`) | — |
| `tests/e2e` (104) | ❌ fail loudly by design (no MT5) | ❌ never run (no terminal on runners) | ⏳ manual, not yet executed |
| Frozen exe build | ❌ cannot build (Windows-only deps) | ✅ (job `build-windows`) | — |
| Frozen builds, macOS/Linux/ARM64 | ❌ cannot build on this VM | ✅ (jobs `build-unix` + `build-windows-arm`, with headless frozen-binary smoke) | — |
| Live Platform round-trip | ❌ unverified | ❌ unverified | ⏳ blocked on Platform deploy |

See docs/RELEASE.md §3 for the full honest verification status.
