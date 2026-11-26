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


# ---------------------------------------------------------------------------
# B2: WS connect-failure classification — a WS-auth failure (HTTP 401)
# is a different failure from an enrolment failure, and the dashboard
# must show the server's verbatim reason, never a bare type name.
# ---------------------------------------------------------------------------
class _FakeWsResponse:
    """Duck-typed websockets InvalidStatus.response."""

    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self.body = body


class _FakeInvalidStatus(Exception):
    def __init__(self, status_code: int, body: bytes = b"") -> None:
        super().__init__(f"server rejected WebSocket connection: HTTP {status_code}")
        self.response = _FakeWsResponse(status_code, body)


@pytest.mark.unit
def test_classify_ws_401_key_revoked_verbatim():
    from nse_bridge.realtime.channel import classify_ws_connect_error

    info = classify_ws_connect_error(
        _FakeInvalidStatus(401, b'{"error": {"code": "key_revoked", "message": "key revoked by owner"}}')
    )
    assert info["kind"] == "ws_auth"
    assert info["http_status"] == 401
    assert info["code"] == "key_revoked"
    assert info["message"] == "key revoked by owner"
    assert info["detail"] == "WS handshake failed: HTTP 401 key_revoked — key revoked by owner"


@pytest.mark.unit
def test_classify_ws_401_key_invalid_and_403():
    from nse_bridge.realtime.channel import classify_ws_connect_error

    info = classify_ws_connect_error(
        _FakeInvalidStatus(401, b'{"error": {"code": "key_invalid", "message": "bad key"}}')
    )
    assert info["kind"] == "ws_auth"
    assert info["code"] == "key_invalid"
    info = classify_ws_connect_error(
        _FakeInvalidStatus(403, b'{"error": {"code": "subscription_expired", "message": "plan ended"}}')
    )
    assert info["kind"] == "ws_auth"
    assert info["http_status"] == 403
    assert info["code"] == "subscription_expired"


@pytest.mark.unit
def test_classify_ws_500_is_transport_not_auth():
    from nse_bridge.realtime.channel import classify_ws_connect_error

    info = classify_ws_connect_error(_FakeInvalidStatus(500, b""))
    assert info["kind"] == "ws_transport"
    assert info["http_status"] == 500
    assert info["detail"] == "WS handshake failed: HTTP 500"


@pytest.mark.unit
def test_classify_ws_timeout_and_refused_are_transport():
    from nse_bridge.realtime.channel import classify_ws_connect_error

    info = classify_ws_connect_error(TimeoutError("timed out"))
    assert info["kind"] == "ws_transport"
    assert info["code"] == "handshake_timeout"
    assert "timed out" in info["detail"]
    info = classify_ws_connect_error(ConnectionRefusedError("mock ws down"))
    assert info["kind"] == "ws_transport"
    assert "ConnectionRefusedError" in info["detail"]


@pytest.mark.unit
def test_classify_ws_real_invalid_status():
    """The classifier reads a real websockets InvalidStatus (the
    exception websockets.connect raises on a non-101 handshake)."""
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from nse_bridge.realtime.channel import classify_ws_connect_error

    exc = InvalidStatus(
        Response(401, "Unauthorized", Headers(), b'{"error": {"code": "key_invalid", "message": "bad key"}}')
    )
    info = classify_ws_connect_error(exc)
    assert info["kind"] == "ws_auth"
    assert info["http_status"] == 401
    assert info["code"] == "key_invalid"


@pytest.mark.unit
def test_outbound_queue_total_queued():
    queue = _OutboundQueue(maxsize=10)
    assert queue.total_queued == 0
    queue.put_chart({"type": "chart.data", "symbol": "EURUSD"})
    queue.put_result({"event_type": "action.result", "correlation_id": "c1"})
    assert queue.total_queued == 2


# ---------------------------------------------------------------------------
# B2 latency hot spot 1 (PERFORMANCE-v14-baseline): the receive loop
# used to await chart.request inline, so a slow MT5 chart fetch
# delayed the next incoming command by ~330 ms. Chart requests run
# on a task now, like commands — the loop keeps receiving.
# ---------------------------------------------------------------------------
class _SlowChartService:
    """ChartService stub whose fetch blocks a worker thread for 0.5 s."""

    class _Limiter:
        def allow(self) -> bool:
            return True

        def would_allow(self) -> bool:
            return True

    def __init__(self) -> None:
        self.rate_limiter = self._Limiter()

    def handle_request(self, frame: dict) -> list[dict]:
        import time

        time.sleep(0.5)
        return [{"type": "chart.data", "request_id": frame.get("request_id"), "candles": []}]


class _ExplodingChartService(_SlowChartService):
    def handle_request(self, frame: dict) -> list[dict]:
        raise RuntimeError("boom")


@pytest.mark.unit
async def test_chart_request_does_not_block_receive_loop():
    settings = BridgeSettings(_env_file=None, server_token="x", device_id="dev-1")
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    channel = ServerChannel(ctx, EventBus())
    channel._chart = _SlowChartService()  # type: ignore[assignment]
    started = asyncio.get_running_loop().time()
    await channel._handle_server_message(
        '{"type": "chart.request", "request_id": "r1", "symbol": "EURUSD", "timeframe": "M1"}'
    )
    elapsed = asyncio.get_running_loop().time() - started
    # The old code awaited the 0.5 s fetch inline; the dispatch must
    # return immediately now (the fetch runs on a task).
    assert elapsed < 0.2, f"receive loop blocked {elapsed:.2f}s on chart.request"
    assert len(channel._chart_request_tasks) == 1
    # The frame still arrives — just later, off the loop.
    frame = await asyncio.wait_for(channel._outbound.get(), timeout=5.0)
    assert frame["request_id"] == "r1"
    deadline = asyncio.get_event_loop().time() + 5.0
    while channel._chart_request_tasks:
        assert asyncio.get_event_loop().time() < deadline, "chart task never finished"
        await asyncio.sleep(0.05)


@pytest.mark.unit
async def test_chart_request_failure_answers_with_error_frame():
    """A chart fetch that raises unexpectedly must answer with a
    chart error frame — a fire-and-forget task can never die silently
    with no answer to the server."""
    settings = BridgeSettings(_env_file=None, server_token="x", device_id="dev-1")
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    channel = ServerChannel(ctx, EventBus())
    channel._chart = _ExplodingChartService()  # type: ignore[assignment]
    await channel._handle_server_message(
        '{"type": "chart.request", "request_id": "r2", "symbol": "EURUSD", "timeframe": "M1"}'
    )
    frame = await asyncio.wait_for(channel._outbound.get(), timeout=5.0)
    assert frame["request_id"] == "r2"
    assert frame["status"] == "error"
    assert frame["error_code"] == "chart_error"
