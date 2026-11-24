"""Mock-integration tests for device enrolment at startup
(CONTRACT v1 §3.1/§3.2, §5.1, §3.4).

Label: mock-integration. Real local HTTP + WS against the CONTRACT v1
mock server (no MT5, no live Platform): the enrolment manager talks
to the mock over real HTTP, and the WS channel connects to the mock
over a real socket. The MT5 driver is never connected.
"""

from __future__ import annotations

import asyncio

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.enrolment import EnrolmentManager
from nse_bridge.server.rest import RestClient
from nse_bridge.server.sync import SyncEngine

pytestmark = pytest.mark.mock_integration


def _settings(mock_server, **overrides):
    base = {
        "server_base_url": mock_server.rest_base_url,
        "environment": "test",
        "server_token": "test-token",
        "server_url": mock_server.ws_url,
        "device_id": "dev-1",
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


def _ctx(settings: BridgeSettings) -> BridgeContext:
    return BridgeContext(settings=settings, driver=Mt5Driver(settings))


async def _wait_until(predicate, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        assert asyncio.get_event_loop().time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.05)


@pytest.mark.mock_integration
async def test_enrolment_success_against_mock(mock_server):
    settings = _settings(mock_server)
    ctx = _ctx(settings)
    async with RestClient(settings, "dev-1") as rest:
        manager = EnrolmentManager(ctx, rest)
        assert await manager.enrol_once() is True
        assert manager.state == "registered"
        # Server-resolved identity + subscription stored on the context.
        stored = ctx.validate_key_result
        assert stored is not None
        assert stored["user_id"] == "00000000-0000-0000-0000-000000000001"
        assert stored["subscription"]["plan"] == "pro"
        assert stored["entitlements"]["max_devices"] == 3
        assert manager.session_expires_at is not None
        assert ctx.enrolment_status()["state"] == "not_configured"  # manager not wired on ctx here
        ctx.enrolment = manager
        assert ctx.enrolment_status()["state"] == "registered"


@pytest.mark.mock_integration
async def test_bad_key_failed_auth_and_no_ws_connection(mock_server):
    settings = _settings(mock_server, server_token="bad-token")
    ctx = _ctx(settings)
    rest = RestClient(settings, "dev-1")
    sync = SyncEngine(rest, ctx)
    manager = EnrolmentManager(ctx, rest, retry_base_s=0.05, jitter=False)
    rest.enrolment = manager
    ctx.rest_client = rest
    ctx.sync_engine = sync
    ctx.enrolment = manager
    stop = asyncio.Event()
    run_task = asyncio.create_task(manager.run(stop))
    channel = ServerChannel(ctx, EventBus(), rest_client=rest, sync_engine=sync, enrolment=manager)
    channel.start()
    try:
        await _wait_until(lambda: manager.state == "failed_auth", what="FAILED_AUTH")
        assert manager.to_dict()["last_error"] == "key_invalid"
        await asyncio.sleep(0.5)  # the channel must never open, even given time
        assert mock_server.state.ws_connections == 0
        assert not channel.running
        assert "failed_auth" in (ctx.server_channel_state.get("last_error") or "")
        # Sync stops immediately too: a flush sends nothing.
        sync.enqueue("account", {"account": {"login": "12345678901"}, "terminal": {}})
        sent, failed = await sync.flush()
        assert (sent, failed) == (0, 0)
        assert sync.pending_count() == 1  # kept queued, unacked, for a future key
    finally:
        await channel.stop()
        stop.set()
        await asyncio.wait_for(run_task, timeout=15.0)
        await rest.close()


@pytest.mark.mock_integration
async def test_channel_connects_only_after_registered(mock_server):
    settings = _settings(mock_server)
    ctx = _ctx(settings)
    rest = RestClient(settings, "dev-1")
    sync = SyncEngine(rest, ctx)
    manager = EnrolmentManager(ctx, rest, retry_base_s=0.05, jitter=False)
    rest.enrolment = manager
    ctx.rest_client = rest
    ctx.sync_engine = sync
    ctx.enrolment = manager
    stop = asyncio.Event()
    run_task = asyncio.create_task(manager.run(stop))
    channel = ServerChannel(ctx, EventBus(), rest_client=rest, sync_engine=sync, enrolment=manager)
    channel.start()
    try:
        await _wait_until(lambda: manager.state == "registered", what="REGISTERED")
        await _wait_until(
            lambda: mock_server.state.ws_connections >= 1,
            what="WS connection after enrolment",
        )
        assert ctx.server_channel_state["connected"] is True
    finally:
        await channel.stop()
        stop.set()
        await asyncio.wait_for(run_task, timeout=15.0)
        await rest.close()


@pytest.mark.mock_integration
async def test_rest_heartbeat_keeps_running_while_ws_down(mock_server):
    # WS endpoint is dead (connection refused) but REST is the mock:
    # enrolment succeeds, the channel keeps retrying the WS, and the
    # REST heartbeat (§3.4) keeps ticking as the keep-alive.
    settings = _settings(
        mock_server,
        server_url="ws://127.0.0.1:1/api/v1/connector/ws",
        heartbeat_interval_s=0.2,
        reconnect_initial_ms=500,
        reconnect_max_ms=1000,
    )
    ctx = _ctx(settings)
    rest = RestClient(settings, "dev-1")
    sync = SyncEngine(rest, ctx)
    manager = EnrolmentManager(ctx, rest, retry_base_s=0.05, jitter=False)
    rest.enrolment = manager
    ctx.rest_client = rest
    ctx.sync_engine = sync
    ctx.enrolment = manager
    assert await manager.enrol_once() is True
    channel = ServerChannel(ctx, EventBus(), rest_client=rest, sync_engine=sync, enrolment=manager)
    channel.start()
    try:
        await _wait_until(
            lambda: mock_server.state.rest_heartbeats_received >= 3,
            timeout=10.0,
            what="REST heartbeats while WS is down",
        )
        assert mock_server.state.ws_connections == 0  # WS never came up
        assert manager.state == "registered"  # enrolment unaffected by the dead WS
    finally:
        await channel.stop()
        await rest.close()
