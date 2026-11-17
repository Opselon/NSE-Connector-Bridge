"""Category 1 — connection & terminal commands (1-9)."""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic import BaseModel

from ..errors import BridgeError, ErrorCode
from ..mt5.types import TerminalInfoModel, f as _f
from .common import BridgeContext, datetime_now_iso
from .registry import command


class ConnectRequest(BaseModel):
    """Optional runtime overrides — config file values remain the defaults."""

    login: int | None = None
    password: str | None = None
    server: str | None = None
    terminal_path: str | None = None
    timeout_ms: int | None = None
    retries: int | None = None


class ConnectResponse(BaseModel):
    ok: bool
    state: str
    pkg_version: str | None = None
    terminal_version: str | None = None
    login: int | None = None
    terminal_name: str | None = None
    error: str | None = None
    last_error: list[Any] | None = None


class TerminalInfoResponse(BaseModel):
    terminal: TerminalInfoModel
    state: str


class TerminalVersionResponse(BaseModel):
    api_version: int | None = None
    build: int | None = None
    build_date: str | None = None
    package_version: str | None = None
    terminal_name: str | None = None


class ServerInfoResponse(BaseModel):
    server: str | None = None
    company: str | None = None
    terminal_name: str | None = None
    currency: str | None = None
    login: int | None = None
    broker_utc_offset_minutes: int
    tick_latency_ms: float | None = None


class DisconnectResponse(BaseModel):
    ok: bool
    state: str


@command(
    "get_mt5_connection_status",
    number=2,
    category="connection",
    title="Get MT5 Connection Status",
    description="Real MT5 connection state with a live terminal liveness probe (never derived from config).",
    response_model=TerminalInfoResponse,
)
def get_mt5_connection_status(ctx: BridgeContext, params: None) -> dict[str, Any]:
    state = ctx.driver.connection_state_dict()
    terminal_raw = ctx.driver.refresh_terminal() or {}
    return {
        "terminal": {
            "name": terminal_raw.get("name"),
            "connected": terminal_raw.get("connected", False),
            "trade_allowed": terminal_raw.get("trade_allowed"),
            "login": terminal_raw.get("login"),
        },
        "state": state["state"],
        "pkg_version": state.get("pkg_version"),
        "login": state.get("login"),
    }


@command(
    "connect_mt5",
    number=3,
    category="connection",
    title="Connect to MT5",
    description="Attach to the local MT5 terminal via the official C++ IPC driver. "
    "Resilient: bounded retries with backoff, stale-attach release, account-mismatch fail-safe.",
    request_model=ConnectRequest,
    response_model=ConnectResponse,
)
def connect_mt5(ctx: BridgeContext, params: ConnectRequest | None) -> dict[str, Any]:
    s = ctx.settings
    p = params or ConnectRequest()
    changed = False
    if p.login is not None:
        s.mt5_login = p.login
        changed = True
    if p.password is not None:
        s.mt5_password = p.password
        changed = True
    if p.server is not None:
        s.mt5_server = p.server
        changed = True
    if p.terminal_path is not None:
        s.mt5_terminal_path = p.terminal_path
        changed = True
    if p.timeout_ms is not None:
        s.mt5_timeout_ms = p.timeout_ms
        changed = True
    if p.retries is not None:
        s.mt5_retries = p.retries
        changed = True
    if changed:
        logger.info("[MT5_CONNECT] runtime overrides applied (login={})", p.login)
    summary = ctx.driver.connect()
    state = ctx.driver.connection_state_dict()
    return {
        "ok": summary.get("ok", False),
        "state": state["state"],
        "pkg_version": summary.get("pkg_version"),
        "terminal_version": summary.get("terminal_version"),
        "login": summary.get("login"),
        "terminal_name": summary.get("terminal_name"),
        "error": summary.get("error"),
        "last_error": summary.get("last_error"),
    }


