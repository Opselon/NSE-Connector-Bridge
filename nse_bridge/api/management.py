"""Management endpoints: settings, pre-flight connection checks, channel control.

These sit on top of the 90-command surface — they operate the BRIDGE itself
(configuration, diagnostics, server-channel lifecycle), not the broker.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, Request
from loguru import logger
from pydantic import BaseModel, Field

from ..config import persist_settings
from ..engine.common import BridgeContext, datetime_now_iso
from ..errors import BridgeError, ErrorCode
from ..retcodes import translate_retcode

router = APIRouter(prefix="/api/v1", tags=["system"])


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
class SettingsUpdate(BaseModel):
    """Partial bridge configuration update. Omitted fields stay unchanged."""

    server_url: str | None = Field(None, description="NSE server channel URL (wss://...; empty = disabled)")
    server_token: str | None = Field(None, description="API key issued on the NSE Platform site")
    local_api_key: str | None = Field(None, description="Local X-API-Key gate (empty = open on localhost)")
    magic: int | None = Field(None, ge=0, le=2**31 - 1)
    bot_symbol: str | None = Field(None, min_length=1, max_length=40)
    default_timeframe: str | None = Field(None, min_length=2, max_length=4)
    broker_utc_offset_minutes: int | None = Field(None, ge=-720, le=720)
    strict_magic_guard: bool | None = None
    stream_account_interval_ms: int | None = Field(None, ge=200, le=60000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None
    gui_mode: Literal["auto", "webview", "browser", "none"] | None = None
    mt5_terminal_path: str | None = None


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
            "device_id": ctx.get_or_create_device_id(),
            "config_file": str(s.config_file),
            "server_channel": ctx.server_channel_state,
        },
    }


@router.put("/settings", summary="Update bridge settings",
    description="Partial update. Persisted to config.json. Server-channel fields apply immediately "
    "(the outbound channel restarts when URL/token change).")
async def update_settings(request: Request, update: SettingsUpdate) -> dict[str, Any]:
    ctx: BridgeContext = request.app.state.ctx
    s = ctx.settings
    changed: list[str] = []
    channel_needs_restart = False
    for key, value in update.model_dump(exclude_none=True).items():
        if key not in EDITABLE_KEYS or getattr(s, key) == value:
            continue
        setattr(s, key, value)
        changed.append(key)
        if key in ("server_url", "server_token"):
            channel_needs_restart = True
    if changed:
        try:
            persist_settings(s)
        except Exception as exc:
            logger.warning("[SETTINGS] persist failed: {}", exc)
        logger.info("[SETTINGS] updated: {}", ", ".join(changed))

    restarted = False
    if channel_needs_restart:
        channel = getattr(request.app.state, "channel", None)
        if channel is not None:
            await channel.stop()
            channel.start()
            restarted = True
    return {
        "ok": True,
        "data": {
            "changed": changed,
            "server_channel_restarted": restarted,
            "server_channel": ctx.server_channel_state,
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
    "Enable the 'Algo Trading' button in the MT5 toolbar (shortcut Ctrl+E) so it turns green. "
    "Also check Tools → Options → Expert Advisors → 'Allow algorithmic trading'. "
    "No API can toggle this — it is a terminal security control."
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

    # 5. bot symbol availability + tick freshness
    if ctx.driver.connected:
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
    except Exception as exc:
        return False, str(exc)[:200]


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
