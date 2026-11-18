"""E2E — outbound WebSocket server channel against a fake NSE server, real MT5 terminal.

Contract protocol (CONTRACT v1 §5/§6/§17.1): there is NO ``hello`` frame
(D-WS-2); command results arrive as ``action.result`` envelopes (§6.5);
chart data arrives as ``chart.data`` / ``chart.tick`` (§17.1); a state
refresh is a ``sync_request`` that re-sends account/positions/history
over REST (§5.2).

Rewritten 2026-11-18 (real-MT5 defect 3): the previous version expected
the pre-Phase-2 protocol (hello → command_result → subscribe/tick),
which the current build never sends. The WS command path itself works
— proven live 2026-11-18 (real close_position via WS, BUY 0.01 ticket
152923734203 closed, action.result executed).

Demo-safe: 0.01 lots on XAUUSD; every test cleans up after itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import websockets

# The CONTRACT v1 mock server lives in scripts/ (importable, in-process);
# the sync test below uses it as the transport for the REST leg.
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from mock_server import MockServer

from nse_bridge.engine.common import BridgeContext
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.uuid7 import new_uuid7_str
from tests.conftest import call

pytestmark = pytest.mark.real_mt5

SYMBOL = "XAUUSD"


def _test_context(ctx: Any, ws_url: str, *, server_base_url: str | None = None) -> BridgeContext:
    """A context sharing the session settings + driver but WITHOUT
    enrolment, so the test channel is not gated on REGISTERED (§5.1).

    The session ctx carries the app's EnrolmentManager, which can never
    register against the undeployed production server — a channel built
    on it would wait forever in ``wait_until_registered``. A fresh
    context has no enrolment attribute, so the channel connects
    ungated, exactly like the mock-server integration tests. Settings
    are copied, never mutated: the session ctx is shared across the
    whole e2e session.
    """
    settings = ctx.settings.model_copy(
        update={
            "server_url": ws_url,
            "server_token": "test-token",
            "heartbeat_interval_s": 1.0,  # fast heartbeat for the test
            # Test context: http:// mock URLs are only valid in a
            # dev/test environment (validate_server_base_url).
            "environment": "test",
            **({"server_base_url": server_base_url} if server_base_url else {}),
        }
    )
    test_ctx = BridgeContext(settings=settings, driver=ctx.driver)
    test_ctx.get_or_create_device_id()
    return test_ctx


def _command_frame(
    command_id: str, account_id: str, device_id: str, operation_type: str, ticket: Any
) -> dict[str, Any]:
    """One §6.2 command envelope."""
    now = datetime.now(UTC)
    return {
        "type": "command",
        "command_id": command_id,
        "account_id": str(account_id),
        "device_id": device_id,
        "operation": {"type": operation_type, "ticket": str(ticket)},
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": command_id,
    }


def _open_positions(client: Any) -> list[dict[str, Any]]:
    return call(client, "get_trading_open_positions", {"symbol": SYMBOL})["positions"]


def _cleanup(client: Any) -> None:
    try:
        call(client, "trade_close_all_positions", {"symbol": SYMBOL})
    except AssertionError:
        pass


@pytest.mark.asyncio
async def test_server_channel_full_roundtrip(ctx, client):
    # 0) Open a real 0.01 BUY on XAUUSD via the local API; the ticket
    # is the position that appears afterwards (set difference, so
    # pre-existing positions can never be mistaken for it).
    before = {p["ticket"] for p in _open_positions(client)}
    buy = call(client, "trade_send_buy_order", {"symbol": SYMBOL, "volume": 0.01, "comment": "E2E_WS"})
    assert buy["status"] == "done" and buy["retcode"] == 10009
    after = {p["ticket"] for p in _open_positions(client)}
    new_tickets = after - before
    assert len(new_tickets) == 1, f"expected exactly one new position, got {new_tickets}"
    ticket = next(iter(new_tickets))

    login = ctx.driver.connection_state_dict()["login"]
    assert login, "no MT5 account bound — cannot address the command"
    device_id = ctx.get_or_create_device_id()
    command_id = new_uuid7_str()
    result_seen = asyncio.Event()
    data_seen = asyncio.Event()
    tick_seen = asyncio.Event()
    results: list[dict[str, Any]] = []
    data_frames: list[dict[str, Any]] = []
    tick_frames: list[dict[str, Any]] = []

    async def handler(ws):  # type: ignore[no-untyped-def]
        # No hello is ever sent by the bridge (D-WS-2): the first
        # frames from the bridge are heartbeats / manifest.update.
        # Heartbeats and manifest frames may interleave with the
        # frames below — the loops below filter by type/event_type and
        # answer heartbeats, like a real server. The handler RECORDS
        # the frames it cares about and never asserts: an assert here
        # would kill the connection (and the bridge would reconnect
        # and re-send the close), masking the real failure as a
        # timeout. The test body asserts on the recorded frames.
        # 1) command roundtrip — close the real position via WS.
        await ws.send(json.dumps(_command_frame(command_id, login, device_id, "close_position", ticket)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not result_seen.is_set() and loop.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if msg.get("type") == "heartbeat":
                await ws.send(json.dumps({"type": "heartbeat"}))
            elif msg.get("event_type") == "action.result" and msg.get("correlation_id") == command_id:
                results.append(msg)
                result_seen.set()

        # 2) contract chart subscription behaviour (§17.1): the server
        # sends chart.subscribe; the bridge answers chart.data deltas
        # plus chart.tick when include_ticks is set. There is no
        # subscribe/tick frame pair in v1.
        sub_id = new_uuid7_str()
        await ws.send(
            json.dumps(
                {
                    "type": "chart.subscribe",
                    "subscription_id": sub_id,
                    "symbol": "EURUSD",
                    "timeframe": "M1",
                    "poll_interval_s": 5,
                    "include_ticks": True,
                }
            )
        )
        deadline = loop.time() + 30
        while (not data_seen.is_set() or not tick_seen.is_set()) and loop.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if msg.get("type") == "heartbeat":
                await ws.send(json.dumps({"type": "heartbeat"}))
            elif msg.get("type") == "chart.data" and msg.get("subscription_id") == sub_id:
                data_frames.append(msg)
                data_seen.set()
            elif msg.get("type") == "chart.tick" and msg.get("subscription_id") == sub_id:
                tick_frames.append(msg)
                tick_seen.set()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    test_ctx = _test_context(ctx, f"ws://127.0.0.1:{port}")
    channel = ServerChannel(test_ctx, EventBus())
    channel.start()
    try:
        await asyncio.wait_for(result_seen.wait(), timeout=25)
        await asyncio.wait_for(data_seen.wait(), timeout=35)
        await asyncio.wait_for(tick_seen.wait(), timeout=35)
        assert results and results[0]["status"] == "executed", f"close_position not executed: {results}"
        assert results[0]["protocol_version"] == "1.0"
        assert data_frames and data_frames[0]["status"] == "ok", f"chart.data error: {data_frames}"
        assert data_frames[0]["symbol"] == "EURUSD" and len(data_frames[0]["candles"]) > 0
        assert tick_frames and tick_frames[0]["symbol"] == "EURUSD"
        assert tick_frames[0]["ticks"][0]["bid"] > 0
        assert test_ctx.server_channel_state["connected"] is True
    finally:
        await channel.stop()
        server.close()
        await server.wait_closed()
        # The WS close must have closed the position; never leave a
        # live position behind even if the assertion above failed.
        _cleanup(client)
    assert not any(p["ticket"] == ticket for p in _open_positions(client))


@pytest.mark.asyncio
async def test_server_channel_sync_request_resends_state(ctx, client):
    # §5.2: a sync_request makes the bridge re-send account, then
    # positions, then history over REST. The mock server is the
    # transport (the production endpoint is undeployed); the DATA is
    # real — account/positions/history from the live terminal. The mock
    # auto-sends sync_request 0.2 s after connect, and acks every batch.
    mock = MockServer(token="test-token")
    mock.start()
    try:
        test_ctx = _test_context(ctx, mock.ws_url, server_base_url=mock.rest_base_url)
        channel = ServerChannel(test_ctx, EventBus())
        channel.start()
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            kinds: list[str] = []
            while loop.time() < deadline:
                kinds = [b["kind"] for b in mock.state.batches]
                if "account" in kinds and "positions" in kinds:
                    break
                await asyncio.sleep(0.2)
            assert kinds[:2] == ["account", "positions"], f"batches: {kinds}"
            # The account batch carries the REAL terminal account.
            login = ctx.driver.connection_state_dict()["login"]
            account_batch = next(b for b in mock.state.batches if b["kind"] == "account")
            assert account_batch["items"]["account"]["login"] == str(login)
            assert account_batch["items"]["account"]["type"] == "demo"
            assert test_ctx.server_channel_state["connected"] is True
        finally:
            await channel.stop()
    finally:
        mock.stop()


@pytest.mark.asyncio
async def test_server_channel_unknown_command_response(ctx, client):
    # §6.3 step 5: an operation outside the §6.1 allowlist is rejected
    # as UnsupportedOperation — reported as an action.result envelope,
    # never as the old command_result frame.
    got_error = asyncio.Event()
    login = ctx.driver.connection_state_dict()["login"]
    assert login, "no MT5 account bound — cannot address the command"
    device_id = ctx.get_or_create_device_id()
    command_id = new_uuid7_str()
    results: list[dict[str, Any]] = []

    async def handler(ws):  # type: ignore[no-untyped-def]
        # Record, never assert, in the handler (see test 1): an
        # assert here would kill the connection and mask the real
        # failure as a timeout on reconnect.
        await ws.send(json.dumps(_command_frame(command_id, login, device_id, "no_such_command", 1)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not got_error.is_set() and loop.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if msg.get("type") == "heartbeat":
                await ws.send(json.dumps({"type": "heartbeat"}))
            elif msg.get("event_type") == "action.result" and msg.get("correlation_id") == command_id:
                results.append(msg)
                got_error.set()

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    test_ctx = _test_context(ctx, f"ws://127.0.0.1:{port}")
    channel = ServerChannel(test_ctx, EventBus())
    channel.start()
    try:
        await asyncio.wait_for(got_error.wait(), timeout=25)
        assert results and results[0]["status"] == "rejected", f"unexpected result: {results}"
        assert results[0]["error"]["code"] == "UnsupportedOperation", f"unexpected error: {results}"
        assert test_ctx.server_channel_state["connected"] is True
    finally:
        await channel.stop()
        server.close()
        await server.wait_closed()