@command(
    "disconnect_mt5",
    number=4,
    category="connection",
    title="Disconnect from MT5",
    description="Release the MT5 IPC handle (always attempts shutdown — safe when already disconnected).",
    response_model=DisconnectResponse,
)
def disconnect_mt5(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return ctx.driver.disconnect()


@command(
    "reconnect_mt5",
    number=5,
    category="connection",
    title="Reconnect to MT5",
    description="Full disconnect + resilient reconnect cycle.",
    response_model=ConnectResponse,
)
def reconnect_mt5(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ctx.driver.disconnect()
    summary = ctx.driver.connect()
    state = ctx.driver.connection_state_dict()
    return {
        "ok": summary.get("ok", False),
        "state": state["state"],
        "pkg_version": summary.get("pkg_version"),
        "terminal_version": summary.get("terminal_version"),
        "login": summary.get("login"),
        "terminal_name": summary.get("terminal_name"),
        "error": summary.get("error"),
        "last_error": summary.get("last_error"),
    }


@command(
    "get_mt5_terminal_info",
    number=6,
    category="connection",
    title="Get MT5 Terminal Info",
    description="Terminal process information: name, company, paths, connected, trade permissions, maxbars.",
    response_model=TerminalInfoResponse,
)
def get_mt5_terminal_info(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.terminal_info()

    raw = ctx.driver.run(job, op="terminal_info")
    if raw is None:
        raise BridgeError(ErrorCode.MT5_CALL_FAILED, "terminal_info() returned None")
    model = TerminalInfoModel(
        name=_f(raw, "name"),
        company=_f(raw, "company"),
        path=_f(raw, "path"),
        data_path=_f(raw, "data_path"),
        connected=bool(_f(raw, "connected", False)),
        trade_allowed=bool(_f(raw, "trade_allowed", False)),
        trade_expert=_f(raw, "trade_expert"),
        dlls_allowed=_f(raw, "dlls_allowed"),
        login=_f(raw, "login"),
        maxbars=_f(raw, "maxbars"),
        portable=_f(raw, "portable"),
    )
    return {"terminal": model.model_dump(), "state": ctx.driver.connection_state_dict()["state"]}


@command(
    "get_mt5_terminal_version",
    number=7,
    category="connection",
    title="Get MT5 Terminal Version",
    description="MT5 driver version, terminal build and build date.",
    response_model=TerminalVersionResponse,
)
def get_mt5_terminal_version(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        return mt5.version()

    vers = ctx.driver.run(job, op="version")
    state = ctx.driver.connection_state_dict()
    api_version = build = None
    date = None
    if vers:
        api_version = int(vers[0])
        build = int(vers[1]) if len(vers) > 1 else None
        date = str(vers[2]) if len(vers) > 2 else None
    return {
        "api_version": api_version,
        "build": build,
        "build_date": date,
        "package_version": state.get("pkg_version"),
        "terminal_name": state.get("terminal_version"),
    }


@command(
    "get_mt5_server_info",
    number=8,
    category="connection",
    title="Get MT5 Server Info",
    description="Broker server identity: server name, company, currency, configured UTC offset, tick latency probe.",
    response_model=ServerInfoResponse,
)
def get_mt5_server_info(ctx: BridgeContext, params: None) -> dict[str, Any]:
    from .common import account_info_model

    ctx.driver.ensure_connected()
    acc = account_info_model(ctx)

    def job(mt5: Any):
        return mt5.terminal_info()

    raw = ctx.driver.run(job, op="terminal_info")
    company = _f(raw, "company") if raw else None
    term_name = _f(raw, "name") if raw else None

    # latency probe: one symbol_info_tick roundtrip on the bot symbol
    t0 = __import__("time").perf_counter()

    def tick_job(mt5: Any):
        return mt5.symbol_info_tick(ctx.settings.bot_symbol)

    tick = ctx.driver.run(tick_job, op="symbol_info_tick", context={"symbol": ctx.settings.bot_symbol})
    latency = (__import__("time").perf_counter() - t0) * 1000.0 if tick is not None else None
    return {
        "server": acc.server,
        "company": company,
        "terminal_name": term_name,
        "currency": acc.currency,
        "login": acc.login,
        "broker_utc_offset_minutes": ctx.settings.broker_utc_offset_minutes,
        "tick_latency_ms": round(latency, 2) if latency is not None else None,
        "probed_at": datetime_now_iso(),
    }
