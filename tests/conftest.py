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
        # A deployed device carries its chart-context configuration
        # (Phase 2b: the chart-context commands read these — a fresh
        # unconfigured env returns an empty timeframe / 422, which is
        # truthful but not what a real deployment looks like).
        bot_symbol="XAUUSD",
        default_timeframe="M1",
    )
    # isolate config persistence — never write the real user config during tests
    settings.data_dir = Path(tempfile.mkdtemp(prefix="nse-bridge-test-"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    driver = Mt5Driver(settings)
    context = BridgeContext(settings=settings, driver=driver)
    setup_logging(settings.resolved_log_dir, settings.log_level, extra_sink=context.logs.sink)
    context.get_or_create_device_id()
    # TEST HONESTY (PLAN addendum §7 / ARCHITECTURE-CONTRACT §9): a
    # required-MT5 test MUST FAIL — never silently skip or pass — when
    # no live terminal is available. The old code swallowed the
    # connect failure with try/except-pass, which let the whole e2e
    # suite report green on a machine with no MT5. On Linux (no
    # MetaTrader5 package) driver.connect() raises ModuleNotFoundError;
    # on Windows without a running terminal it returns ok=False.
    # Either way the session fixture fails loudly here, so every e2e
    # test that depends on it ERRORS at setup instead of passing.
    try:
        summary = driver.connect()
    except Exception as exc:  # noqa: BLE001 — any connect failure is a loud test failure, not a skip
        pytest.fail(
            "MT5 terminal unavailable — required-MT5 test cannot run: "
            f"driver.connect() raised {type(exc).__name__}: {exc}",
            pytrace=False,
        )
    if not summary.get("ok"):
        pytest.fail(
            f"MT5 terminal unavailable — required-MT5 test cannot run: {summary.get('error')}",
            pytrace=False,
        )
    yield context
    driver.disconnect()


@pytest.fixture(scope="session")
def app(ctx):
    return create_app(ctx)


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c


def auth_headers(client: TestClient) -> dict[str, str]:
    """The app's own local API key (set by create_app → ensure_local_api_key).

    The mandatory X-API-Key gate rejects every unauthenticated /api/v1
    call with 401 — the e2e suite runs only on machines WITH a live
    terminal, so every direct client.post/get in the e2e files must
    carry this header (Phase 2b fix: it previously never did, and the
    suite could never go green where it can actually run).
    """
    ctx = getattr(client.app.state, "ctx", None)
    key = getattr(getattr(ctx, "settings", None), "local_api_key", "") or ""
    return {"X-API-Key": key} if key else {}


def call(client: TestClient, name: str, params: dict | None = None, *, expect_ok: bool = True):
    """POST a command through the real HTTP layer and unwrap the envelope."""
    resp = client.post(f"/api/v1/{name}", json=params or {}, headers=auth_headers(client))
    assert resp.status_code == 200, f"{name}: HTTP {resp.status_code}: {resp.text[:500]}"
    body = resp.json()
    if expect_ok:
        assert body["ok"] is True, f"{name}: business error: {body}"
        return body["data"]
    assert body["ok"] is False, f"{name}: expected failure envelope, got {body}"
    return body["error"]
