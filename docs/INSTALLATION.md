# Installation

The Bridge ships as a self-contained Windows x64 single-file
executable — no Python, no Node.js, no package manager. Three ways to
install, all verified with SHA-256 checksums.

## 1. Desktop installer (recommended)

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

## 2. Direct install from GitHub (PowerShell)

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
signatures, but no signing key has been published yet —
`releases/latest` answers `signature: null`. Until a signed manifest
exists, verification relies on the SHA-256 manifest + sidecar over
HTTPS, and `install.ps1` (and the Bridge's own updater) say so
explicitly rather than pretending a signature check ran. No key is
fabricated and no verification is bypassed.

## 3. Portable executable

Download `NSE-MT5-Bridge.exe` from the release, place it anywhere,
run it. Everything the app needs is inside the file (Python runtime,
dependencies, GUI assets, icon). Configuration and credentials still
live under `%LOCALAPPDATA%\NSEBridge`.

## After install

1. Launch `NSE MT5 Bridge` from the Start Menu (or run the exe).
2. The onboarding flow appears on first run: MT5 discovery →
   platform endpoint → API key → validation → device registration →
   connection → initial sync → health check. Every step reflects real
   results.
3. The API key comes from the NSE Platform site; it is stored in
   Windows Credential Manager (DPAPI fallback) — never in plaintext
   config.
4. No MT5 password is ever requested: sign in through MetaTrader
   itself; the Bridge only attaches to the running terminal.

## Requirements

| Requirement | Value |
|---|---|
| OS | Windows 10 / 11 / Server 2016+ (x64; ARM64 unsupported — see docs/COMPATIBILITY.md) |
| Admin rights | not required (per-user install) |
| MetaTrader 5 | any recent terminal build; the Bridge attaches to it via the official IPC driver |
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

- Desktop installer: run the new setup exe — upgrades in place.
- `install.ps1`: rerun it (idempotent); it detects the existing
  installation and upgrades when the version differs.
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
