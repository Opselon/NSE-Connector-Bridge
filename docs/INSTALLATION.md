# Installation

The Bridge ships as self-contained builds — no Python, no Node.js, no
package manager. Since v1.3.0 there is one artifact per target (each
with a `.sha256` sidecar; all listed in `release-manifest.json`):

| Target | File (from the [latest release](https://github.com/Opselon/NSE-Connector-Bridge/releases/latest)) | MT5 trading |
|---|---|---|
| Windows 10/11 x64 | `NSE-MT5-Bridge-Setup-<version>.exe` (installer) or `NSE-MT5-Bridge.exe` (portable) | **Yes — the trading build** |
| Windows 11 ARM64 | `NSE-MT5-Bridge-<version>-windows-arm64.exe` (portable, core-only) | No — use the x64 build under emulation (§4) |
| macOS 13+ x64 (Intel) | `NSE-MT5-Bridge-<version>-macos-x64.dmg` | No |
| macOS 13+ arm64 (Apple Silicon) | `NSE-MT5-Bridge-<version>-macos-arm64.dmg` | No |
| Linux x64 (glibc) | `NSE-MT5-Bridge-<version>-linux-x64.tar.gz` | No |
| Linux arm64 (glibc) | `NSE-MT5-Bridge-<version>-linux-arm64.tar.gz` | No |

**Why the non-Windows builds cannot trade:** the official MetaTrader5
IPC driver ships Windows-x64 wheels only — there is no macOS, Linux,
or ARM64 build of it, and no terminal to attach to on those machines.
On macOS/Linux/ARM64 the bridge core runs (dashboard, platform
channel, settings, diagnostics) and the MetaTrader screen reports
`MT5_WINDOWS_REQUIRED` — "Windows MT5 terminal required" — instead of
pretending a terminal exists. There is no remote-terminal mode. Verify
every download against its `.sha256` sidecar (and the manifest entry)
before running it.

Builds are unsigned: Windows SmartScreen and macOS Gatekeeper ask on
first run (on macOS, right-click the app → Open the first time). No
signing/notarization key is provisioned yet.

## 1. Windows x64 — desktop installer (recommended)

Download `NSE-MT5-Bridge-Setup-<version>.exe` from the
[latest release](https://github.com/Opselon/NSE-Connector-Bridge/releases/latest)
and run it.

What it does:

- per-user install into `%LOCALAPPDATA%\Programs\NSE MT5 Bridge` —
  **no administrator rights, no UAC prompt**;
- Start Menu shortcut; optional desktop shortcut;
- in-place upgrades keep your configuration and credentials (they
  live in `%LOCALAPPDATA%\NSEBridge`, outside the install dir —
  config.json, the OS secret store, checkpoints and logs are never
  touched by install/upgrade/uninstall);
- refuses a second instance while the Bridge is running (asks to
  close first);
- clean uninstaller leaves user data in place.

Upgrade-in-place is guaranteed by the installer's AppId
(`b80bf455-6a18-41e5-bfa2-03a04736ee8f`) — never changed since the
first release.

## 2. Windows x64 — direct install from GitHub (PowerShell)

```powershell
$script = "$env:TEMP\nse-bridge-install.ps1"
Invoke-WebRequest `
  "https://raw.githubusercontent.com/Opselon/NSE-Connector-Bridge/main/scripts/install.ps1" `
  -OutFile $script
& $script -Channel Stable
```

Options:

| Option | Meaning |
|---|---|
| `-Channel Stable\|Prerelease` | release channel (default Stable = latest non-prerelease) |
| `-Version 1.2.0` | pin a specific release |
| `-InstallDir <path>` | override the target directory |
| `-Portable` | install the standalone exe (no Start Menu entry / uninstaller) |
| `-Silent` | run the installer without the wizard UI |
| `-Force` | reinstall even when the version is already installed |
| `-VerifyOnly` | download + verify everything, install nothing |

The script resolves the official GitHub release, downloads the
artifact plus `release-manifest.json` and the `.sha256` sidecar, and
verifies **all three against each other and the file on disk** — any
disagreement is a hard failure (exit 5). After installation it
re-verifies the installed exe's hash and reports the result.

Exit codes: `0` success · `1` usage · `2` unsupported OS/arch ·
`3` GitHub unreachable · `4` artifact missing · `5` checksum
mismatch · `6` installer failed · `7` post-install verification
failed. The script is idempotent — safe to rerun.

**Signature honesty:** CONTRACT v1 reserves Ed25519 release
signatures. Signed releases (P1-6) carry a base64 Ed25519 signature
per file in the manifest (`files[].signature`) plus a `<name>.sig`
sidecar, and `releases/latest` serves the setup installer's signature
once the Platform D19 change lands; `install.ps1` reports a present
signature and points at `scripts/sign_release_artifact.py --verify`
(it cannot verify Ed25519 natively). An unsigned release still
answers `signature: null`, and verification relies on the SHA-256
manifest + sidecar over HTTPS — `install.ps1` (and the Bridge's own
updater) say so explicitly rather than pretending a signature check
ran. No key is fabricated and no verification is bypassed.

## 3. Windows x64 — portable executable

Download `NSE-MT5-Bridge.exe` from the release, place it anywhere,
run it. Everything the app needs is inside the file (Python runtime,
dependencies, GUI assets, icon). Configuration and credentials still
live under `%LOCALAPPDATA%\NSEBridge`.

## 4. Windows ARM64

Two options, and they are not equivalent:

- **To trade: use the Windows x64 build (§1–§3).** Windows 11 on
  ARM64 runs x64 apps under emulation, and the x64 bridge (with its
  x64 IPC driver) attaches to the x64 MT5 terminal normally.
- **Native ARM64 build (core-only):** download
  `NSE-MT5-Bridge-<version>-windows-arm64.exe`, place it anywhere,
  run it. The dashboard, platform channel, and diagnostics work, but
  the MetaTrader5 driver has no ARM64 build, so terminal attach
  reports `MT5_WINDOWS_REQUIRED` and no trading is possible from
  this binary. There is no ARM64 installer — portable exe only.

## 5. macOS (Intel x64 and Apple Silicon arm64)

Download the `.dmg` for your chip from the release
(`-macos-x64` for Intel, `-macos-arm64` for Apple Silicon), verify
its SHA-256, open it, and drag `NSE MT5 Bridge.app` into
Applications. First launch: right-click the app → Open (the build is
unsigned, so Gatekeeper blocks a plain double-click the first time).

What runs: the bridge core — Control Center dashboard at
http://127.0.0.1:8788/ui, platform channel, settings, diagnostics.
What does not: anything that needs an MT5 terminal. The MetaTrader
screen reports `MT5_WINDOWS_REQUIRED` ("Windows MT5 terminal
required"). No MT5 password is ever asked for — there is no terminal
to sign in to.

Configuration and logs live under `~/NSEBridge` (config.json, logs).
Run from a terminal for logs in the foreground:

```bash
/Applications/NSE\ MT5\ Bridge.app/Contents/MacOS/NSE-MT5-Bridge --console
```

## 6. Linux (x64 and arm64, glibc)

Download the tarball for your architecture (`-linux-x64` or
`-linux-arm64`), verify its SHA-256, and extract it:

```bash
tar -xzf NSE-MT5-Bridge-<version>-linux-x64.tar.gz
cd NSE-MT5-Bridge-<version>-linux-x64
./NSE-MT5-Bridge --console
```

What runs: the bridge core — Control Center dashboard at
http://127.0.0.1:8788/ui, platform channel, settings, diagnostics.
What does not: anything that needs an MT5 terminal. The MetaTrader
screen reports `MT5_WINDOWS_REQUIRED` ("Windows MT5 terminal
required"). The build needs a glibc-based distro (no musl build is
published); no system packages are required beyond what the distro
already ships for a desktop session.

Configuration and logs live under `~/NSEBridge` (config.json, logs).
Keep the binary running (systemd unit, `nohup`, or a terminal
multiplexer) — there is no .deb/.rpm or auto-start service yet.

## After install

1. Launch `NSE MT5 Bridge` from the Start Menu / Applications /
   the extracted binary.
2. The onboarding flow appears on first run: MT5 discovery →
   platform endpoint → API key → validation → device registration →
   connection → initial sync → health check. Every step reflects real
   results. On macOS/Linux/ARM64 the MT5 discovery step reports
   `MT5_WINDOWS_REQUIRED` — that is the expected state there, not a
   failure; the remaining steps still work.
3. The API key comes from the NSE Platform site; on Windows it is
   stored in Windows Credential Manager (DPAPI fallback) — never in
   plaintext config. On macOS/Linux the keyring package is not
   bundled, so the bridge uses its file fallback
   (`~/NSEBridge/dev-secrets.json`, kept out of config.json and
   never logged); a proper OS-keychain store for those platforms is
   still owed.
4. No MT5 password is ever requested: sign in through MetaTrader
   itself; the Bridge only attaches to the running terminal.

## Requirements

| Requirement | Value |
|---|---|
| OS | Windows 10 / 11 x64 (trading) · Windows 11 ARM64 (core-only, or x64 build under emulation for trading) · macOS 13+ x64/arm64 (core-only) · Linux x64/arm64 glibc (core-only) |
| Admin rights | not required (per-user install on Windows; drag-install on macOS; plain extract on Linux) |
| MetaTrader 5 | any recent terminal build; the Bridge attaches to it via the official IPC driver — **Windows x64 build only** |
| Network | outbound HTTPS + WSS to `bridge.nexusverse.space` (no inbound ports, firewall friendly) |

## Restricted networks (proxy)

If your network's direct route to the platform hangs or is blocked
(verified 2026-11-21 on some ISPs: REST to `bridge.nexusverse.space`
times out while a local SOCKS5 proxy answers in ~3 s), configure an
explicit proxy — Settings → Network proxy (or `config.json`
`network_proxy` / env `NSE_BRIDGE_NETWORK_PROXY`):

```
socks5://127.0.0.1:10808        # local SOCKS5 (v2rayN/Clash default port)
http://proxy.example.org:3128   # HTTP CONNECT proxy
```

The proxy routes BOTH the platform REST traffic and the outbound WSS
channel. It is always a deliberate configuration — the ambient system
proxy environment is never trusted for API-key-bearing traffic. A
change needs a Bridge restart.

## Upgrading

- Windows desktop installer: run the new setup exe — upgrades in place.
- `install.ps1`: rerun it (idempotent); it detects the existing
  installation and upgrades when the version differs.
- macOS: open the new `.dmg` and replace the app in Applications
  (user data in `~/NSEBridge` is untouched).
- Linux: extract the new tarball over the old directory (user data
  in `~/NSEBridge` is untouched).
- Windows ARM64 portable exe: replace the file.
- In-app: the Updates screen checks `releases/latest`, verifies
  SHA-256 (+ Ed25519 when a key is provisioned — fail-closed without
  one), stages the verified artifact, and installs on next restart.

## Troubleshooting

- **"Bridge is already running"** — one Bridge per session; use
  `NSE-MT5-Bridge.exe status --json` to query the running one.
- **Installer asks to close the Bridge** — it is running; close it
  from the tray/window or kill `NSE-MT5-Bridge.exe`.
- **Checksum mismatch (exit 5)** — the download is corrupt or the
  release assets are inconsistent; rerun, and if it persists report
  the release tag.
- **No terminals found** — see docs/TROUBLESHOOTING.md §MetaTrader.
- **macOS: "cannot be opened because the developer cannot be
  verified"** — the build is unsigned: right-click the app → Open,
  then Open in the dialog. This is expected until signing is
  provisioned; it is not a corrupt download (verify the SHA-256 if
  unsure).
- **MetaTrader screen says "Windows MT5 terminal required"** — that
  is the honest state on macOS, Linux, and native-ARM64 Windows:
  MT5 trading needs the Windows x64 build on a Windows PC (§4 for
  ARM64 Windows).
- **Linux: binary will not start** — check it is the right
  architecture (`uname -m`: `x86_64` → `-linux-x64`, `aarch64` →
  `-linux-arm64`) and that the file is executable
  (`chmod +x NSE-MT5-Bridge`).
