"""Unit tests for production wiring (Phase E N1/N2).

Label: unit. No MT5, no network — a fake driver is never connected,
and the lifespan's enrolment/channel loops idle without a server_url.

The journal collector and the durable diagnostic queue existed as
modules, but until Phase E they were never instantiated in production:
``SyncEngine.journal`` stayed None (journal events were never collected
at runtime, so the GUI journal feed was always empty) and
``TelemetryEmitter`` used the in-memory queue (queued ERROR/WARN
diagnostics were lost on a Bridge restart). These tests enter the real
app lifespan (unlike the other GUI tests, which deliberately never run
it) and pin the wiring so a future refactor cannot silently unwire it
again.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.journal.collector import JournalCollector
from nse_bridge.mt5.driver import Mt5Driver
from nse_bridge.reliability.diagnostic_store import DurableDiagnosticQueue

pytestmark = pytest.mark.unit


@pytest.fixture
def live_app(tmp_path):
    """App with the lifespan running (TestClient context entered)."""
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, ctx, app, driver
    driver.disconnect()


def test_lifespan_wires_journal_collector(live_app):
    _, _, app, _ = live_app
    assert isinstance(app.state.sync_engine.journal, JournalCollector)


def test_lifespan_wires_durable_diagnostic_queue(live_app):
    _, _, app, _ = live_app
    assert isinstance(app.state.telemetry.queue, DurableDiagnosticQueue)


def test_wired_journal_collects_snapshot_events(live_app):
    """The wired collector actually collects: baseline emits nothing,
    a new position in the next snapshot emits ``position_opened`` —
    and the engine's ``journal_events()`` (the GUI endpoint's source)
    surfaces it."""
    _, _, app, _ = live_app
    journal = app.state.sync_engine.journal
    before = len(app.state.sync_engine.journal_events())
    journal.diff_snapshots(None, None, [], [])
    journal.diff_snapshots(
        [],
        [],
        [
            {
                "ticket": 424242,
                "symbol": "XAUUSD",
                "type": 0,
                "volume": 0.01,
                "price_open": 2650.5,
                "price_current": 2655.0,
                "sl": 2640.0,
                "tp": 2670.0,
                "profit": 9.0,
                "swap": 0.0,
                "commission": -1.5,
                "magic": 123,
                "time": 1763276400,
                "time_utc": "2026-11-16T09:00:00",
            }
        ],
        [],
    )
    events = app.state.sync_engine.journal_events()
    assert len(events) == before + 1
    assert "position_opened" in [e["event_type"] for e in events]
