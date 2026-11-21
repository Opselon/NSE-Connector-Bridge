# CLI Reference

The Bridge is a desktop application first. The CLI exists for
automation, diagnostics, and service operation — it is fully
compatible with the GUI and never launches a second Bridge instance.

## Serve modes

```powershell
# Default: native desktop application (Connector Control Center)
NSE-MT5-Bridge.exe

# Foreground console service (structured logs on the console, NO GUI)
NSE-MT5-Bridge.exe --console

# Verbose diagnostic console (DEBUG telemetry + DEBUG logging)
NSE-MT5-Bridge.exe --console --diagnostic

# Headless service mode (no console window, no GUI)
NSE-MT5-Bridge.exe --gui none
```

Serve flags (unchanged from earlier releases):

| Flag | Meaning |
|---|---|
| `--host <h>` | bind host (default `127.0.0.1`; non-loopback requires `allow_non_loopback=true` AND the mandatory `X-API-Key`) |
| `--port <p>` | bind port (default `8788`) |
| `--gui auto\|webview\|browser\|none` | GUI mode (`auto` = native window with browser fallback) |
| `--no-connect` | skip automatic MT5 connect at boot |
| `--console` | foreground console service; attaches the parent console or allocates one (frozen exe), forces `--gui none` |
| `--diagnostic` | DEBUG telemetry events + DEBUG logging; on a frozen exe also attaches/allocates a console |

`--console` and `--diagnostic` compose; both keep Ctrl+C handling and
the graceful shutdown order (uvicorn drain → channel stop → driver
disconnect).

## Single instance

Only one Bridge may run per Windows session. A second launch exits
fast with **exit code 2** and an actionable message instead of failing
on the port bind after the boot wait. Query subcommands (below) never
acquire the mutex — they talk to the running instance.

## Machine-readable operations

All query commands talk to the **already-running** Bridge over its
authenticated loopback API (the key comes from the OS secret store —
no secrets on the command line, no second MT5 worker):

```powershell
NSE-MT5-Bridge.exe status --json
NSE-MT5-Bridge.exe health --json
NSE-MT5-Bridge.exe diagnostics --json
NSE-MT5-Bridge.exe mt5 list --json
```

Each one-shot command prints **exactly one complete JSON document** to
stdout: no ANSI colors, no progress animations, no human logs on
stdout (diagnostics go to stderr). Timestamps are UTC. Secrets are
redacted by the Bridge before they ever reach the CLI.

### Envelope

Success:

```json
{
  "ok": true,
  "data": { "bridge_version": "1.1.0", "mt5_connected": true, "...": "real fields from the endpoint" },
  "timestamp": "2026-11-21T10:00:00Z"
}
```

Failure (always on stdout, with a human hint on stderr):

```json
{
  "ok": false,
  "error": { "code": "BRIDGE_NOT_RUNNING", "message": "No Bridge is running on 127.0.0.1:8788 — start it first (NSE-MT5-Bridge.exe, or --console for foreground service mode)." },
  "timestamp": "2026-11-21T10:00:00Z"
}
```

`data` is the endpoint's own payload (registry command or management
route) — the CLI adds no invented fields. `status` reports the
registry command `get_bridge_status`; `health` reports
`get_bridge_health` (component checks); `diagnostics` reports the
guided diagnostics summary; `mt5 list` reports discovered terminals.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage error |
| 2 | another Bridge instance already owns the session (serve mode) |
| 3 | no Bridge is running (query mode) |
| 4 | operation failed / authentication refused / timeout |
| 5 | the Bridge answered with an error envelope |

## Examples

```powershell
# Watchtower-style health check
$out = NSE-MT5-Bridge.exe health --json | ConvertFrom-Json
if (-not $out.ok -or -not $out.data.healthy) { Write-Host "bridge unhealthy: $($out.error.message)" }

# CI smoke: bridge must answer with machine-readable status
NSE-MT5-Bridge.exe status --json
if ($LASTEXITCODE -ne 0) { throw "bridge status failed ($LASTEXITCODE)" }

# List discovered terminals (human-readable when --json is omitted)
NSE-MT5-Bridge.exe mt5 list
```

## What the CLI deliberately does NOT do

- It never starts a Bridge, MT5 worker, or channel of its own —
  routine queries are pure loopback API calls (mission §D4).
- It never accepts secrets as arguments (process command lines are
  readable by other local processes); credentials live in the OS
  secret store and are configured once via the desktop app.
- It never executes trades; trading operations travel the Platform's
  server channel under the contract's server-side gate chain.
