"""Unit tests for the HTTP diagnostics sink (P5a, GAP F / D1).

Label: unit. Covers the sink's wire contract against the Platform
diagnostics endpoint (path, auth headers, batch envelope, 9-field
event shape), its failure posture (transport error / 4xx / 5xx /
non-JSON 2xx all return False so the queue keeps the event for
retry/backoff), the queue drain through the sink, and the emitter
wiring (ERROR immediate drain + summary flags).

The sink is exercised against ``httpx.MockTransport`` — no network,
no Platform needed. The live round-trip against a real Platform is
a mock-integration concern, not a unit test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from nse_bridge.reliability.telemetry import (
    PATH_DIAGNOSTICS,
    WARN,
    DiagnosticQueue,
    HttpDiagnosticsSink,
    TelemetryEmitter,
    TelemetryEvent,
)

pytestmark = pytest.mark.unit


def _event(**overrides) -> TelemetryEvent:
    fields = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "severity": WARN,
        "event_type": "test.event",
        "device_id": "dev-1",
        "correlation_id": "corr-1",
        "command_id": None,
        "component": "test",
        "error_code": None,
        "sanitized_details": {},
    }
    fields.update(overrides)
    return TelemetryEvent(**fields)


def _sink(handler, **overrides):
    kwargs = {
        "base_url": "https://bridge.example.test",
        "api_key": "nsek_test_key",
        "device_id": "dev-1",
        "transport": httpx.MockTransport(handler),
    }
    kwargs.update(overrides)
    return HttpDiagnosticsSink(**kwargs)


def _accepting_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"accepted": 1, "stored": 1})


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------
def test_sink_posts_event_to_diagnostics_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["device"] = request.headers.get("X-NSE-Device-Id")
        seen["body"] = json.loads(request.content.decode())
        return _accepting_handler(request)

    sink = _sink(handler)
    assert sink.send(_event()) is True
    assert seen["method"] == "POST"
    assert seen["path"] == PATH_DIAGNOSTICS == "/api/v1/connector/diagnostics"
    assert seen["auth"] == "Bearer nsek_test_key"
    assert seen["device"] == "dev-1"
    # Batch envelope with exactly the nine contract fields, in order.
    events = seen["body"]["events"]
    assert len(events) == 1
    assert list(events[0].keys()) == [
        "timestamp",
        "severity",
        "event_type",
        "device_id",
        "correlation_id",
        "command_id",
        "component",
        "error_code",
        "sanitized_details",
    ]
    assert events[0]["event_type"] == "test.event"


def test_sink_resolves_lazy_credentials_at_send_time():
    seen_auth: list = []
    current_key = {"value": "nsek_first"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        return _accepting_handler(request)

    sink = HttpDiagnosticsSink(
        base_url="https://bridge.example.test",
        api_key=lambda: current_key["value"],
        device_id="dev-1",
        transport=httpx.MockTransport(handler),
    )
    assert sink.send(_event()) is True
    # Enrolment reassigns the key after startup: the next send must
    # use the NEW key, not the snapshot from construction.
    current_key["value"] = "nsek_rotated"
    assert sink.send(_event()) is True
    assert seen_auth == ["Bearer nsek_first", "Bearer nsek_rotated"]


# ---------------------------------------------------------------------------
# Failure posture: False means "keep it queued"
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_sink_returns_false_on_http_error(status):
    sink = _sink(lambda request: httpx.Response(status, json={"error": {"code": "x"}}))
    assert sink.send(_event()) is False


def test_sink_returns_false_when_server_acks_zero():
    sink = _sink(lambda request: httpx.Response(200, json={"accepted": 0, "stored": 0}))
    assert sink.send(_event()) is False


def test_sink_returns_false_on_non_json_2xx():
    sink = _sink(lambda request: httpx.Response(200, content=b"not json"))
    assert sink.send(_event()) is False


def test_sink_returns_false_on_2xx_without_accepted_field():
    # A 2xx that acks nothing (e.g. {"stored": 1} with no "accepted")
    # is NOT an ack: the event stays queued.
    sink = _sink(lambda request: httpx.Response(200, json={"stored": 1}))
    assert sink.send(_event()) is False


def test_sink_returns_false_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    sink = _sink(handler)
    assert sink.send(_event()) is False


def test_sink_sends_nothing_without_an_api_key():
    called: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return _accepting_handler(request)

    sink = _sink(handler, api_key="")
    assert sink.send(_event()) is False
    assert called == []


# ---------------------------------------------------------------------------
# Queue drain through the sink
# ---------------------------------------------------------------------------
def test_queue_drain_delivers_via_http_sink():
    sink = _sink(_accepting_handler)
    queue = DiagnosticQueue()
    queue.put(_event(event_type="test.one"))
    queue.put(_event(event_type="test.two", error_code="E1"))
    assert queue.drain(sink) == 2
    assert len(queue) == 0
    assert queue.stats()["delivered"] == 2


def test_queue_drain_keeps_failed_events_for_retry():
    sink = _sink(lambda request: httpx.Response(500, json={"error": {"code": "internal"}}))
    queue = DiagnosticQueue()
    queue.put(_event(event_type="test.one"))
    assert queue.drain(sink) == 0
    assert len(queue) == 1
    assert queue.stats()["delivered"] == 0
    # A second immediate drain backs off (no tight loop into the same 500).
    assert queue.drain(sink) == 0
    assert len(queue) == 1


# ---------------------------------------------------------------------------
# Emitter wiring
# ---------------------------------------------------------------------------
def test_emitter_with_http_sink_reports_attached_and_drains_errors_immediately():
    sink = _sink(_accepting_handler)
    emitter = TelemetryEmitter("dev-1", sink=sink)
    summary = emitter.summary()
    assert summary["sink_attached"] is True
    assert summary["sink_inert"] is False
    emitter.error("test.boom", component="test", error_code="E1")
    # ERROR triggers an immediate drain: the queue is empty afterwards.
    assert len(emitter.queue) == 0
    assert emitter.queue.stats()["delivered"] == 1


def test_emitter_without_sink_keeps_queue_inert():
    emitter = TelemetryEmitter("dev-1")
    assert emitter.summary()["sink_inert"] is True
    emitter.warn("test.warn", component="test")
    assert len(emitter.queue) == 1
    assert emitter.drain() == 0


# ---------------------------------------------------------------------------
# Startup wiring (B4): the PR #22 sink must actually be attached by the
# app lifespan, and the 60 s periodic drain must run — a sink that
# exists only as a class is not shipped.
# ---------------------------------------------------------------------------
def test_lifespan_wires_http_diagnostics_sink_and_periodic_drain(tmp_path):
    """Entering the real app lifespan attaches the HTTP sink (not the
    inert Null sink) and starts the 60 s periodic drain task."""
    from fastapi.testclient import TestClient

    from nse_bridge.api.app import DIAGNOSTICS_DRAIN_INTERVAL_S, create_app
    from nse_bridge.config import BridgeSettings
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from nse_bridge.reliability.telemetry import HttpDiagnosticsSink

    assert DIAGNOSTICS_DRAIN_INTERVAL_S == 60.0
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    try:
        with TestClient(app, raise_server_exceptions=False):
            telemetry = app.state.telemetry
            assert isinstance(telemetry.sink, HttpDiagnosticsSink)
            assert telemetry.summary()["sink_attached"] is True
            assert telemetry.summary()["sink_inert"] is False
            drain_task = app.state.diagnostics_drain_task
            assert drain_task.get_name() == "nse-diagnostics-drain"
            assert not drain_task.done()
            # The sink resolves its credentials lazily from the REST
            # client the lifespan built — no snapshot taken at startup.
            assert telemetry.sink._api_key() == ""
    finally:
        driver.disconnect()


@pytest.mark.unit
async def test_periodic_drain_loop_drains_repeatedly(monkeypatch):
    """The periodic drain task actually calls telemetry.drain() once
    per interval until cancelled — with the interval patched short
    so the test observes several drains without waiting 60 s."""
    import asyncio

    from nse_bridge.api import app as app_module

    # The interval is patched to 0 (each iteration is just a yield),
    # so the loop drains as fast as the event loop iterates — the
    # test observes several drains without depending on wall-clock
    # timing under load.
    monkeypatch.setattr(app_module, "DIAGNOSTICS_DRAIN_INTERVAL_S", 0.0)
    calls: list[int] = []

    class _FakeTelemetry:
        def drain(self) -> int:
            calls.append(1)
            return 0

    task = asyncio.create_task(app_module._periodic_diagnostics_drain(_FakeTelemetry()))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2  # drained repeatedly, not once


def test_durable_queue_reloads_and_drains_via_mock_sink(tmp_path):
    """Resume story for diagnostics: events queued before a restart
    are re-queued from the durable store and drain through the mock
    Platform endpoint — none are lost to the restart."""
    from nse_bridge.reliability.diagnostic_store import DurableDiagnosticQueue, DurableDiagnosticStore

    store_path = tmp_path / "pending_diagnostics.jsonl"
    queue1 = DurableDiagnosticQueue(DurableDiagnosticStore(store_path))
    queue1.put(_event(event_type="test.before_restart"))
    assert store_path.is_file()
    # A new queue over the same store (a Bridge restart) reloads it.
    queue2 = DurableDiagnosticQueue(DurableDiagnosticStore(store_path))
    assert len(queue2) == 1
    sink = _sink(_accepting_handler)
    assert queue2.drain(sink) == 1
    assert len(queue2) == 0
    assert queue2.stats()["delivered"] == 1
    # Delivered events leave the store: a third restart finds nothing.
    queue3 = DurableDiagnosticQueue(DurableDiagnosticStore(store_path))
    assert len(queue3) == 0
