"""Management endpoints: settings, pre-flight connection checks, channel control.

These sit on top of the 90-command surface — they operate the BRIDGE itself
(configuration, diagnostics, server-channel lifecycle), not the broker.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from .. import BRIDGE_VERSION
from ..config import persist_settings, validate_server_base_url, validate_server_url
from ..engine.common import BridgeContext, datetime_now_iso
from ..errors import BridgeError, ErrorCode
from ..logging_setup import redact_text
from ..secrets import get_secret_store
from ..server.errors import ServerError
from ..updates import Updater

router = APIRouter(prefix="/api/v1", tags=["system"])


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
class SettingsUpdate(BaseModel):
    """Partial bridge configuration update. Omitted fields stay unchanged.

    Secret fields (``server_token``, ``local_api_key``) are accepted
    here but NEVER written to config.json — the update handler routes
    them through the secret store.
    """

    server_url: str | None = Field(None, description="NSE server channel URL (wss://...; empty = disabled)")
    server_token: str | None = Field(None, description="API key issued on the NSE Platform site (stored in the secret store)")
    local_api_key: str | None = Field(None, description="Local X-API-Key gate (mandatory; stored in the secret store)")
    environment: Literal["production", "staging", "dev"] | None = Field(
        None, description="Server environment (production | staging | dev)"
    )
    server_base_url: str | None = Field(
        None, description="NSE server REST base URL (https://...; changing it needs a bridge restart)"
    )
    magic: int | None = Field(None, ge=0, le=2**31 - 1)
    # Empty string = clear the configured value (no default symbol /
    # timeframe is ever invented by the bridge).
    bot_symbol: str | None = Field(None, min_length=0, max_length=40)
    default_timeframe: str | None = Field(None, min_length=0, max_length=4)
    broker_utc_offset_minutes: int | None = Field(None, ge=-720, le=720)
    strict_magic_guard: bool | None = None
    stream_account_interval_ms: int | None = Field(None, ge=200, le=60000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    gui_mode: Literal["auto", "webview", "browser", "none"] | None = None
    mt5_terminal_path: str | None = None
    mt5_custom_terminal_dirs: str | None = Field(
        None, description="Comma-separated extra directories searched for terminals (custom/portable installs)"
    )


EDITABLE_KEYS = set(SettingsUpdate.model_fields.keys())


def _mask(token: str) -> str:
    if not token:
        return ""
    return "•" * max(0, len(token) - 4) + token[-4:]


@router.get("/settings", summary="Get bridge settings", description="Current configuration (secrets masked).")
async def get_settings(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    s = ctx.settings
    return {
        "ok": True,
        "data": {
            "server_url": s.server_url,
            "server_base_url": s.server_base_url,
            "environment": s.environment,
            "server_token_masked": _mask(s.server_token),
            "server_token_set": bool(s.server_token),
            "local_api_key_set": bool(s.local_api_key),
            "magic": s.magic,
            "bot_symbol": s.bot_symbol,
            "default_timeframe": s.default_timeframe,
            "broker_utc_offset_minutes": s.broker_utc_offset_minutes,
            "strict_magic_guard": s.strict_magic_guard,
            "stream_account_interval_ms": s.stream_account_interval_ms,
            "log_level": s.log_level,
            "gui_mode": s.gui_mode,
            "mt5_terminal_path": s.mt5_terminal_path,
            "mt5_custom_terminal_dirs": s.mt5_custom_terminal_dirs,
            "device_id": ctx.get_or_create_device_id(),
            "config_file": str(s.config_file),
            "server_channel": ctx.server_channel_state,
        },
    }


@router.put("/settings", summary="Update bridge settings",
    description="Partial update. Non-secret fields are persisted to config.json. "
    "Secrets (server_token, local_api_key) are routed through the secret store and "
    "never written to config.json. Server-channel fields apply immediately "
    "(the outbound channel restarts when URL/token change). Terminal fields "
    "(mt5_terminal_path, mt5_custom_terminal_dirs) re-run discovery immediately "
    "(fresh list returned in this response); changing the terminal path while the "
    "driver is connected disconnects it from the old terminal instead of keeping "
    "a stale selection.")
async def update_settings(request: Request, update: SettingsUpdate) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    s = ctx.settings
    changed: list[str] = []
    channel_needs_restart = False
    values = update.model_dump(exclude_none=True)
    # Secrets go through the secret store, never persist_settings.
    secret_updates = {
        key: values.pop(key) for key in ("server_token", "local_api_key") if key in values
    }
    if secret_updates:
        store = get_secret_store(s.data_dir)
        for key, value in secret_updates.items():
            if getattr(s, key) == value:
                continue
            store.set_secret(key, value)
            setattr(s, key, value)
            changed.append(key)
            if key == "server_token":
                channel_needs_restart = True
    if "server_url" in values:
        try:
            validate_server_url(values["server_url"])
        except ValueError as exc:
            raise BridgeError(ErrorCode.VALIDATION, str(exc), http_status=422)
    if "server_base_url" in values:
        try:
            validate_server_base_url(
                values["server_base_url"], environment=values.get("environment", s.environment)
            )
        except ValueError as exc:
            raise BridgeError(ErrorCode.VALIDATION, str(exc), http_status=422)
    # environment / server_base_url are baked into the REST client at
    # construction — they take effect after a bridge restart (never a
    # silent mid-flight switch). The response flags that honestly.
    restart_required = "environment" in values or "server_base_url" in values
    for key, value in values.items():
        if key not in EDITABLE_KEYS or getattr(s, key) == value:
            continue
        setattr(s, key, value)
        changed.append(key)
        if key in ("server_url", "server_token"):
            channel_needs_restart = True
    if changed:
        try:
            persist_settings(s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SETTINGS] persist failed: {}", redact_text(str(exc)))
        logger.info("[SETTINGS] updated: {}", ", ".join(changed))

    restarted = False
    if channel_needs_restart:
        # A new API key invalidates the old key's terminal enrolment
        # state (FAILED_AUTH is terminal for the OLD key only) — reset
        # the state machine first so it re-enrols with the new key,
        # then restart the channel (it gates on REGISTERED).
        if "server_token" in changed:
            enrolment = getattr(request.app.state, "enrolment", None)
            if enrolment is not None:
                enrolment.reset_for_new_key()
        channel = getattr(request.app.state, "channel", None)
        if channel is not None:
            await channel.stop()
            channel.start()
            restarted = True

    # Terminal re-discovery (spec §3: re-discover after configuration
    # changes). Changing the terminal path or the custom terminal dirs
    # re-runs discovery immediately and the fresh list is returned in
    # this response — callers never have to guess whether a later GET
    # will pick the change up. If the configured terminal path changed
    # while the driver was attached, the driver is still connected to
    # the OLD terminal: disconnect it rather than silently keep a
    # stale selected terminal (the next connect attaches to the new
    # path). A custom-dirs-only change leaves the attached terminal
    # alone — the selection did not move.
    terminals_rediscovered = bool(
        {"mt5_terminal_path", "mt5_custom_terminal_dirs"} & set(changed)
    )
    discovery: dict[str, Any] | None = None
    driver_disconnected = False
    if terminals_rediscovered:
        discovery = _discover_terminals(ctx)
        if "mt5_terminal_path" in changed and ctx.driver.connected:
            ctx.driver.disconnect()
            driver_disconnected = True
            logger.info(
                "[SETTINGS] terminal path changed while connected — "
                "driver disconnected from the old terminal"
            )
    return {
        "ok": True,
        "data": {
            "changed": changed,
            "server_channel_restarted": restarted,
            "restart_required": restart_required,
            "server_channel": ctx.server_channel_state,
            "terminals_rediscovered": terminals_rediscovered,
            "driver_disconnected": driver_disconnected,
            "discovery": discovery,
        },
    }


# ---------------------------------------------------------------------------
# pre-flight connection checks
# ---------------------------------------------------------------------------
class CheckResult(BaseModel):
    id: str
    title: str
    status: Literal["ok", "fail", "warn"]
    detail: str | None = None
    hint: str | None = None


AUTOTRADING_HINT = (
    "Three switches must all be right (no API can toggle them — terminal security controls): "
    "1) the 'Algo Trading' toolbar button must be GREEN (Ctrl+E); "
    "2) Tools → Options → Experts → 'Allow algorithmic trading' checked; "
    "3) THE HIDDEN KILLER: in the same tab, 'Disable algorithmic trading via external "
    "Python API' must be UNCHECKED — when it is checked, the toolbar shows green but every "
    "Python-API order is rejected with retcode 10027 'AutoTrading disabled by client'."
)


@router.post("/run_connection_checks", summary="Run pre-flight connection checks",
    description="Full readiness checklist: driver attach, terminal link, AutoTrading button, account "
    "permissions, symbol availability, tick freshness, server-channel reachability.")
async def run_connection_checks(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    checks: list[CheckResult] = []

    def add(cid: str, title: str, status: str, detail: str | None = None, hint: str | None = None) -> None:
        checks.append(CheckResult(id=cid, title=title, status=status, detail=detail, hint=hint).model_dump())

    # 1. driver attach
    if ctx.driver.connected:
        add("mt5_attached", "MT5 driver attached", "ok", "Official MetaTrader5 IPC driver is attached.")
    else:
        state = ctx.driver.connection_state_dict()
        add("mt5_attached", "MT5 driver attached", "fail",
            f"State: {state['state']}" + (f" — {state['detail']}" if state["detail"] else ""),
            "Run connect_mt5 (or restart the bridge) — check the terminal is running.")

    # 2. terminal link
    terminal = ctx.driver.refresh_terminal() or {}
    if terminal.get("connected"):
        add("terminal_connected", "Terminal connected to broker", "ok",
            f"{terminal.get('name') or 'MetaTrader 5'}")
    elif ctx.driver.connected:
        add("terminal_connected", "Terminal connected to broker", "fail",
            "Terminal reports disconnected from the trade server.",
            "Check the terminal's connection indicator (bottom-right) and broker login.")
    else:
        add("terminal_connected", "Terminal connected to broker", "fail", "Driver not attached.")

    # 3. AutoTrading button — the classic silent killer
    trade_allowed = terminal.get("trade_allowed")
    if trade_allowed is True:
        add("autotrading_enabled", "Algo Trading enabled", "ok", "Terminal AutoTrading button is ON.")
    elif trade_allowed is False:
        add("autotrading_enabled", "Algo Trading enabled", "fail",
            "Terminal AutoTrading button is OFF — every trade command is rejected "
            '(retcode 10027, "AutoTrading disabled by client").',
            AUTOTRADING_HINT)
    else:
        add("autotrading_enabled", "Algo Trading enabled", "warn", "Unknown (driver not attached).")

    # 4. account permissions
    if ctx.driver.connected:
        try:
            from ..engine.common import account_info_model

            acc = await asyncio.to_thread(account_info_model, ctx)
            ok = bool(acc.trade_allowed) and bool(acc.trade_expert)
            add(
                "account_permissions", "Account trading permissions", "ok" if ok else "fail",
                f"trade_allowed={acc.trade_allowed}, trade_expert={acc.trade_expert}, "
                f"trade_mode={acc.trade_mode_label}",
                None if ok else "The account itself restricts trading (investor password? trade mode?). "
                "Log in with the master password and an account that allows expert trading.",
            )
        except BridgeError as exc:
            add("account_permissions", "Account trading permissions", "fail", exc.message)
    else:
        add("account_permissions", "Account trading permissions", "fail", "Driver not attached.")

    # 5. bot symbol availability + tick freshness (skipped with a warn
    # when no bot_symbol is configured — there is no default symbol).
    if not ctx.settings.bot_symbol:
        add("symbol_market", "Market data: (no symbol configured)", "warn",
            "No bot_symbol is configured — set one in Settings to probe market data.",
            "Settings → Primary symbol, e.g. the symbol the server trades on this device.")
    elif ctx.driver.connected:
        try:
            from ..engine.common import require_symbol_info
            from ..engine.symbols import _fresh_tick

            spec = await asyncio.to_thread(require_symbol_info, ctx, ctx.settings.bot_symbol)
            tick = await asyncio.to_thread(_fresh_tick, ctx, ctx.settings.bot_symbol)
            fresh = (tick.freshness_ms or 1e9) < 30_000
            add(
                "symbol_market", f"Market data: {ctx.settings.bot_symbol}",
                "ok" if fresh else "warn",
                f"digits={spec.digits}, point={spec.point}, bid={tick.bid}, freshness={round(tick.freshness_ms or -1)}ms",
                None if fresh else "Tick older than 30s — market may be closed or the symbol is stale.",
            )
        except BridgeError as exc:
            add("symbol_market", f"Market data: {ctx.settings.bot_symbol}", "fail", exc.message,
                "Add the symbol to Market Watch (right-click → Symbols) or check its exact name.")
    else:
        add("symbol_market", f"Market data: {ctx.settings.bot_symbol}", "fail", "Driver not attached.")

    # 6. server channel configuration + reachability
    s = ctx.settings
    if not s.server_url or not s.server_token:
        add("server_channel", "NSE server channel", "warn",
            "Not configured — set the server URL and the API key from the NSE Platform site "
            "(Settings tab). The bridge works standalone without it.",
            "Settings → paste the API key generated on the NSE Platform site.")
    else:
        state = ctx.server_channel_state
        if state.get("connected"):
            add("server_channel", "NSE server channel", "ok",
                f"Connected to {s.server_url} (reconnects: {state.get('reconnects', 0)}).")
        else:
            reachable, err = await _probe_server(s.server_url, s.server_token)
            if reachable:
                add("server_channel", "NSE server channel", "warn",
                    f"URL reachable but the persistent channel is not connected — {err or 'reconnecting'}.")
            else:
                add("server_channel", "NSE server channel", "fail",
                    f"Cannot reach {s.server_url}: {err}",
                    "Verify the URL (wss://...) and that the API key is valid on the NSE Platform site.")

    overall = "ok" if all(c["status"] == "ok" for c in checks) else (
        "fail" if any(c["status"] == "fail" for c in checks) else "warn"
    )
    return {
        "ok": True,
        "data": {
            "overall": overall,
            "checks": checks,
            "generated_at": datetime_now_iso(),
        },
    }


async def _probe_server(url: str, token: str, timeout: float = 5.0) -> tuple[bool, str | None]:
    """One-shot reachability probe for the configured NSE server URL."""
    try:
        import websockets

        async def probe() -> None:
            headers = {"Authorization": f"Bearer {token}"}
            async with websockets.connect(url, additional_headers=headers, open_timeout=timeout):
                pass  # handshake success is all we need

        await asyncio.wait_for(probe(), timeout=timeout + 1.0)
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, redact_text(str(exc))[:200]


# ---------------------------------------------------------------------------
# server channel control
# ---------------------------------------------------------------------------
@router.post("/server_channel/restart", summary="Restart the NSE server channel",
    description="Disconnect and reconnect the outbound server channel with current settings.")
async def restart_server_channel(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    channel = getattr(request.app.state, "channel", None)
    if channel is None:
        raise BridgeError(ErrorCode.INTERNAL, "Server channel not initialised.", http_status=500)
    await channel.stop()
    channel.start()
    await asyncio.sleep(0.3)
    return {"ok": True, "data": {"server_channel": ctx.server_channel_state}}


# ---------------------------------------------------------------------------
# Phase 4 — Connector Control Center: guided MT5 diagnostics
# ---------------------------------------------------------------------------
#: Structured MT5 diagnostic codes (ARCHITECTURE-CONTRACT §5/§6). Every
#: code carries a human-readable explanation + actionable recovery
#: steps; ordinary users never see a raw stack trace.
MT5_DIAGNOSTICS: dict[str, dict[str, Any]] = {
    "MT5_NOT_FOUND": {
        "title": "No MetaTrader 5 terminal found",
        "explanation": (
            "The bridge could not find a MetaTrader 5 terminal on this machine — "
            "no configured path and none of the standard install locations exists."
        ),
        "recovery": [
            "Install MetaTrader 5 from your broker, then press Retry diagnostics.",
            ("If MT5 lives in a custom folder, select it on the MetaTrader screen "
            "(it is saved as the terminal path)."),
            ("Portable installs: set the NSE_MT5_TERMINAL_PATH environment variable "
            "to the terminal64.exe path and restart the bridge."),
        ],
    },
    "MT5_NOT_RUNNING": {
        "title": "Terminal found but not running",
        "explanation": (
            "A terminal was found on this machine, but it is not running or the "
            "bridge could not attach to it yet."
        ),
        "recovery": [
            "Start the MetaTrader 5 terminal and wait for it to finish loading.",
            "Then press Connect on the MetaTrader screen.",
            ("If it still fails, close the terminal completely (check the system "
            "tray icon) and start it again, then retry."),
        ],
    },
    "MT5_IPC_FAILED": {
        "title": "Terminal attach (IPC) failed",
        "explanation": (
            "The bridge found the terminal but the local IPC attach failed — "
            "initialize() was rejected by the terminal."
        ),
        "recovery": [
            "Close the terminal completely (check the system tray icon) and start it again.",
            "Make sure no other bridge instance is already attached to the same terminal.",
            "If the terminal was just updated, restart Windows once, then press Retry diagnostics.",
        ],
    },
    "MT5_ACCOUNT_NOT_READY": {
        "title": "No trading account logged in",
        "explanation": (
            "The terminal is running, but no trading account is logged in, so the "
            "bridge cannot read account data."
        ),
        "recovery": [
            ("Open the MT5 terminal and log in to your trading account "
            "(File → Login to Trade Account)."),
            "The bridge never asks for your MT5 password — sign in through MT5 itself.",
            "Then press Connect on the MetaTrader screen.",
        ],
    },
    "MT5_UNSUPPORTED_VERSION": {
        "title": "Terminal build is too old",
        "explanation": (
            "The terminal connected, but its build is older than the minimum this "
            "bridge supports, so some calls may behave unexpectedly."
        ),
        "recovery": [
            ("Update MetaTrader 5 (Help → Check for Updates) or install the latest "
            "build from your broker."),
            "Then reconnect and press Retry diagnostics.",
        ],
    },
}

#: Conservative local floor for the terminal build number. CONTRACT v1
#: defines NO minimum terminal build — this floor is intentionally old
#: (build 3000, 2021-era) so any terminal from the last several years
#: passes; it exists so a genuinely ancient terminal produces a
#: structured code instead of cryptic IPC failures.
MIN_SUPPORTED_TERMINAL_BUILD = 3000


def _terminal_build(pkg_version: str | None) -> int | None:
    """Build number from the driver's pkg_version ("<api>.<build>.<date>")."""
    if not pkg_version:
        return None
    parts = str(pkg_version).split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _discover_terminals(ctx: BridgeContext) -> dict[str, Any]:
    """Configured + auto-discovered terminal paths (deduped).

    Discovery reuses the driver's multi-terminal enumeration
    (Phase 5): configured path, env var, standard install locations,
    App Paths registry, custom/portable directories, running processes
    — each with version + running status. Selection stays user-driven:
    ``selected`` marks the effective path (configured wins, else the
    first discovered); discovery never overwrites configuration.
    """
    from ..mt5.driver import discover_terminals

    configured = (ctx.settings.mt5_terminal_path or "").strip()
    discovered = discover_terminals(ctx.settings)
    effective = configured or (discovered[0].path if discovered else "") or ""
    terminals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for info in discovered:
        item = info.to_dict()
        item["selected"] = info.path == effective
        terminals.append(item)
        seen.add(info.path)
    if configured and configured not in seen:
        # Configured path discovery did not report (defensive: the
        # driver always includes it) — still reported, never dropped.
        terminals.append(
            {
                "path": configured,
                "version": None,
                "running": False,
                "source": "configured",
                "exists": Path(configured).is_file(),
                "data_dir": None,
                "selected": True,
            }
        )
    return {
        "terminals": terminals,
        "selected_path": effective or None,
        "configured_path": configured or None,
        "can_open": sys.platform == "win32",
    }


