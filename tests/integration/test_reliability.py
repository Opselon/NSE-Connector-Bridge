"""Mock-integration tests for the reliability phase (§8).

Label: mock-integration. Real local HTTP + WS against the CONTRACT v1
mock server (no MT5, no live Platform):

- a Bridge restart mid-operation re-queues a durably persisted
  command result and the mock server receives it on reconnect;
- the diagnostics summary exposes the telemetry queue state and the
  15-scenario self-healing state (data only — the GUI renders it).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.realtime.bus import EventBus
from nse_bridge.realtime.channel import ServerChannel
from nse_bridge.reliability import SelfHealingRegistry, TelemetryEmitter
from nse_bridge.reliability.self_healing import HealingContext, Scenario
from nse_bridge.server.chart import ChartService
from nse_bridge.server.rest import RestClient
from nse_bridge.server.sync import SyncEngine


def _settings(mock_server, tmp_path, **overrides):
    base = {
        "server_base_url": mock_server.rest_base_url,
        "environment": "test",
        "server_token": "test-token",
        "device_id": "dev-1",
        "data_dir": tmp_path,
        "log_dir": str(tmp_path / "logs"),
    }
    base.update(overrides)
    return BridgeSettings(_env_file=None, **base)


@pytest.mark.mock_integration
async def test_restart_mid_operation_persisted_result_redelivered(mock_server, tmp_path):
    """Seed a persisted unacknowledged result (as if the previous
    Bridge process produced it and died before the WS send), start a
    fresh channel, and verify the mock receives it on connect."""
    settings = _settings(mock_server, tmp_path)
    settings.server_url = mock_server.ws_url
    settings.heartbeat_interval_s = 1.0
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    ctx.get_or_create_device_id()
    ctx.telemetry = TelemetryEmitter(ctx.device_id)
    ctx.self_healing = SelfHealingRegistry(telemetry=ctx.telemetry)

    persisted_id = "01900000-0000-7000-8000-000000000099"
    store_path = tmp_path / "pending_results.jsonl"
    store_path.write_text(
        json.dumps({
            "protocol_version": "1.0",
            "schema_version": "2026-11-17",
            "message_id": "01900000-0000-7000-8000-000000000098",
            "correlation_id": persisted_id,
            "event_type": "action.result",
            "device_id": ctx.device_id,
            "account_id": "12345678901",
            "source_tool": "trade_close_single_position",
            "status": "executed",
        })
        + "\n",
        encoding="utf-8",
    )

    rest = RestClient(settings, ctx.device_id)
    engine = SyncEngine(rest, ctx)
    chart = ChartService(fetch_candles=lambda *a: [], fetch_ticks=lambda *a: [])
    channel = ServerChannel(
        ctx, EventBus(), rest_client=rest, sync_engine=engine, chart_service=chart,
        dead_after_s=45.0,
    )
    # Construction re-queued the persisted result and recorded the
    # restart recovery in the matrix.
    assert len(channel._pending_results) == 1
    recovery = ctx.self_healing.state(Scenario.BRIDGE_RESTART_MID_OPERATION)
    assert recovery.status == "recovering"
    assert "re-queued 1 persisted" in recovery.last_action

    channel.start()
    try:
        result = await asyncio.to_thread(
            mock_server.state.wait_for_result, persisted_id, timeout=10.0
        )
        assert result is not None, "mock never received the persisted result"
        assert result["correlation_id"] == persisted_id
        assert result["status"] == "executed"
        # Delivered results leave the durable store (safe cleanup).
        deadline = asyncio.get_running_loop().time() + 5.0
        while store_path.exists() and store_path.read_text(
            encoding="utf-8"
        ).strip() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        assert persisted_id not in store_path.read_text(encoding="utf-8")
    finally:
        await channel.stop()
        await rest.close()


@pytest.mark.mock_integration
def test_diagnostics_summary_exposes_telemetry_and_self_healing(tmp_path):
    """Data only (Phase 4 owns the screens): the summary carries the
    telemetry queue state and one entry per self-healing scenario,
    reflecting handling that already ran."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    ctx.telemetry = TelemetryEmitter(ctx.get_or_create_device_id())
    ctx.self_healing = SelfHealingRegistry(telemetry=ctx.telemetry)
    ctx.self_healing.handle(Scenario.MT5_CLOSE, HealingContext(mt5_connected=False))

    app = create_app(ctx)
    http = TestClient(app, raise_server_exceptions=False)
    try:
        resp = http.get(
            "/api/v1/diagnostics/summary",
            headers={"X-API-Key": ctx.settings.local_api_key},
        )
    finally:
        driver.disconnect()
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    telemetry = data["telemetry"]
    assert telemetry["device_id"] == ctx.device_id
    assert telemetry["diagnostic_mode"] is False
    assert telemetry["sink_inert"] is True
    assert telemetry["queue"]["queued"] == 1  # the WARN mt5_close event
    assert telemetry["queue"]["max_items"] == 128

    healing = data["self_healing"]
    assert len(healing) == 15
    assert healing["mt5_close"]["status"] == "degraded"
    assert healing["mt5_close"]["occurrences"] == 1
    assert healing["mt5_close"]["last_error_code"] == "MT5_NOT_RUNNING"
    assert healing["ws_drop"]["status"] == "ok"
    assert healing["ws_drop"]["occurrences"] == 0


@pytest.mark.mock_integration
def test_diagnostics_summary_without_reliability_wired_reports_defaults(tmp_path):
    """A bare context (no lifespan ran) still returns a well-formed
    summary — all 15 scenarios present, never claiming a recovery."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    http = TestClient(app, raise_server_exceptions=False)
    try:
        resp = http.get(
            "/api/v1/diagnostics/summary",
            headers={"X-API-Key": ctx.settings.local_api_key},
        )
    finally:
        driver.disconnect()
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["telemetry"]["sink_inert"] is True
    assert data["telemetry"]["queue"]["queued"] == 0
    assert len(data["self_healing"]) == 15
    assert all(entry["occurrences"] == 0 for entry in data["self_healing"].values())
