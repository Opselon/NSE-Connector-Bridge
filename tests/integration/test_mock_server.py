"""Mock-integration tests against the CONTRACT v1 mock server.

Label: mock-integration. Real local HTTP + WS (no MT5, no live
Platform): the REST client talks to the mock over real HTTP, and the
WS channel connects to the mock over a real socket. The MT5 driver
is never connected — snapshot providers and the command executor are
injected fakes / the real validator with no bound account.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.chart import ChartService
from nse_bridge.server.rest import RestClient
from nse_bridge.server.sync import SyncEngine
from nse_bridge.server.uuid7 import new_uuid7_str

pytestmark = pytest.mark.mock_integration


def _settings(mock_server, **overrides):
    base = {
        "server_base_url": mock_server.rest_base_url,
        "environment": "test",
        "server_token": "test-token",
        "device_id": "dev-1",
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


@pytest.mark.mock_integration
async def test_rest_round_trip_against_mock(mock_server):
    settings = _settings(mock_server)
    async with RestClient(settings, "dev-1") as rest:
        key = await rest.validate_key()
        assert key.valid is True
        assert key.user_id == "00000000-0000-0000-0000-000000000001"
        assert key.entitlements.position_manager is True
        assert key.entitlements.auto_trade is True

        registered = await rest.register_device()
        assert registered.registered is True
        assert registered.session_expires_at is not None

        ack = await rest.send_batch({
            "batch_id": new_uuid7_str(), "kind": "account",
            "items": {"account": {"login": "12345678901"}, "terminal": {}},
            "count": 1, "produced_at": "2026-11-18T00:00:00Z",
        })
        assert ack.accepted is True
        assert len(mock_server.state.batches) == 1
        assert mock_server.state.batches[0]["kind"] == "account"

        heartbeat = await rest.heartbeat()
        assert heartbeat.ok is True
        assert heartbeat.pending_commands == 0

        refreshed = await rest.session_refresh()
        assert refreshed.session_expires_at is not None
        assert await rest.session_revoke() is True

        release = await rest.releases_latest()
        assert release.version == "0.2.0"
        assert release.sha256 == "0" * 64

        manifest_ack = await rest.send_manifest({
            "device_id": "dev-1", "manifest_hash": "abc123", "action_count": 90,
        })
        assert manifest_ack["accepted"] is True
        assert mock_server.state.manifests[0]["manifest_hash"] == "abc123"


@pytest.mark.mock_integration
async def test_rest_401_key_invalid_against_mock(mock_server):
    settings = _settings(mock_server, server_token="bad-token")
    async with RestClient(settings, "dev-1") as rest:
        from nse_bridge.server.errors import ServerError, ServerErrorCode

        with pytest.raises(ServerError) as excinfo:
            await rest.validate_key()
        assert excinfo.value.code is ServerErrorCode.KEY_INVALID
        with pytest.raises(ServerError) as excinfo:
            await rest.register_device()
        assert excinfo.value.code is ServerErrorCode.KEY_INVALID


@pytest.mark.mock_integration
async def test_ws_channel_demo_command_rejected_wrong_account(mock_server):
    """End-to-end over a real socket: the mock sends sync_request +
    a demo close_position for account 12345678901. This Bridge has NO
    bound MT5 account (driver not connected), so §6.3 step 4 rejects
    it as WrongAccount — and the mock receives the action.result."""
    settings = _settings(mock_server)
    settings.server_url = mock_server.ws_url
    settings.heartbeat_interval_s = 1.0  # fast heartbeat for the test
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    ctx.get_or_create_device_id()

    rest = RestClient(settings, ctx.device_id)

    async def account_snapshot():
        return {"account": {"login": "12345678901"}, "terminal": {}}

    async def positions_snapshot():
        return {"positions": [], "orders": []}

    async def history_orders(from_dt, to_dt):
        return {"orders": [], "deals": []}

    async def history_positions(from_dt, to_dt):
        return {"positions": []}

    engine = SyncEngine(
        rest, ctx,
        account_snapshot=account_snapshot,
        positions_snapshot=positions_snapshot,
        history_orders=history_orders,
        history_positions=history_positions,
    )
    chart = ChartService(fetch_candles=lambda *a: [], fetch_ticks=lambda *a: [])
    channel = ServerChannel(
        ctx, EventBus(), rest_client=rest, sync_engine=engine, chart_service=chart,
        dead_after_s=45.0,
    )
    channel.start()
    try:
        # The demo command id is fixed in the mock server.
        demo_id = "01900000-0000-7000-8000-000000000001"
        result = await asyncio.to_thread(
            mock_server.state.wait_for_result, demo_id, timeout=10.0
        )
        assert result is not None, "mock never received the demo command result"
        assert result["correlation_id"] == demo_id
        assert result["status"] == "rejected"
        assert result["error"]["code"] == "WrongAccount"
        assert result["event_type"] == "action.result"
        assert result["protocol_version"] == "1.0"

        # The mock's sync_request made the channel re-send account +
        # positions batches (REST ack is authoritative).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while len(mock_server.state.batches) < 2 and loop.time() < deadline:
            await asyncio.sleep(0.1)
        kinds = [b["kind"] for b in mock_server.state.batches]
        assert kinds[:2] == ["account", "positions"]

        # Heartbeats flow both ways (§5.3) — the first client heartbeat
        # is due one interval (1 s in this test) after connect, so poll
        # for it instead of asserting immediately.
        deadline = loop.time() + 3.0
        while mock_server.state.heartbeats_received < 1 and loop.time() < deadline:
            await asyncio.sleep(0.1)
        assert mock_server.state.heartbeats_received >= 1
        assert ctx.server_channel_state["connected"] is True
    finally:
        await channel.stop()
        await rest.close()
    assert ctx.server_channel_state["connected"] is False


@pytest.mark.mock_integration
async def test_mock_chart_request_subscribe_round_trip(mock_server):
    """Raw WS client against the mock: chart.request / subscribe /
    unsubscribe shapes (§17.1) and the 21st-subscription refusal."""
    async with websockets.connect(
        mock_server.ws_url,
        additional_headers={
            "Authorization": "Bearer test-token",
            "X-NSE-Device-Id": "dev-1",
        },
    ) as ws:
        # Drain the mock's sync_request + demo command first.
        for _ in range(2):
            await asyncio.wait_for(ws.recv(), timeout=5)

        request_id = new_uuid7_str()
        await ws.send(json.dumps({
            "type": "chart.request", "request_id": request_id,
            "symbol": "EURUSD", "timeframe": "M15", "count": 2,
            "include_ticks": True,
        }))
        data = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert data["type"] == "chart.data"
        assert data["request_id"] == request_id
        assert data["status"] == "ok"
        assert len(data["candles"]) == 2
        assert data["candles"][0]["time"] == "2026-11-16T00:00:00"
        tick = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert tick["type"] == "chart.tick"
        assert tick["ticks"][0]["bid"] == 1.10042

        # Unknown symbol → symbol_not_found error frame (§17.1.4).
        bad_id = new_uuid7_str()
        await ws.send(json.dumps({
            "type": "chart.request", "request_id": bad_id,
            "symbol": "UNKNOWN", "timeframe": "M15", "count": 2,
        }))
        error = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert error["status"] == "error"
        assert error["error_code"] == "symbol_not_found"

        # 20 subscriptions succeed; the 21st is refused (§17.1.1).
        for i in range(20):
            await ws.send(json.dumps({
                "type": "chart.subscribe", "subscription_id": new_uuid7_str(),
                "symbol": "EURUSD", "timeframe": "M15", "poll_interval_s": 15,
            }))
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["status"] == "ok", f"subscription {i} failed: {frame}"
        await ws.send(json.dumps({
            "type": "chart.subscribe", "subscription_id": new_uuid7_str(),
            "symbol": "EURUSD", "timeframe": "M15", "poll_interval_s": 15,
        }))
        refused = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert refused["error_code"] == "subscription_limit"


@pytest.mark.mock_integration
async def test_mock_ws_rejects_bad_auth(mock_server):
    with pytest.raises(websockets.ConnectionClosed):
        async with websockets.connect(
            mock_server.ws_url,
            additional_headers={
                "Authorization": "Bearer wrong-token",
                "X-NSE-Device-Id": "dev-1",
            },
        ) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)