def _derive_mt5_diagnostic(
    ctx: BridgeContext, terminals: list[dict[str, Any]]
) -> tuple[str | None, str]:
    """Map driver state + discovery onto one structured code.

    Returns ``(code, severity)``; ``(None, "ok")`` when healthy. The
    driver itself is never rewritten — this only reads
    ``connection_state_dict`` / ``diagnostic_code``.
    """
    state = ctx.driver.connection_state_dict()
    if ctx.driver.connected:
        build = _terminal_build(state.get("pkg_version"))
        if build is not None and build < MIN_SUPPORTED_TERMINAL_BUILD:
            return "MT5_UNSUPPORTED_VERSION", "warning"
        return None, "ok"
    code = state.get("diagnostic_code")
    if isinstance(code, str) and code in MT5_DIAGNOSTICS:
        return code, "error"
    if not any(t.get("exists") for t in terminals):
        return "MT5_NOT_FOUND", "error"
    if state.get("state") == "TERMINAL_ERROR":
        return "MT5_IPC_FAILED", "error"
    return "MT5_NOT_RUNNING", "error"


# ---------------------------------------------------------------------------
# Account & Subscription (read-only, server-driven)
# ---------------------------------------------------------------------------
def _default_entitlements() -> dict[str, Any]:
    return {
        "history_sync": False,
        "ai_coach": False,
        "position_manager": False,
        "auto_trade": False,
        "max_devices": 0,
        "max_accounts": 0,
    }


