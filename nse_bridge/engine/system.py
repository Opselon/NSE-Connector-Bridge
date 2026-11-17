"""Bridge-level commands (MCP/server-facing identity)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .. import BRIDGE_VERSION, PROTOCOL_VERSION
from .common import BridgeContext
from .registry import command


class BridgeStatusResponse(BaseModel):
    bridge_name: str
    bridge_version: str
    protocol_version: int
    uptime_seconds: float
    mt5_state: str
    mt5_connected: bool
    device_id: str
    commands_registered: int
    subscriptions_active: int
    server_channel: dict[str, Any]
    pid: int


class McpServerInfoResponse(BaseModel):
    server_name: str
    server_version: str
    protocol: str
    transport: list[str]
    tools_count: int
    categories: list[str]
    docs_url: str
    health_url: str


@command(
    "get_bridge_status",
    number=1,
    category="system",
    title="Get Bridge Status",
    description="Overall bridge status: version, uptime, MT5 state, server channel, command count.",
    response_model=BridgeStatusResponse,
)
def get_bridge_status(ctx: BridgeContext, params: None) -> dict[str, Any]:
    import os

    subs = ctx.subscriptions.snapshot()
    return {
        "bridge_name": ctx.settings.app_name,
        "bridge_version": BRIDGE_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "uptime_seconds": round(ctx.uptime_s, 1),
        "mt5_state": ctx.driver.connection_state_dict()["state"],
        "mt5_connected": ctx.driver.connected,
        "device_id": ctx.get_or_create_device_id(),
        "commands_registered": len(ctx_registered(ctx)),
        "subscriptions_active": len(subs),
        "server_channel": ctx.server_channel_state,
        "pid": os.getpid(),
    }


def ctx_registered(ctx: BridgeContext) -> list:
    from .registry import REGISTRY

    return REGISTRY


@command(
    "get_mcp_server_info",
    number=9,
    category="system",
    title="Get MCP Server Info",
    description="Bridge identity for MCP/platform integration: protocol, transports, tool count, docs URLs.",
    response_model=McpServerInfoResponse,
)
def get_mcp_server_info(ctx: BridgeContext, params: None) -> dict[str, Any]:
    from .registry import CATEGORY_ORDER, REGISTRY

    host = ctx.settings.host
    port = ctx.settings.port
    base = f"http://{host}:{port}"
    return {
        "server_name": ctx.settings.app_name,
        "server_version": BRIDGE_VERSION,
        "protocol": f"nse-mt5-bridge/{PROTOCOL_VERSION}",
        "transport": ["rest", "websocket"],
        "tools_count": len(REGISTRY),
        "categories": list(CATEGORY_ORDER),
        "docs_url": f"{base}/docs",
        "health_url": f"{base}/api/v1/get_bridge_health",
    }
