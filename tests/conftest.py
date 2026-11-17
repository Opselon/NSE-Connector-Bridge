"""Shared e2e fixtures: real app + real MT5 terminal (session-scoped)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.logging_setup import setup_logging
from nse_bridge.mt5.driver import Mt5Driver


@pytest.fixture(scope="session")
def ctx():
    import tempfile

    settings = BridgeSettings(
        log_dir=".pytest-logs",
        log_level="INFO",
        port=8788,
    )
    # isolate config persistence — never write the real user config during tests
    settings.data_dir = Path(tempfile.mkdtemp(prefix="nse-bridge-test-"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    driver = Mt5Driver(settings)
    context = BridgeContext(settings=settings, driver=driver)
    setup_logging(settings.resolved_log_dir, settings.log_level, extra_sink=context.logs.sink)
    context.get_or_create_device_id()
    try:  # partial runs (e.g. only cat4) still need a live connection
        driver.connect()
    except Exception:
        pass
    yield context
    driver.disconnect()


@pytest.fixture(scope="session")
def app(ctx):
    return create_app(ctx)


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c


def call(client: TestClient, name: str, params: dict | None = None, *, expect_ok: bool = True):
    """POST a command through the real HTTP layer and unwrap the envelope."""
    resp = client.post(f"/api/v1/{name}", json=params or {})
    assert resp.status_code == 200, f"{name}: HTTP {resp.status_code}: {resp.text[:500]}"
    body = resp.json()
    if expect_ok:
        assert body["ok"] is True, f"{name}: business error: {body}"
        return body["data"]
    assert body["ok"] is False, f"{name}: expected failure envelope, got {body}"
    return body["error"]