def _account_subscription_payload(ctx: BridgeContext) -> dict[str, Any]:
    """Read-only snapshot for the Account & Subscription screen.

    Source of truth is the last stored validate-key result (CONTRACT
    v1 §3.1). Falls back to the live REST client state when no snapshot
    was stored yet. The API key is NEVER included — only masked status.
    All billing/subscription decisions come from the server; the bridge
    invents no plan, tariff, or price.
    """
    s = ctx.settings
    stored = dict(ctx.validate_key_result or {})
    if not stored:
        rest = ctx.rest_client
        if rest is not None and getattr(rest, "user_id", None):
            ent = getattr(rest, "entitlements", None)
            stored = {
                "valid": True,
                "user_id": rest.user_id,
                "subscription": dict(getattr(rest, "subscription", None) or {}),
                "entitlements": asdict(ent) if ent is not None else _default_entitlements(),
                "validated_at": None,
            }
    entitlements = stored.get("entitlements")
    if not isinstance(entitlements, dict):
        entitlements = _default_entitlements()
    subscription = stored.get("subscription")
    if not isinstance(subscription, dict):
        subscription = {}
    rest = ctx.rest_client
    return {
        "validated": bool(stored.get("valid")),
        "validated_at": stored.get("validated_at"),
        "validation_error": stored.get("error"),
        "user_id": stored.get("user_id"),
        "subscription": dict(subscription),
        "entitlements": {**_default_entitlements(), **entitlements},
        "device_id": ctx.get_or_create_device_id(),
        "session_expires_at": getattr(rest, "session_expires_at", None),
        "api_key_set": bool(s.server_token),
        "api_key_masked": _mask(s.server_token),
    }


