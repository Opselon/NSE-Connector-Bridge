"""Category 10 — diagnostics & monitoring (82-90)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import BRIDGE_VERSION, PROTOCOL_VERSION
from ..mt5.types import f as _f
from .common import BridgeContext
from .registry import command


class LastErrorResponse(BaseModel):
    last_error: dict[str, Any] | None = None
    mt5_last_error: dict[str, Any] | None = None
    connection_state: str


class LogsRequest(BaseModel):
    limit: int = Field(200, ge=1, le=2000)
    level: str | None = Field(None, description="Filter: DEBUG/INFO/WARNING/ERROR")


class LogsResponse(BaseModel):
    count: int
    entries: list[dict[str, Any]]


class HealthResponse(BaseModel):
    healthy: bool
    checks: dict[str, Any]
    uptime_seconds: float
    mt5_state: str
    subscriptions: int


class LatencyResponse(BaseModel):
    overall_avg_ms: float
    overall_max_ms: float
    slowest_op: str | None = None
    operations: dict[str, Any]


class ConnectionMetricsResponse(BaseModel):
    connection: dict[str, Any]
    operations: dict[str, Any]
    recent_calls: list[dict[str, Any]]


class TradeMetricsResponse(BaseModel):
    metrics: dict[str, Any]


class CapabilitiesResponse(BaseModel):
    bridge_version: str
    protocol_version: int
    commands_count: int
    categories: list[str]
    features: dict[str, Any]
    commands: list[dict[str, Any]]


class McpToolsResponse(BaseModel):
    tools_count: int
    tools: list[dict[str, Any]]


class DiagnosticReportResponse(BaseModel):
    generated_at: str
    bridge: dict[str, Any]
    connection: dict[str, Any]
    terminal: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    metrics: dict[str, Any]
    trade_metrics: dict[str, Any]
    recent_calls: list[dict[str, Any]]
    recent_logs: list[dict[str, Any]]
    subscriptions: list[dict[str, Any]]


@command(
    "get_mt5_last_error",
    number=82,
    category="diagnostics",
    title="Get MT5 Last Error",
    description="Last recorded MT5 error (code + message) and the live driver last_error.",
    response_model=LastErrorResponse,
)
def get_mt5_last_error(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ctx.driver.ensure_connected()

    def job(mt5: Any):
        err = mt5.last_error()
        return {"code": err[0], "message": str(err[1])} if err else None

    live = ctx.driver.run(job, op="last_error")
    state = ctx.driver.connection_state_dict()
    return {
        "last_error": state.get("last_error"),
        "mt5_last_error": live,
        "connection_state": state["state"],
    }


@command(
    "get_bridge_logs",
    number=83,
    category="diagnostics",
    title="Get Bridge Logs",
    description="Recent bridge log entries (ring buffer) with optional level filter. "
    "Full files live in logs/bridge.log (rotated) and logs/bridge.jsonl (structured).",
    request_model=LogsRequest,
    response_model=LogsResponse,
)
def get_bridge_logs(ctx: BridgeContext, params: LogsRequest | None) -> dict[str, Any]:
    p = params or LogsRequest()
    entries = ctx.logs.tail(p.limit, p.level)
    return {"count": len(entries), "entries": entries}


@command(
    "get_bridge_health",
    number=84,
    category="diagnostics",
    title="Get Bridge Health",
    description="Health check summary: MT5 liveness, error rates, server channel, subscriptions.",
    response_model=HealthResponse,
)
def get_bridge_health(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ops = ctx.driver.metrics_dict()
    total_calls = sum(o["calls"] for o in ops.values())
    total_fail = sum(o["failures"] for o in ops.values())
    error_rate = total_fail / total_calls if total_calls else 0.0
    terminal = ctx.driver.refresh_terminal() or {}
    mt5_ok = ctx.driver.connected and bool(terminal.get("connected", False))
    # B2: the server link is a first-class health signal, not a single
    # boolean. Enrolment (terminal states + the server's verbatim
    # reason), the WS state machine (auth vs transport vs enrolment
    # failure + next attempt), the last heartbeat sent/received on
    # either transport, and whether data sending is gated — with the
    # bounded queue depths — all land here so the dashboard and the
    # diagnostics summary can show the truth instead of "RETRY / —".
    enrolment = ctx.enrolment_status()
    channel_state = ctx.server_channel_state
    enrolment_state = str(enrolment.get("state") or "not_configured")
    send_gated = enrolment_state != "registered"
    if send_gated:
        gate_reason = enrolment.get("last_error_message") or enrolment.get("last_error") or enrolment_state
        send_gated_reason = f"enrolment {enrolment_state}: {gate_reason}"
    else:
        send_gated_reason = None
    sync_engine = getattr(ctx, "sync_engine", None)
    try:
        sync_pending = sync_engine.pending_count() if sync_engine is not None else None
    except Exception:  # noqa: BLE001 — a failing probe degrades to None, never fails health
        sync_pending = None
    checks = {
        "mt5_connected": mt5_ok,
        "total_mt5_calls": total_calls,
        "total_mt5_failures": total_fail,
        "mt5_error_rate": round(error_rate, 4),
        "server_channel_connected": channel_state.get("connected", False),
        "enrolment_state": enrolment_state,
        "enrolment_terminal": bool(enrolment.get("terminal", enrolment_state in ("failed_auth", "failed"))),
        "enrolment_last_error": enrolment.get("last_error"),
        "enrolment_last_error_message": enrolment.get("last_error_message"),
        "enrolment_attempts": enrolment.get("attempts"),
        "enrolment_next_retry_at": enrolment.get("next_retry_at"),
        "ws_state": channel_state.get("ws_state"),
        "ws_last_error_kind": channel_state.get("last_error_kind"),
        "ws_last_error": channel_state.get("last_error"),
        "ws_last_error_detail": channel_state.get("last_error_detail"),
        "ws_next_attempt_at": channel_state.get("next_attempt_at"),
        "ws_auth_failures": channel_state.get("ws_auth_failures"),
        "last_heartbeat_sent_at": channel_state.get("last_heartbeat_sent_at"),
        "last_heartbeat_received_at": channel_state.get("last_heartbeat_received_at"),
        "heartbeat_via": channel_state.get("heartbeat_via"),
        "send_gated": send_gated,
        "send_gated_reason": send_gated_reason,
        "outbound_queued": channel_state.get("outbound_queued"),
        "pending_results": channel_state.get("pending_results"),
        "sync_pending_batches": sync_pending,
    }
    return {
        "healthy": mt5_ok,
        "checks": checks,
        "uptime_seconds": round(ctx.uptime_s, 1),
        "mt5_state": ctx.driver.connection_state_dict()["state"],
        "subscriptions": len(ctx.subscriptions.snapshot()),
    }


@command(
    "get_bridge_latency",
    number=85,
    category="diagnostics",
    title="Get Bridge Latency",
    description="Per-operation latency statistics (avg/max/last) for every MT5 call the bridge performs.",
    response_model=LatencyResponse,
)
def get_bridge_latency(ctx: BridgeContext, params: None) -> dict[str, Any]:
    ops = ctx.driver.metrics_dict()
    if not ops:
        return {"overall_avg_ms": 0.0, "overall_max_ms": 0.0, "slowest_op": None, "operations": {}}
    total_ms = sum(o["avg_ms"] * o["calls"] for o in ops.values())
    total_calls = sum(o["calls"] for o in ops.values())
    slowest = max(ops.items(), key=lambda kv: kv[1]["max_ms"])
    return {
        "overall_avg_ms": round(total_ms / total_calls, 3) if total_calls else 0.0,
        "overall_max_ms": round(max(o["max_ms"] for o in ops.values()), 3),
        "slowest_op": slowest[0],
        "operations": ops,
    }


@command(
    "get_mt5_connection_metrics",
    number=86,
    category="diagnostics",
    title="Get MT5 Connection Metrics",
    description="Connection state + full per-op MT5 call metrics + recent call ring (50 ops).",
    response_model=ConnectionMetricsResponse,
)
def get_mt5_connection_metrics(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return {
        "connection": ctx.driver.connection_state_dict(),
        "operations": ctx.driver.metrics_dict(),
        "recent_calls": ctx.driver.ring_dict(50),
    }


@command(
    "get_trade_execution_metrics",
    number=87,
    category="diagnostics",
    title="Get Trade Execution Metrics",
    description="Order dispatch telemetry: sent/done/rejected/unknown, latencies, retcode histogram, recent orders.",
    response_model=TradeMetricsResponse,
)
def get_trade_execution_metrics(ctx: BridgeContext, params: None) -> dict[str, Any]:
    return {"metrics": ctx.trade_metrics.to_dict()}


@command(
    "get_bridge_capabilities",
    number=88,
    category="diagnostics",
    title="Get Bridge Capabilities",
    description="Full capability manifest: every registered command, versions, supported features.",
    response_model=CapabilitiesResponse,
)
def get_bridge_capabilities(ctx: BridgeContext, params: None) -> dict[str, Any]:
    from .registry import all_commands

    cmds = all_commands()
    return {
        "bridge_version": BRIDGE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "commands_count": len(cmds),
        "categories": sorted({c.category for c in cmds}),
        "features": {
            "market_orders": True,
            "pending_orders": True,
            "position_management": True,
            "partial_close": True,
            "reverse_position": True,
            "sl_tp_modification": True,
            "history_access": True,
            "risk_calculations": True,
            "market_depth": True,
            "tick_streaming": True,
            "websocket_server_channel": True,
            "swagger_docs": True,
            "gui_dashboard": True,
            "retcode_diagnostics": True,
            "ambiguous_fill_recovery": True,
        },
        "commands": [{"number": c.number, "name": c.name, "category": c.category, "mutates": c.mutates} for c in cmds],
    }


@command(
    "get_mcp_available_tools",
    number=89,
    category="diagnostics",
    title="Get MCP Available Tools",
    description="Every command as an MCP tool descriptor with its JSON-schema input (server-side "
    "MCP tool registration source).",
    response_model=McpToolsResponse,
)
def get_mcp_available_tools(ctx: BridgeContext, params: None) -> dict[str, Any]:
    from .registry import all_commands

    tools = []
    for c in all_commands():
        tools.append(
            {
                "name": c.name,
                "description": c.description,
                "category": c.category,
                "mutates": c.mutates,
                "input_schema": c.request_model.model_json_schema() if c.request_model else {"type": "object"},
            }
        )
    return {"tools_count": len(tools), "tools": tools}


@command(
    "get_mt5_diagnostic_report",
    number=90,
    category="diagnostics",
    title="Get MT5 Diagnostic Report",
    description="Comprehensive one-shot troubleshooting dump: bridge, connection, terminal, account, "
    "metrics, recent calls, recent logs, subscriptions.",
    response_model=object,
)
def get_mt5_diagnostic_report(ctx: BridgeContext, params: None) -> dict[str, Any]:
    from datetime import UTC, datetime

    from .common import account_info_model

    terminal = None
    account = None
    if ctx.driver.connected:
        try:

            def job(mt5: Any):
                return mt5.terminal_info()

            raw = ctx.driver.run(job, op="terminal_info")
            terminal = {
                "name": _f(raw, "name"),
                "company": _f(raw, "company"),
                "connected": bool(_f(raw, "connected", False)),
                "trade_allowed": bool(_f(raw, "trade_allowed", False)),
                "path": _f(raw, "path"),
                "data_path": _f(raw, "data_path"),
            }
        except Exception as exc:  # noqa: BLE001 — a failing probe degrades to {"error": ...}, never fails the summary
            terminal = {"error": str(exc)}
        try:
            account = account_info_model(ctx).model_dump()
        except Exception as exc:  # noqa: BLE001 — a failing probe degrades to {"error": ...}, never fails the summary
            account = {"error": str(exc)}
    subs = [
        {"symbol": s.symbol, "interval_ms": s.interval_ms, "sent": s.sent, "created_at": s.created_at}
        for s in ctx.subscriptions.snapshot()
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "bridge": {
            "version": BRIDGE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "uptime_seconds": round(ctx.uptime_s, 1),
            "device_id": ctx.get_or_create_device_id(),
            "server_channel": ctx.server_channel_state,
            "pid": __import__("os").getpid(),
        },
        "connection": ctx.driver.connection_state_dict(),
        "terminal": terminal,
        "account": account,
        "metrics": ctx.driver.metrics_dict(),
        "trade_metrics": ctx.trade_metrics.to_dict(),
        "recent_calls": ctx.driver.ring_dict(30),
        "recent_logs": ctx.logs.tail(50),
        "subscriptions": subs,
    }
