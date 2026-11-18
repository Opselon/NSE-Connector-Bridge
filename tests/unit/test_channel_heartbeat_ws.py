"""Unit tests for the REST-heartbeat → WS interaction (contract §3.4,
gap §5 B1 task 2) and the §4.1 ``manifest.update`` WS frame.

Label: unit. No MT5, no network, no socket — the REST heartbeat is a
fake, the WS open is observed through the channel's wake/start hooks.

Contract §3.4: "the Connector SHOULD open/refresh its WS session when
[``pending_commands``] is non-zero and no WS is live." The audit could
not verify whether the old code did this; it did NOT — the WS loop
only retried on its own backoff schedule (up to 30 s), and the REST
heartbeat loop only logged the count. These tests prove the fixed
behaviour: a REST heartbeat with ``pending_commands > 0`` and no WS
live wakes the WS supervisor immediately.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.server.manifest import build_manifest
from nse_bridge.server.rest import HeartbeatResult

pytestmark = pytest.mark.unit


def _settings(tmp_path: Path, **overrides) -> BridgeSettings:
    base = {
        "server_base_url": "http://127.0.0.1:1",
        "environment": "test",
        "server_token": "tok-SECRET",
        "server_url": "ws://127.0.0.1:1/api/v1/connector/ws",
        "device_id": "dev-1",
        "data_dir": tmp_path,
        "heartbeat_interval_s": 0.05,
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


class _FakeRest:
    """REST stub: configurable pending_commands; records manifest posts."""

    def __init__(self, pending_commands: int = 0) -> None:
        self.pending_commands = pending_commands
        self.heartbeats = 0
        self.manifests: list[dict[str, Any]] = []
        self.user_id: str | None = None

    async def heartbeat(self) -> HeartbeatResult:
        self.heartbeats += 1
        return HeartbeatResult(ok=True, pending_commands=self.pending_commands)

    async def send_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self.manifests.append(manifest)
        return {"accepted": True, "manifest_hash": manifest.get("manifest_hash")}

    async def close(self) -> None:
        return None


def _channel(tmp_path: Path, rest: _FakeRest, **overrides) -> ServerChannel:
    settings = _settings(tmp_path, **overrides)
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    return ServerChannel(ctx, EventBus(), rest_client=rest)


async def _run_heartbeat_loop(channel: ServerChannel, seconds: float = 2.5) -> None:
    # The loop clamps its interval to >= 1 s, so allow > 1 s per beat.
    task = asyncio.create_task(channel._rest_heartbeat_loop())
    await asyncio.sleep(seconds)
    channel._stop.set()
    await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# REST heartbeat → WS open (§3.4)
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_heartbeat_pending_commands_wakes_ws(tmp_path: Path):
    rest = _FakeRest(pending_commands=3)
    channel = _channel(tmp_path, rest)
    wake_calls: list[bool] = []
    channel._request_ws_open = lambda: wake_calls.append(True) or True  # type: ignore[method-assign]
    await _run_heartbeat_loop(channel)
    assert rest.heartbeats >= 1
    assert wake_calls, "pending_commands=3 with no WS live must request a WS open"


@pytest.mark.unit
async def test_heartbeat_no_pending_commands_does_not_wake_ws(tmp_path: Path):
    rest = _FakeRest(pending_commands=0)
    channel = _channel(tmp_path, rest)
    wake_calls: list[bool] = []
    channel._request_ws_open = lambda: wake_calls.append(True) or True  # type: ignore[method-assign]
    await _run_heartbeat_loop(channel)
    assert rest.heartbeats >= 1
    assert wake_calls == []


@pytest.mark.unit
async def test_heartbeat_pending_commands_ignored_when_ws_live(tmp_path: Path):
    rest = _FakeRest(pending_commands=3)
    channel = _channel(tmp_path, rest)
    channel._ws = object()  # a live socket
    channel.ctx.server_channel_state["connected"] = True
    wake_calls: list[bool] = []
    channel._request_ws_open = lambda: wake_calls.append(True) or True  # type: ignore[method-assign]
    await _run_heartbeat_loop(channel)
    assert rest.heartbeats >= 1
    assert wake_calls == []  # WS already live: nothing to open


@pytest.mark.unit
async def test_request_ws_open_wakes_running_supervisor(tmp_path: Path):
    rest = _FakeRest(pending_commands=3)
    channel = _channel(tmp_path, rest)
    # Simulate the _run supervisor alive but backing off.
    channel._task = asyncio.create_task(asyncio.sleep(60.0))
    try:
        assert channel._is_ws_live() is False
        assert channel._request_ws_open() is True
        assert channel._wake.is_set()  # the backoff wait is interrupted
    finally:
        channel._task.cancel()
        try:
            await channel._task
        except asyncio.CancelledError:
            pass


@pytest.mark.unit
async def test_request_ws_open_noop_when_ws_live(tmp_path: Path):
    rest = _FakeRest(pending_commands=3)
    channel = _channel(tmp_path, rest)
    channel._ws = object()
    channel.ctx.server_channel_state["connected"] = True
    assert channel._request_ws_open() is False
    assert not channel._wake.is_set()


@pytest.mark.unit
async def test_wait_backoff_returns_early_on_wake(tmp_path: Path):
    rest = _FakeRest()
    channel = _channel(tmp_path, rest)
    channel._wake.set()
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(channel._wait_backoff(30.0), timeout=5.0)
    assert asyncio.get_running_loop().time() - started < 5.0  # did not sleep 30 s
    assert not channel._wake.is_set()  # the wake was consumed


# ---------------------------------------------------------------------------
# manifest.update WS frame (§4.1)
# ---------------------------------------------------------------------------
@pytest.mark.unit
async def test_maybe_send_manifest_update_enqueues_frame_once(tmp_path: Path):
    rest = _FakeRest()
    channel = _channel(tmp_path, rest)
    assert channel._maybe_send_manifest_update() is True
    frame = await asyncio.wait_for(channel._outbound.get(), timeout=2.0)
    assert frame["event_type"] == "manifest.update"
    assert frame["payload"]["manifest_hash"] == build_manifest("dev-1")["manifest_hash"]
    assert frame["payload"]["action_count"] == 91  # 91 local commands (C2: +trade_close_position_by)
    # Unchanged hash: no second frame.
    assert channel._maybe_send_manifest_update() is False


@pytest.mark.unit
async def test_heartbeat_loop_posts_manifest_via_rest_when_ws_down(tmp_path: Path):
    """While the WS is down, a changed manifest still reaches the
    server — over REST, from the heartbeat loop (§4.1 offline path)."""
    rest = _FakeRest(pending_commands=0)
    channel = _channel(tmp_path, rest)
    channel._request_ws_open = lambda: False  # type: ignore[method-assign]
    await _run_heartbeat_loop(channel)
    assert len(rest.manifests) == 1  # posted once, then the hash is known
    assert rest.manifests[0]["manifest_hash"] == build_manifest("dev-1")["manifest_hash"]
