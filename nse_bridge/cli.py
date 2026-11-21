"""Machine-readable CLI operations against a RUNNING Bridge (local API).

The desktop application is the default interface; this module is the
automation surface (CONTRACT-LOCAL §CLI). Commands talk to an
already-running Bridge over its authenticated loopback API and NEVER
start a second Bridge process or MT5 worker — routine status queries
must not fight the running instance for the serialized MT5 channel.

Envelope (stable field names, machine consumers only):

    {"ok": true,  "data": <endpoint payload>, "timestamp": "<UTC ISO8601>"}
    {"ok": false, "error": {"code": "...", "message": "..."},
     "timestamp": "<UTC ISO8601>"}

Rules (mission §D3):
- stdout carries exactly ONE complete JSON document per one-shot
  command — no ANSI colors, no progress, no human logs on stdout.
- diagnostics/messages go to stderr.
- timestamps are UTC.
- secrets never appear (the Bridge's own redaction applies server-side;
  the CLI additionally never prints its key).
- exit codes are predictable: 0 ok · 1 usage · 2 another instance
  (serve) · 3 Bridge not running · 4 operation failed / auth refused ·
  5 Bridge answered an error envelope.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

import httpx

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_ALREADY_RUNNING = 2
EXIT_NOT_RUNNING = 3
EXIT_FAILED = 4
EXIT_SERVER_ERROR = 5

REQUEST_TIMEOUT_S = 15.0


class CliError(Exception):
    """A CLI operation failure carrying its exit code."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _emit(payload: dict[str, Any]) -> None:
    """Write the one JSON document to stdout (UTF-8, no colors)."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _client(settings: Any) -> httpx.Client:
    from .secrets import initialize_secrets

    initialize_secrets(settings, strict=False)
    api_key = settings.local_api_key or ""
    return httpx.Client(
        base_url=f"http://127.0.0.1:{settings.port}",
        headers={"X-API-Key": api_key} if api_key else {},
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=5.0),
        trust_env=False,  # loopback only — never via an ambient proxy
    )


def _request(settings: Any, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One authenticated loopback call → the endpoint's payload dict.

    Raises CliError with the documented exit codes on every failure
    mode (Bridge down, auth refused, server-side error envelope).
    """
    try:
        with _client(settings) as client:
            response = client.request(method, path, json=json_body)
    except httpx.ConnectError:
        raise CliError(
            "BRIDGE_NOT_RUNNING",
            f"No Bridge is running on 127.0.0.1:{settings.port} — start it first "
            "(NSE-MT5-Bridge.exe, or --console for foreground service mode).",
            EXIT_NOT_RUNNING,
        ) from None
    except httpx.TimeoutException:
        raise CliError(
            "BRIDGE_TIMEOUT",
            f"The Bridge did not answer {method} {path} within {REQUEST_TIMEOUT_S:.0f}s.",
            EXIT_FAILED,
        ) from None
    except httpx.HTTPError as exc:
        raise CliError(
            "BRIDGE_UNREACHABLE",
            f"Loopback request failed: {type(exc).__name__}",
            EXIT_FAILED,
        ) from None
    if response.status_code == 401:
        raise CliError(
            "AUTH_FAILED",
            "The Bridge refused the local API key (stored in the OS secret store). "
            "If it was regenerated, restart the Bridge or re-enter credentials in Settings.",
            EXIT_FAILED,
        )
    try:
        payload = response.json()
    except ValueError:
        raise CliError(
            "MALFORMED_RESPONSE",
            f"The Bridge answered HTTP {response.status_code} with a non-JSON body.",
            EXIT_FAILED,
        ) from None
    if not isinstance(payload, dict):
        raise CliError(
            "MALFORMED_RESPONSE",
            f"The Bridge answered HTTP {response.status_code} with an unexpected shape.",
            EXIT_FAILED,
        )
    if response.status_code >= 400 or payload.get("ok") is False:
        raw_error = payload.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        raise CliError(
            str(error.get("code") or "BRIDGE_ERROR"),
            str(error.get("message") or f"Bridge answered HTTP {response.status_code}"),
            EXIT_SERVER_ERROR,
        )
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {"value": data}


