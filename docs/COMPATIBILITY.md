# Compatibility

## 1. Operating system

| Platform | Status |
|---|---|
| Windows 10 / 11 x64 | **Primary and only supported release target.** The packaged exe (`NSE-MT5-Bridge.exe`) is built on `windows-latest` in CI. |
| Windows x64, other builds (8.1+) | Declared in the exe manifest (`supportedOS` 8.1/10), but **untested** — Windows 10/11 is what CI builds on. |
| Linux / macOS | **Development + CI only.** Unit and mock-integration tests, the mock server, and the OpenAPI export all run on Linux. The Bridge itself can run from source on Linux for development, but MT5 (and therefore all trading, e2e, and the packaged exe) is Windows-only. |
| Windows ARM64 | **Unsupported — no build is published.** See §4. |

## 2. CPU architecture

- **x64 (amd64): primary.** The release exe is `windows-x64`
  (see `release-manifest.json`, `platform: "windows-x64"`).
- **ARM64: unsupported.** The full stack has not been proven on
  ARM64: the MetaTrader5 wheel, the MT5 terminal itself, and the
  PyInstaller/pywebview native backends are all x64-first, and none
  of it can be proven on this VM or on GitHub-hosted runners (which
  are x64). **No ARM64 build is published, and no ARM64 artifact
  will be attached to a release.** If an ARM64 machine runs the
  installer, the correct outcome is a clear "unsupported platform"
  message — not a broken binary. Proving ARM64 (terminal + wheel +
  frozen exe + e2e on a real ARM64 Windows machine with MT5) is a
  separate, explicitly scoped work item; it is not part of this
  release.

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
| Live Platform round-trip | ❌ unverified | ❌ unverified | ⏳ blocked on Platform deploy |

See docs/RELEASE.md §3 for the full honest verification status.
