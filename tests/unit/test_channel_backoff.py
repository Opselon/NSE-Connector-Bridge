"""Unit tests for the WS channel helpers (CONTRACT v1 §5.3/§5.4).

Label: unit. The reconnect backoff formula and the priority outbound
queue are pure; the live socket behaviour is covered by the
mock-integration tests.
"""

from __future__ import annotations

import asyncio

import pytest

from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import (
    ServerChannel,
    _OutboundQueue,
    backoff_delay,
    backoff_with_jitter,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_backoff_delay_doubles_from_500ms_and_caps_at_30s():
    assert backoff_delay(0) == pytest.approx(0.5)
    assert backoff_delay(1) == pytest.approx(1.0)
    assert backoff_delay(2) == pytest.approx(2.0)
    assert backoff_delay(5) == pytest.approx(16.0)
    assert backoff_delay(6) == pytest.approx(30.0)  # capped
    assert backoff_delay(20) == pytest.approx(30.0)


@pytest.mark.unit
def test_backoff_delay_respects_configured_initial_and_cap():
    assert backoff_delay(0, initial_s=1.0, cap_s=5.0) == pytest.approx(1.0)
    assert backoff_delay(10, initial_s=1.0, cap_s=5.0) == pytest.approx(5.0)


@pytest.mark.unit
def test_backoff_jitter_is_0_to_100ms():
    for _ in range(200):
        value = backoff_with_jitter(1.0)
        assert 1.0 <= value <= 1.1


@pytest.mark.unit
async def test_outbound_queue_results_first():
    queue = _OutboundQueue(maxsize=10)
    queue.put_chart({"type": "chart.data", "symbol": "EURUSD"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c1"})
    first = await asyncio.wait_for(queue.get(), timeout=2)
    assert first["event_type"] == "action.result"
    second = await asyncio.wait_for(queue.get(), timeout=2)
    assert second["type"] == "chart.data"


@pytest.mark.unit
async def test_outbound_queue_full_drops_oldest_chart_for_result():
    queue = _OutboundQueue(maxsize=3)
    queue.put_chart({"type": "chart.data", "symbol": "A"})
    queue.put_chart({"type": "chart.data", "symbol": "B"})
    queue.put_chart({"type": "chart.data", "symbol": "C"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c1"})
    got = [await asyncio.wait_for(queue.get(), timeout=2) for _ in range(3)]
    assert got[0]["correlation_id"] == "c1"  # results first
    assert [g.get("symbol") for g in got[1:]] == ["B", "C"]  # oldest chart dropped


@pytest.mark.unit
async def test_outbound_queue_full_of_results_drops_oldest_result():
    queue = _OutboundQueue(maxsize=2)
    queue.put_result({"event_type": "action.result", "correlation_id": "c1"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c2"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c3"})
    got = [await asyncio.wait_for(queue.get(), timeout=2) for _ in range(2)]
    assert [g["correlation_id"] for g in got] == ["c2", "c3"]


@pytest.mark.unit
async def test_outbound_queue_full_of_results_drops_new_chart():
    queue = _OutboundQueue(maxsize=2)
    queue.put_result({"event_type": "action.result", "correlation_id": "c1"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c2"})
    assert queue.put_chart({"type": "chart.data", "symbol": "EURUSD"}) is False
    got = [await asyncio.wait_for(queue.get(), timeout=2) for _ in range(2)]
    assert [g["correlation_id"] for g in got] == ["c1", "c2"]


@pytest.mark.unit
async def test_channel_ignores_unknown_server_frames():
    settings = BridgeSettings(_env_file=None, server_token="x", device_id="dev-1")
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    channel = ServerChannel(ctx, EventBus())
    # Unknown frames are silently ignored, never fatal (§5.2).
    await channel._handle_server_message('{"type": "bogus", "x": 1}')
    await channel._handle_server_message("not json at all")
    await channel._handle_server_message('{"type": "command_result", "id": "1"}')
    await channel._handle_server_message("[1, 2, 3]")


@pytest.mark.unit
async def test_channel_heartbeat_timing_defaults():
    settings = BridgeSettings(_env_file=None)
    assert settings.heartbeat_interval_s == 15.0  # §5.3
    assert settings.reconnect_initial_ms == 500  # §5.4
    assert settings.reconnect_max_ms == 30000