@router.get("/account/subscription", summary="Read-only account & subscription (server-resolved)",
    description="Last stored validate-key result (§3.1): identity, subscription, entitlements, "
    "device allowance, masked key status. No payment processing; nothing is invented server-side.")
async def get_account_subscription(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    return {"ok": True, "data": _account_subscription_payload(ctx)}


@router.post("/account/validate-key", summary="Validate the API key against NSE Platform",
    description="Calls CONTRACT v1 §3.1 validate-key through the REST client and stores the "
    "server-resolved result on the bridge context for the read-only Account screen. "
    "Never returns key material.")
async def validate_api_key(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    rest = ctx.rest_client
    if rest is None:
        raise BridgeError(
            ErrorCode.INTERNAL,
            "Server client not initialised yet — the bridge is still starting.",
            http_status=503,
        )
    if not ctx.settings.server_token:
        raise BridgeError(
            ErrorCode.VALIDATION,
            "No API key configured — paste the key from the NSE Platform site in Settings first.",
            http_status=422,
        )
    try:
        result = await rest.validate_key()
    except ServerError as exc:
        ctx.validate_key_result = {
            "valid": False,
            "error": redact_text(str(exc))[:300],
            "validated_at": datetime_now_iso(),
        }
        status = exc.http_status if isinstance(exc.http_status, int) and exc.http_status >= 400 else 401
        raise BridgeError(
            ErrorCode.UNAUTHORIZED,
            f"Key validation failed: {redact_text(str(exc))[:300]}",
            http_status=status,
        ) from exc
    ctx.validate_key_result = {
        "valid": True,
        "user_id": result.user_id,
        "subscription": dict(result.subscription or {}),
        "entitlements": asdict(result.entitlements),
        "validated_at": datetime_now_iso(),
    }
    return {"ok": True, "data": _account_subscription_payload(ctx)}


# ---------------------------------------------------------------------------
# Updates & Releases (read-only check; download/install is Phase 5)
# ---------------------------------------------------------------------------
def _version_tuple(version: str | None) -> tuple[int, ...] | None:
    if not version:
        return None
    parts: list[int] = []
    for piece in str(version).strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            return None
    return tuple(parts) if parts else None


def _is_newer(latest: str | None, installed: str) -> bool:
    a, b = _version_tuple(latest), _version_tuple(installed)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return (a + (0,) * (n - len(a))) > (b + (0,) * (n - len(b)))


@router.get("/updates/latest", summary="Latest stable release (CONTRACT v1 §3.5 releases/latest)",
    description="Read-only update check via the REST client. Download + install belong to the "
    "Phase 5 updater — this endpoint only reports status and never downloads anything.")
async def get_updates_latest(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    installed = BRIDGE_VERSION
    base: dict[str, Any] = {
        "installed_version": installed,
        "latest": None,
        "update_available": False,
        "status": "unknown",
        "detail": "",
    }
    rest = ctx.rest_client
    if rest is None:
        base.update(
            status="unavailable",
            detail="Server client not initialised yet — the bridge is still starting.",
        )
        return {"ok": True, "data": base}
    if not ctx.settings.server_token:
        base.update(
            status="not_configured",
            detail="No API key configured — set it in Settings first, then check again.",
        )
        return {"ok": True, "data": base}
    try:
        rel = await rest.releases_latest()
    except ServerError as exc:
        base.update(status="check_failed", detail=redact_text(str(exc))[:300])
        return {"ok": True, "data": base}
    base["latest"] = {
        "version": rel.version,
        "download_url": rel.download_url,
        "sha256": rel.sha256,
        "size_bytes": rel.size_bytes,
        "min_connector_version": rel.min_connector_version,
        "signature_present": bool(rel.signature),
        "released_at": rel.released_at,
    }
    if rel.version is None:
        base.update(status="unknown", detail="The server answered but reported no version.")
    elif _is_newer(rel.version, installed):
        base.update(
            status="update_available",
            update_available=True,
            detail=f"Version {rel.version} is available (installed {installed}).",
        )
    else:
        base.update(
            status="current",
            detail=f"Installed {installed} is the latest known version.",
        )
    return {"ok": True, "data": base}


# ---------------------------------------------------------------------------
# Updates & Releases — verified updater (Phase 5)
# ---------------------------------------------------------------------------
def _get_updater(request: Request) -> Updater:
    """Updater instance for this app (created lazily; the lifespan also creates one)."""
    updater = getattr(request.app.state, "updater", None)
    if updater is None:
        ctx: BridgeContext = request.app.state.ctx
        updater = Updater(ctx.settings, rest_client_getter=lambda: ctx.rest_client)
        request.app.state.updater = updater
    return updater


@router.get("/updates/status", summary="Verified updater status",
    description="Current updater state: check result, staged (verified) update, "
    "restart requirement, and whether an update public key is configured. Pollable by the GUI.")
async def get_updates_status(request: Request) -> dict[str, Any]:
    return {"ok": True, "data": _get_updater(request).status()}


@router.post("/updates/check", summary="Check for updates (CONTRACT v1 §3.5 releases/latest)",
    description="Polls releases/latest through the REST client. Reports availability or "
    "incompatibility (min_connector_version). Never downloads anything.")
async def post_updates_check(request: Request) -> dict[str, Any]:
    return {"ok": True, "data": await _get_updater(request).check()}


@router.post("/updates/download", summary="Download + verify + stage the latest release",
    description="Downloads the release artifact, verifies SHA-256 AND the Ed25519 signature, "
    "then stages it for install on restart. Refuses with a structured error (nothing staged) "
    "when no public key is configured, the signature/SHA-256 is missing, verification fails, "
    "or min_connector_version is not satisfied. Never executes the downloaded bytes.")
async def post_updates_download(request: Request) -> dict[str, Any]:
    return {"ok": True, "data": await _get_updater(request).download()}


# ---------------------------------------------------------------------------
# MetaTrader terminals (discovery + open)
# ---------------------------------------------------------------------------
@router.get("/mt5/terminals", summary="Detected MetaTrader 5 terminals",
    description="All discovered terminals (Phase 5): configured path, env var, standard install "
    "locations, App Paths registry, custom/portable directories, running processes — each with "
    "version, data directory (when determinable, else null — never guessed), + running status. "
    "Discovery re-runs on every request, and PUT /settings re-runs it immediately when the "
    "terminal path or custom terminal dirs change. Selection stays user-driven "
    "(PUT /settings mt5_terminal_path).")
async def list_mt5_terminals(request: Request) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    state = ctx.driver.connection_state_dict()
    return {
        "ok": True,
        "data": {
            **_discover_terminals(ctx),
            "driver_state": state["state"],
            "connected": ctx.driver.connected,
            "diagnostic_code": state.get("diagnostic_code"),
        },
    }


class TerminalOpenRequest(BaseModel):
    path: str = Field(..., min_length=1)


@router.post("/mt5/terminals/open", summary="Open the selected MT5 terminal",
    description="Launches the terminal executable. The path must be one of the discovered / "
    "configured terminals — arbitrary paths are rejected. Windows only; on Linux this "
    "reports honestly instead of pretending to launch.")
async def open_mt5_terminal(request: Request, body: TerminalOpenRequest) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    allowed = {t["path"] for t in _discover_terminals(ctx)["terminals"]}
    if body.path not in allowed:
        raise BridgeError(
            ErrorCode.VALIDATION,
            "Unknown terminal path — select a discovered terminal first.",
            http_status=422,
        )
    if sys.platform != "win32":
        raise BridgeError(
            ErrorCode.INTERNAL,
            "Opening a terminal is only supported on Windows — this bridge runs on Linux.",
            http_status=501,
        )
    if not Path(body.path).is_file():
        raise BridgeError(
            ErrorCode.NOT_FOUND,
            "Terminal executable not found at the selected path.",
            http_status=404,
        )
    try:
        await asyncio.to_thread(
            subprocess.Popen, [body.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        raise BridgeError(
            ErrorCode.INTERNAL, f"Could not launch the terminal: {exc}", http_status=500
        ) from exc
    return {"ok": True, "data": {"launched": body.path}}


# ---------------------------------------------------------------------------
# Diagnostics summary (guided diagnostics for the Control Center)
# ---------------------------------------------------------------------------
@router.get("/diagnostics/summary", summary="Guided diagnostics summary",
    description="Bridge / MT5 / network / WebSocket health, sync status, last errors, and the "
    "structured MT5 diagnostic code with human-readable guidance. No stack traces.")
async def diagnostics_summary(request: Request) -> dict[str, Any]:
    from ..engine.connection import get_mt5_connection_status
    from ..engine.diagnostics import get_bridge_health
    from ..engine.system import get_bridge_status

    ctx: BridgeContext = request.app.state.ctx
    terminals = _discover_terminals(ctx)
    code, severity = _derive_mt5_diagnostic(ctx, terminals["terminals"])
    diagnostic = None
    if code is not None:
        entry = MT5_DIAGNOSTICS[code]
        diagnostic = {
            "code": code,
            "severity": severity,
            "title": entry["title"],
            "explanation": entry["explanation"],
            "recovery": list(entry["recovery"]),
        }
    # Network: a live channel proves reachability; otherwise probe once.
    s = ctx.settings
    channel = dict(ctx.server_channel_state)
    if channel.get("connected"):
        network = {"status": "ok", "detail": "WebSocket channel is connected."}
    elif not s.server_token:
        network = {"status": "not_configured", "detail": "No API key configured — set it in Settings first."}
    else:
        reachable, err = await _probe_server(s.resolved_server_ws_url, s.server_token)
        network = {
            "status": "ok" if reachable else "fail",
            "detail": "Server URL reachable." if reachable else f"Cannot reach server: {err}",
        }
    sync_engine = ctx.sync_engine
    sync = {
        "available": sync_engine is not None,
        "pending_batches": sync_engine.pending_count() if sync_engine is not None else None,
        "checkpoint": sync_engine.checkpoint.iso if sync_engine is not None else None,
    }
    # Reliability (§8): telemetry queue state + per-scenario
    # self-healing state. Data only — the GUI screens render it.
    telemetry = getattr(ctx, "telemetry", None)
    telemetry_summary = (
        telemetry.summary()
        if telemetry is not None
        else {"device_id": None, "diagnostic_mode": False, "sink_attached": False,
              "sink_inert": True, "emitted": 0, "info_local_only": 0,
              "debug_dropped": 0, "queue": {"queued": 0}}
    )
    self_healing = getattr(ctx, "self_healing", None)
    if self_healing is not None:
        self_healing_summary = self_healing.summary()
    else:
        # No registry wired (e.g. a bare context in tests): report all
        # 15 scenarios as never-observed, never claim a recovery.
        from ..reliability.self_healing import SCENARIOS

        self_healing_summary = {
            scenario.value: {
                "scenario": scenario.value, "status": "ok", "occurrences": 0,
                "last_occurrence": None, "last_action": None,
                "last_error_code": None, "detail": None,
            }
            for scenario in SCENARIOS
        }
    state = ctx.driver.connection_state_dict()
    return {
        "ok": True,
        "data": {
            "generated_at": datetime_now_iso(),
            "bridge": get_bridge_health(ctx, None),
            "bridge_status": get_bridge_status(ctx, None),
            "mt5": get_mt5_connection_status(ctx, None),
            "terminals": terminals,
            "network": network,
            "websocket": channel,
            "enrolment": ctx.enrolment_status(),
            "sync": sync,
            "telemetry": telemetry_summary,
            "self_healing": self_healing_summary,
            "last_errors": ctx.logs.tail(20, "ERROR"),
            "driver_last_error": state.get("last_error"),
            "diagnostic": diagnostic,
        },
    }