def run_status(settings: Any, as_json: bool) -> int:
    """`status` — overall Bridge status (registry command get_bridge_status)."""
    data = _request(settings, "POST", "/api/v1/get_bridge_status")
    if as_json:
        _emit({"ok": True, "data": data, "timestamp": _timestamp()})
    else:
        channel = data.get("server_channel") or {}
        print(
            "NSE MT5 Bridge {version} — MT5 {mt5} · server channel {ws} · uptime {up:.0f}s".format(
                version=data.get("bridge_version", "?"),
                mt5="connected" if data.get("mt5_connected") else "down",
                ws="connected" if channel.get("connected") else "down",
                up=float(data.get("uptime_seconds", 0.0) or 0.0),
            )
        )
    return EXIT_OK


def run_health(settings: Any, as_json: bool) -> int:
    """`health` — component health with checks (command get_bridge_health)."""
    data = _request(settings, "POST", "/api/v1/get_bridge_health")
    if as_json:
        _emit({"ok": True, "data": data, "timestamp": _timestamp()})
    else:
        checks = data.get("checks") or {}
        print(
            "healthy={} · MT5 {} · server channel {} · MT5 calls {} ({} failed)".format(
                data.get("healthy"),
                "up" if checks.get("mt5_connected") else "down",
                "up" if checks.get("server_channel_connected") else "down",
                checks.get("total_mt5_calls", 0),
                checks.get("total_mt5_failures", 0),
            )
        )
    return EXIT_OK


def run_diagnostics(settings: Any, as_json: bool) -> int:
    """`diagnostics` — guided diagnostics summary (management endpoint)."""
    data = _request(settings, "GET", "/api/v1/diagnostics/summary")
    if as_json:
        _emit({"ok": True, "data": data, "timestamp": _timestamp()})
    else:
        for line in _human_diagnostic_lines(data):
            print(line)
    return EXIT_OK


def run_mt5_list(settings: Any, as_json: bool) -> int:
    """`mt5 list` — discovered MetaTrader terminals (management endpoint)."""
    data = _request(settings, "GET", "/api/v1/mt5/terminals")
    if as_json:
        _emit({"ok": True, "data": data, "timestamp": _timestamp()})
    else:
        terminals = data.get("terminals") or []
        if not terminals:
            print("No MetaTrader terminals discovered.")
            return EXIT_OK
        for t in terminals:
            marker = "*" if t.get("configured") else " "
            print(
                "{} {} ({}) — version {}".format(
                    marker,
                    t.get("path", "?"),
                    "running" if t.get("running") else "not running",
                    t.get("version") or "unknown",
                )
            )
        print("(* = configured terminal)")
    return EXIT_OK


def _human_diagnostic_lines(data: dict[str, Any]) -> list[str]:
    """Flatten the diagnostics summary into a few human lines."""
    lines: list[str] = []
    for key in ("bridge", "mt5", "network", "websocket", "sync"):
        section = data.get(key)
        if isinstance(section, dict):
            status = section.get("status") or section.get("healthy")
            lines.append(f"{key:>10}: {status}")
        elif section is not None:
            lines.append(f"{key:>10}: {section}")
    diagnostic = data.get("diagnostic")
    if isinstance(diagnostic, dict):
        lines.append(f"diagnostic: [{diagnostic.get('code')}] {diagnostic.get('title')}")
        for step in diagnostic.get("recovery") or []:
            lines.append(f"  - {step}")
    return lines


def emit_cli_error(exc: CliError) -> int:
    """Print the error envelope to stdout (machine-readable) + stderr hint."""
    _emit({"ok": False, "error": {"code": exc.code, "message": exc.message}, "timestamp": _timestamp()})
    print(f"error: {exc.message}", file=sys.stderr)
    return exc.exit_code
