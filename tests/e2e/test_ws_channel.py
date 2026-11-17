"""E2E — outbound WebSocket server channel against a mock NSE server.

Runs its own EventBus + pollers inside the test loop so cross-loop queues
never mix, then asserts: hello handshake -> command roundtrip -> tick stream.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from nse_bridge.realtime.bus import EventBus, start_streaming, stop_streaming
from nse_bridge.realtime.channel import ServerChannel


@pytest.mark.asyncio
async def test_server_channel_full_roundtrip(ctx, client):
    received: list[dict] = []
    tick_seen = asyncio.Event()
    hello_seen = asyncio.Event()
    result_seen = asyncio.Event()

    async def handler(ws):  # type: ignore[no-untyped-def]
        # 1) hello handshake
        hello = json.loads(await ws.recv())
        received.append(hello)
        assert hello["type"] == "hello"
        assert hello["device_id"].startswith("nse-")
        assert hello["mt5"]["connected"] is True
        assert hello["capabilities"]["commands_count"] >= 81
        hello_seen.set()

        # 2) command roundtrip
        await ws.send(json.dumps({"type": "command", "id": "t1", "command": "get_account_balance", "params": {}}))
        result = json.loads(await ws.recv())
        received.append(result)
        assert result["type"] == "command_result" and result["id"] == "t1"
        assert result["ok"] is True
        assert result["data"]["balance"] > 0
        assert result["latency_ms"] >= 0
        result_seen.set()

        # 3) subscribe + receive a live tick
        await ws.send(json.dumps({"type": "subscribe", "symbol": "EURUSD", "interval_ms": 150}))
        deadline = asyncio.get_event_loop().time() + 15
        while not tick_seen.is_set() and asyncio.get_event_loop().time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            received.append(msg)
            if msg.get("type") == "tick" and msg["data"]["symbol"] == "EURUSD":
                assert msg["data"]["bid"] > 0
                tick_seen.set()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    ctx.settings.server_url = f"ws://127.0.0.1:{port}"
    ctx.settings.server_token = "test-token"
    bus = EventBus()
    pollers, stop_event = start_streaming(ctx, bus)
    channel = ServerChannel(ctx, bus)
    channel.start()

    try:
        await asyncio.wait_for(hello_seen.wait(), timeout=15)
        await asyncio.wait_for(result_seen.wait(), timeout=15)
        await asyncio.wait_for(tick_seen.wait(), timeout=15)
        assert ctx.server_channel_state["connected"] is True
    finally:
        await channel.stop()
        stop_streaming(pollers, stop_event)
        server.close()
        await server.wait_closed()
        ctx.settings.server_url = ""
        ctx.settings.server_token = ""


@pytest.mark.asyncio
async def test_server_channel_full_state_sync(ctx, client):
    got_state = asyncio.Event()

    async def handler(ws):  # type: ignore[no-untyped-def]
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "request_full_state"}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") == "full_state":
                data = msg["data"]
                assert data["device_id"].startswith("nse-")
                assert data["mt5"]["connected"] is True
                assert data["account"]["login"] > 0
                assert "positions" in data and "orders" in data
                assert data["positions"]["count"] >= 0
                got_state.set()
                return

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ctx.settings.server_url = f"ws://127.0.0.1:{port}"
    ctx.settings.server_token = "test-token"
    channel = ServerChannel(ctx, EventBus())
    channel.start()
    try:
        await asyncio.wait_for(got_state.wait(), timeout=15)
    finally:
        await channel.stop()
        server.close()
        await server.wait_closed()
        ctx.settings.server_url = ""
        ctx.settings.server_token = ""


@pytest.mark.asyncio
async def test_server_channel_unknown_command_response(ctx, client):
    got_error = asyncio.Event()

    async def handler(ws):  # type: ignore[no-untyped-def]
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "command", "id": "x", "command": "no_such_command", "params": {}}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") == "command_result":
                assert msg["ok"] is False
                assert msg["error"]["code"] == "UNKNOWN_COMMAND"
                got_error.set()
                return

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ctx.settings.server_url = f"ws://127.0.0.1:{port}"
    ctx.settings.server_token = "test-token"
    bus = EventBus()
    channel = ServerChannel(ctx, bus)
    channel.start()
    try:
        await asyncio.wait_for(got_error.wait(), timeout=15)
    finally:
        await channel.stop()
        server.close()
        await server.wait_closed()
        ctx.settings.server_url = ""
        ctx.settings.server_token = ""
