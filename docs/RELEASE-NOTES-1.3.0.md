# NSE MT5 Bridge v1.3.0 — release notes (draft)

> Draft for the v1.3.0 GitHub release. The release job attaches the
> generated notes too; this file is the human-facing version, written
> for users, per platform. Verify every download against its `.sha256`
> sidecar (and `release-manifest.json`) before running it.

## What changed in v1.3.0

**The bridge now starts visibly.** Double-clicking the app used to
leave it running silently in the background with no window, no
browser, and no way to know it was up. It now shows a loading window
immediately, opens the Control Center dashboard in your browser as
soon as the server is ready, and a second double-click brings the
already-running dashboard to the front instead of silently dying.
If the server cannot start, you get a visible error — never silence.

> DRAFT NOTE (coordinator): this section describes the S1
> visible-startup tranche, which lands in v1.3.0 via a separate
> branch integrated before S3. Confirm its wording against the
> merged S1 work before publishing; delete this note at release time.

**One release, six downloads.** v1.3.0 ships a build for every
platform below — Windows x64 and ARM64, macOS Intel and Apple
Silicon, Linux x64 and arm64 — each with its own SHA-256 sidecar.
Pick the row that matches your machine.

**Honest MT5 status on every platform.** The MetaTrader 5 terminal
and its official driver exist for Windows x64 only, so only the
Windows x64 build can attach to a terminal and trade. Every other
build runs the full bridge core (dashboard, platform channel,
settings, diagnostics) and its MetaTrader screen says so plainly —
"Windows MT5 terminal required" — instead of showing an empty
terminal list that looks broken. Nothing is faked: no trading, no
positions, no account data from MT5 on those builds.

## What to download

| Your machine | Download | MT5 trading |
|---|---|---|
| Windows 10/11, Intel/AMD 64-bit | `NSE-MT5-Bridge-Setup-1.3.0.exe` (installer, recommended) or `NSE-MT5-Bridge.exe` (portable) | **Yes — this is the trading build** |
| Windows 11 on ARM64 (Snapdragon etc.) | `NSE-MT5-Bridge.exe` (the x64 build above, runs under emulation) to trade; `NSE-MT5-Bridge-1.3.0-windows-arm64.exe` (native, core-only) if you only need the dashboard | x64 build: yes. Native ARM64 build: no |
| Mac, Intel chip | `NSE-MT5-Bridge-1.3.0-macos-x64.dmg` — open it, drag the app into Applications | No — dashboard + platform channel only |
| Mac, Apple Silicon (M1/M2/M3/M4) | `NSE-MT5-Bridge-1.3.0-macos-arm64.dmg` — open it, drag the app into Applications | No — dashboard + platform channel only |
| Linux, Intel/AMD 64-bit | `NSE-MT5-Bridge-1.3.0-linux-x64.tar.gz` — extract, run `./NSE-MT5-Bridge --console` | No — dashboard + platform channel only |
| Linux, ARM64 (incl. Raspberry Pi 4/5, 64-bit OS) | `NSE-MT5-Bridge-1.3.0-linux-arm64.tar.gz` — extract, run `./NSE-MT5-Bridge --console` | No — dashboard + platform channel only |

Install steps for each row: [docs/INSTALLATION.md](INSTALLATION.md).

## First-run notes, per platform

- **Windows x64:** install, launch from the Start Menu, and the
  dashboard opens in your browser at `http://127.0.0.1:8788/ui`.
  Follow the guided setup: MT5 discovery → platform endpoint →
  API key → device registration → connection. Trading (close /
  modify positions, per CONTRACT v1) works once a terminal is
  attached.
- **Windows ARM64:** to trade, install the **x64** build — Windows
  runs it under emulation and it attaches to the x64 MT5 terminal
  normally. The native ARM64 exe is for the dashboard only; its
  MetaTrader screen reports "Windows MT5 terminal required".
- **macOS:** first launch needs a right-click → Open (the build is
  unsigned, so Gatekeeper blocks a plain double-click the first
  time). The dashboard is at `http://127.0.0.1:8788/ui`. The MT5
  setup step reports "Windows MT5 terminal required" — that is the
  expected state, not a failure; the remaining setup steps still
  work.
- **Linux:** needs a glibc-based distro (no musl build is
  published). Extract the tarball, run the binary, and open
  `http://127.0.0.1:8788/ui`. Keep it running with a systemd unit,
  `nohup`, or a terminal multiplexer — there is no .deb/.rpm or
  auto-start service yet. Same "Windows MT5 terminal required"
  state for the MetaTrader screen.

## What is NOT in this release

- **Opening new positions from the bridge is not enabled by this
  release.** The bridge executes the CONTRACT v1 operations
  (close / modify) on the Windows x64 build; new-position opening
  needs the platform-side P3 protocol, which is a separate program
  still in progress.
- **No remote-terminal mode.** A macOS/Linux/ARM64 bridge never
  connects to a terminal on another machine; MT5 features there
  stay disabled until a Windows x64 bridge attaches.
- **Builds are unsigned.** Windows SmartScreen and macOS Gatekeeper
  ask on first run. No signing / notarization key is provisioned
  yet. macOS ships without a custom `.icns` icon for now.

## Upgrading from v1.2.2

- Windows installer: run the new setup exe — it upgrades in place.
- Portable exe / ARM64 exe: replace the file.
- macOS: open the new `.dmg` and replace the app in Applications.
- Linux: extract the new tarball over the old directory.
- Your data (`%LOCALAPPDATA%\NSEBridge` on Windows,
  `~/NSEBridge` on macOS/Linux) is untouched by an upgrade.
- No config migration is needed for v1.3.0.
