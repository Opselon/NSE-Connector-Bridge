"""Unit tests for the GUI — Connector Control Center (nextgen, 9 screens).

Covers: exactly 9 screens (Overview landing + Platform, MetaTrader,
Account, Symbols, Execution, Diagnostics, Updates, Settings), no
forbidden trading-dashboard content, every local API path the GUI's JS
calls exists in the route table, the Account & Subscription endpoint
never leaks the secret, the cookie auth carriage for the loopback GUI
(now covering /ws/ui too — page JS cannot set headers on a WebSocket
upgrade), the guided MT5 diagnostic codes, the updates check, terminal
discovery, and the nextgen structure invariants: theme toggle with
persistence + prefers-color-scheme, live /ws/ui feed usage with the
30 s poll fallback (no 10 s polling loop), masked secrets with password
inputs confined to API-key re-entry (no MT5 password field anywhere),
and the first-run onboarding stepper with its 9 real-result steps.
Label: unit.
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import MT5_PLATFORM_SUPPORTED, Mt5Driver
from nse_bridge.server.errors import ServerError, ServerErrorCode
from nse_bridge.server.rest import Entitlements, ReleaseInfo, ValidateKeyResult

pytestmark = pytest.mark.unit

#: Trading-dashboard content the bridge GUI must never grow. "Overview"
#: and "bot_symbol" were removed from this list deliberately: Overview
#: is now the sanctioned landing screen (a status surface, not a trading
#: dashboard), and bot_symbol is a legitimate Settings configuration
#: field the server reads — neither re-introduces forbidden content.
FORBIDDEN_CONTENT = [
    "AI Agent",
    "Execute proposal",
    "Open Positions",
    "Pending Orders",
    "Backtest",
    "backtest",
    "sparkline",
    "Live Tickers",
]

EXPECTED_SCREENS = {
    "overview",
    "platform",
    "metatrader",
    "account",
    "symbols",
    "execution",
    "diagnostics",
    "updates",
    "settings",
}


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NSE_MT5_TERMINAL_PATH", raising=False)


@pytest.fixture(autouse=True)
def _isolate_host_mt5_install(monkeypatch):
    r"""Hermetic on MT5-equipped hosts (Phase 2b W0): the host's real
    terminal (C:\Program Files, App Paths registry, running
    terminal64.exe, real %APPDATA% data dirs) must never leak into
    fixtures that assert specific diagnostic codes — CI has no MT5,
    a developer machine does. Sources with a per-test fake pass
    through unchanged."""
    from nse_bridge.mt5 import driver as _driver_module

    monkeypatch.setattr(_driver_module, "_standard_terminal_paths", list)

    real_app_paths = _driver_module._app_paths_candidates
    monkeypatch.setattr(
        _driver_module,
        "_app_paths_candidates",
        lambda winreg_module=None: real_app_paths(winreg_module) if winreg_module is not None else [],
    )
    real_running = _driver_module._running_terminal_paths
    monkeypatch.setattr(
        _driver_module,
        "_running_terminal_paths",
        lambda process_list=None: real_running(process_list) if process_list is not None else [],
    )
    real_appdata = _driver_module._appdata_terminal_data_dirs
    monkeypatch.setattr(
        _driver_module,
        "_appdata_terminal_data_dirs",
        lambda appdata_root=None: real_appdata(appdata_root) if appdata_root is not None else [],
    )


@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx, app
    driver.disconnect()


def _auth(ctx):
    return {"X-API-Key": ctx.settings.local_api_key}


class FakeRestClient:
    """Minimal stand-in for the Phase 2 REST client."""

    def __init__(self, *, validate_result=None, validate_error=None, release=None, release_error=None):
        self.validate_result = validate_result
        self.validate_error = validate_error
        self.release = release
        self.release_error = release_error
        self.user_id = None
        self.subscription: dict = {}
        self.entitlements = Entitlements()
        self.session_expires_at = None

    async def validate_key(self):
        if self.validate_error is not None:
            raise self.validate_error
        result = self.validate_result
        self.user_id = result.user_id
        self.subscription = dict(result.subscription)
        self.entitlements = result.entitlements
        return result

    async def releases_latest(self):
        if self.release_error is not None:
            raise self.release_error
        return self.release


# ---------------------------------------------------------------------------
# HTML: screens + forbidden content
# ---------------------------------------------------------------------------
def test_gui_html_has_exactly_nine_screens(client):
    http, _, _ = client
    resp = http.get("/ui")
    assert resp.status_code == 200, resp.text
    html = resp.text
    pages = set(re.findall(r'data-page="([a-z]+)"', html))
    assert pages == EXPECTED_SCREENS, pages
    sections = set(re.findall(r'id="page-([a-z]+)"', html))
    assert sections == EXPECTED_SCREENS, sections
    # Overview is the DEFAULT landing screen (first nav item + active section).
    assert 'data-page="overview"' in html
    assert re.search(r'class="navitem active" data-page="overview"', html)
    assert 'id="page-overview" aria-label="Overview"' in html
    assert re.search(r'class="page active" id="page-overview"', html)


def test_gui_settings_screen_has_all_setting_fields(client):
    # Settings is its own screen in the nextgen layout; every field the
    # PUT /api/v1/settings endpoint accepts (and the mission lists)
    # keeps a stable element id the JS fills from GET /settings.
    http, _, _ = client
    html = http.get("/ui").text
    assert 'id="page-settings"' in html
    assert 'data-page="settings"' in html
    settings_html = html.split('id="page-settings"')[1].split("</section>")[0]
    for element_id in (
        "setBaseUrl",
        "setServerUrl",
        "setEnv",
        "setToken",
        "setLogLevel",
        "setGuiMode",
        "setBotSymbol",
        "setTimeframe",
        "setMagic",
        "setStrictMagic",
        "setTerminalPath",
        "setTerminalDirs",
        "setStreamInt",
        "setBrokerOffset",
        "saveSettings",
    ):
        assert f'id="{element_id}"' in settings_html, f"settings field {element_id} missing from the Settings screen"
    # Secrets are never echoed back: the token input is a masked entry
    # field whose placeholder carries the server-provided masked value.
    assert re.search(r'<input id="setToken" type="password"', settings_html)


def test_gui_overview_is_a_live_status_surface(client):
    # The Overview is powered by /ws/ui push (status_update + account_update)
    # with an initial REST fetch and a 30 s poll fallback — and the old
    # 10 s poll-everything loop is gone.
    http, _, _ = client
    html = http.get("/ui").text
    assert "'status_update'" in html
    assert "'account_update'" in html
    assert "'/ws/ui'" in html
    assert "new WebSocket(" in html
    assert ", 30000);" in html  # the single 30 s light-refresh interval
    assert ", 10000)" not in html  # the old 10 s poll loop is dead
    assert 'id="liveBanner"' in html  # visible "polling" fallback state


def test_gui_theme_toggle_persists_and_respects_os_preference(client):
    http, _, _ = client
    html = http.get("/ui").text
    assert 'id="themeToggle"' in html
    assert "data-theme" in html
    assert "prefers-color-scheme" in html  # OS preference decides first run
    assert "nse-theme" in html  # persisted toggle (localStorage)


def test_gui_password_fields_are_confined_to_secret_entry(client):
    # The ONLY password inputs in the page are the API-key re-entry
    # fields (Settings, Platform quick re-entry, onboarding step 4).
    # The MetaTrader screen never grows an MT5 password field — the
    # bridge never asks for it.
    http, _, _ = client
    html = http.get("/ui").text
    token_ids = re.findall(r'<input id="([A-Za-z]+)" type="password"', html)
    assert set(token_ids) == {"setToken", "platToken", "obToken"}, token_ids
    mt = html.split('id="page-metatrader"')[1].split('id="page-account"')[0]
    assert 'type="password"' not in mt
    # The MT5 note states the honesty rule explicitly.
    assert "never asks for your MT5 password" in mt


def test_gui_masks_secrets_and_never_renders_raw_tokens(client):
    # The GUI renders the server-provided MASKED values (server_token_masked /
    # api_key_masked) — raw key material is never displayed anywhere.
    http, _, _ = client
    html = http.get("/ui").text
    assert "server_token_masked" in html
    assert "api_key_masked" in html
    # No client-side re-entry path ever echoes a stored secret back.
    assert 'type="text" id="setToken"' not in html


def test_gui_onboarding_flow_present_with_real_steps(client):
    # First-run guided setup: 9 steps, each running a REAL bridge action
    # (no simulated success), gated by prerequisites, skippable for
    # standalone use.
    http, _, _ = client
    html = http.get("/ui").text
    assert 'id="onboardingOverlay"' in html
    steps = set(re.findall(r'class="ob-step" data-step="([a-z0-9]+)"', html))
    assert steps == {
        "init",
        "mt5",
        "endpoint",
        "key",
        "validate",
        "register",
        "connect",
        "sync",
        "health",
    }, steps
    assert 'id="obSkip"' in html  # skippable for standalone use
    assert 'id="obFinish"' in html
    assert "nse_onboarding_dismissed" in html  # dismissal persists
    # The endpoint step hydrates from the effective settings — it never
    # invents a URL client-side.
    assert "obHydrateEndpoint" in html


def test_gui_platform_screen_has_channel_control_and_preflight_checks(client):
    http, _, _ = client
    html = http.get("/ui").text
    plat = html.split('id="page-platform"')[1].split('id="page-metatrader"')[0]
    assert "/api/v1/server_channel/restart" in html
    assert "/api/v1/run_connection_checks" in html
    assert 'id="btnRestartChannel"' in plat
    assert 'id="btnRunChecks"' in plat
    assert 'id="checkList"' in plat
    assert 'id="platToken"' in plat  # masked key re-entry on Platform


def test_gui_updates_screen_is_fail_closed_honest(client):
    http, _, _ = client
    html = http.get("/ui").text
    upd = html.split('id="page-updates"')[1].split('id="page-settings"')[0]
    assert "/api/v1/updates/check" in html
    assert "/api/v1/updates/download" in html
    assert 'id="upRestartBanner"' in upd  # restart-required banner
    assert "NO_PUBLIC_KEY" in html  # updater error codes shown with meaning


def test_gui_html_has_no_forbidden_content(client):
    http, _, _ = client
    html = http.get("/ui").text
    for phrase in FORBIDDEN_CONTENT:
        assert phrase not in html, f"forbidden content present: {phrase!r}"


def test_gui_html_never_embeds_the_local_api_key(client):
    http, ctx, _ = client
    html = http.get("/ui").text
    assert ctx.settings.local_api_key
    assert ctx.settings.local_api_key not in html


# ---------------------------------------------------------------------------
# Route table: every path the GUI calls must exist
# ---------------------------------------------------------------------------
def test_gui_api_paths_exist_in_route_table(client):
    http, _, app = client
    html = http.get("/ui").text
    called = set(re.findall(r"/api/v1/[A-Za-z0-9_/\-]+", html))
    assert called, "no API paths found in the GUI"
    # The management router is included lazily (an _IncludedRouter sits
    # in app.routes until first use); the published OpenAPI paths are
    # the authoritative route table.
    routes = set(app.openapi()["paths"].keys())
    missing = sorted(p for p in called if p not in routes)
    assert not missing, f"GUI calls unknown local API paths: {missing}"


# ---------------------------------------------------------------------------
# Cookie auth carriage (loopback GUI, no exemption)
# ---------------------------------------------------------------------------
def test_gui_cookie_authenticates_api_calls(client):
    http, _, _ = client
    resp = http.get("/ui")
    assert resp.status_code == 200
    # No X-API-Key header — the cookie set by /ui must carry the key.
    resp = http.get("/api/v1/settings")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


def test_new_endpoints_require_auth_without_cookie_or_key(client):
    http, _, _ = client
    for method, path in [
        ("GET", "/api/v1/account/subscription"),
        ("POST", "/api/v1/account/validate-key"),
        ("GET", "/api/v1/updates/latest"),
        ("GET", "/api/v1/diagnostics/summary"),
        ("GET", "/api/v1/mt5/terminals"),
        ("GET", "/api/v1/symbols/inventory"),
        ("POST", "/api/v1/symbols/resolve"),
        ("POST", "/api/v1/symbols/refresh"),
        ("GET", "/api/v1/execution/recent"),
        ("GET", "/api/v1/journal/events"),
    ]:
        resp = http.request(method, path)
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code}"


# ---------------------------------------------------------------------------
# Account & Subscription: masked key status, never the secret
# ---------------------------------------------------------------------------
def _validated_client(client):
    http, ctx, _ = client
    secret = "sk-test-secret-abcdef123456"
    ctx.settings.server_token = secret
    fake = FakeRestClient(
        validate_result=ValidateKeyResult(
            valid=True,
            user_id="user-123",
            subscription={
                "plan": "Pro",
                "tariff": "monthly",
                "billing_status": "active",
                "expires_at": "2027-01-01T00:00:00Z",
            },
            entitlements=Entitlements(history_sync=True, position_manager=True, max_devices=3, max_accounts=2),
        )
    )
    fake.session_expires_at = "2026-11-19T00:00:00Z"
    ctx.rest_client = fake
    return http, ctx, secret


def test_account_subscription_masks_secret(client):
    http, ctx, secret = _validated_client(client)
    resp = http.post("/api/v1/account/validate-key", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    resp = http.get("/api/v1/account/subscription", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert secret not in json.dumps(resp.json()), "secret leaked in subscription payload"
    assert data["validated"] is True
    assert data["user_id"] == "user-123"
    assert data["subscription"]["plan"] == "Pro"
    assert data["subscription"]["tariff"] == "monthly"
    assert data["entitlements"]["position_manager"] is True
    assert data["entitlements"]["max_devices"] == 3
    assert data["api_key_set"] is True
    assert data["api_key_masked"].endswith(secret[-4:])
    assert secret not in data["api_key_masked"]
    # GET /settings masks the same secret too.
    settings = http.get("/api/v1/settings", headers=_auth(ctx)).json()["data"]
    assert secret not in json.dumps(settings)
    assert settings["server_token_masked"].endswith(secret[-4:])


def test_account_subscription_unvalidated_state(client):
    http, ctx, _ = client
    resp = http.get("/api/v1/account/subscription", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["validated"] is False
    assert data["user_id"] is None
    assert data["subscription"] == {}
    assert data["api_key_set"] is False
    assert "server_token" not in json.dumps(data)


def test_validate_key_failure_stores_error_without_secret(client):
    http, ctx, secret = _validated_client(client)
    ctx.rest_client = FakeRestClient(
        validate_error=ServerError(ServerErrorCode.KEY_INVALID, "bad key", http_status=401)
    )
    resp = http.post("/api/v1/account/validate-key", headers=_auth(ctx))
    assert resp.status_code == 401, resp.text
    data = http.get("/api/v1/account/subscription", headers=_auth(ctx)).json()["data"]
    assert data["validated"] is False
    assert data["validation_error"]
    assert secret not in json.dumps(data)


# ---------------------------------------------------------------------------
# Updates & Releases
# ---------------------------------------------------------------------------
def test_updates_latest_reports_newer_version(client):
    http, ctx, _ = client
    ctx.settings.server_token = "sk-test-secret-abcdef123456"
    ctx.rest_client = FakeRestClient(
        release=ReleaseInfo(
            version="99.99.99",
            download_url="https://example.invalid/bridge-99.99.99.exe",
            sha256="ab" * 32,
            size_bytes=12_345_678,
            min_connector_version="1.0.0",
            signature="c2lnbmF0dXJl",
            released_at="2026-11-18T00:00:00Z",
        )
    )
    resp = http.get("/api/v1/updates/latest", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # The installed version IS the single-source-of-truth version —
    # never a hardcoded copy (this test broke on the 1.1.0 bump).
    from nse_bridge import BRIDGE_VERSION

    assert data["installed_version"] == BRIDGE_VERSION
    assert data["latest"]["version"] == "99.99.99"
    assert data["update_available"] is True
    assert data["status"] == "update_available"
    assert data["latest"]["signature_present"] is True


def test_updates_latest_check_failed_and_not_configured(client):
    http, ctx, _ = client
    # No API key configured -> honest not_configured, no server call.
    ctx.rest_client = FakeRestClient(release_error=AssertionError("must not be called"))
    resp = http.get("/api/v1/updates/latest", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "not_configured"
    # Key configured but the server fails -> check_failed, never a fake version.
    ctx.settings.server_token = "sk-test-secret-abcdef123456"
    ctx.rest_client = FakeRestClient(
        release_error=ServerError(ServerErrorCode.SERVER_UNAVAILABLE, "server down", http_status=503)
    )
    resp = http.get("/api/v1/updates/latest", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "check_failed"
    assert data["latest"] is None
    assert data["update_available"] is False


# ---------------------------------------------------------------------------
# Terminal discovery + open
# ---------------------------------------------------------------------------
def test_mt5_terminals_discovery_with_env_path(client, tmp_path, monkeypatch):
    http, ctx, _ = client
    fake_terminal = tmp_path / "terminal64.exe"
    fake_terminal.write_bytes(b"MZ")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(fake_terminal))
    resp = http.get("/api/v1/mt5/terminals", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    found = {t["path"]: t for t in data["terminals"]}
    assert str(fake_terminal) in found
    assert found[str(fake_terminal)]["exists"] is True
    assert found[str(fake_terminal)]["selected"] is True
    assert data["selected_path"] == str(fake_terminal)
    assert data["can_open"] == (sys.platform == "win32")


def test_mt5_terminals_open_rejects_unknown_path_and_linux(client):
    http, ctx, _ = client
    resp = http.post(
        "/api/v1/mt5/terminals/open",
        json={"path": r"C:\nowhere\terminal64.exe"},
        headers=_auth(ctx),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Guided MT5 diagnostics: structured codes
# ---------------------------------------------------------------------------
def _summary(http, ctx):
    resp = http.get("/api/v1/diagnostics/summary", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_diagnostics_summary_mt5_not_found(client):
    http, ctx, _ = client
    data = _summary(http, ctx)
    # v1.3.0 honest gate: on a platform that cannot load the IPC driver
    # (Linux/macOS/native-ARM64 Windows) there is no terminal to find —
    # the code is MT5_WINDOWS_REQUIRED, not MT5_NOT_FOUND. On a
    # supported platform (Windows x64) the old code still applies.
    expected = "MT5_WINDOWS_REQUIRED" if not MT5_PLATFORM_SUPPORTED else "MT5_NOT_FOUND"
    assert data["diagnostic"]["code"] == expected
    assert data["diagnostic"]["severity"] == "error"
    assert data["diagnostic"]["explanation"]
    assert data["diagnostic"]["recovery"]


def test_diagnostics_summary_mt5_not_running(client, tmp_path, monkeypatch):
    http, ctx, _ = client
    fake_terminal = tmp_path / "terminal64.exe"
    fake_terminal.write_bytes(b"MZ")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(fake_terminal))
    data = _summary(http, ctx)
    # Same v1.3.0 gate as above: a "found but not running" terminal is
    # only a meaningful state where a terminal CAN attach.
    expected = "MT5_WINDOWS_REQUIRED" if not MT5_PLATFORM_SUPPORTED else "MT5_NOT_RUNNING"
    assert data["diagnostic"]["code"] == expected


def test_diagnostics_summary_mt5_ipc_failed(client, tmp_path, monkeypatch):
    http, ctx, _ = client
    fake_terminal = tmp_path / "terminal64.exe"
    fake_terminal.write_bytes(b"MZ")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(fake_terminal))
    ctx.driver._diagnostic_code = None
    ctx.driver._set_state("TERMINAL_ERROR", "initialize failed")
    data = _summary(http, ctx)
    # Same v1.3.0 gate as above: IPC failure is only meaningful where
    # IPC can exist. The TERMINAL_ERROR state is still recorded on the
    # driver; the user-facing code is the platform gate.
    expected = "MT5_WINDOWS_REQUIRED" if not MT5_PLATFORM_SUPPORTED else "MT5_IPC_FAILED"
    assert data["diagnostic"]["code"] == expected


def test_diagnostics_summary_mt5_account_not_ready(client):
    http, ctx, _ = client
    ctx.driver._diagnostic_code = "MT5_ACCOUNT_NOT_READY"
    data = _summary(http, ctx)
    assert data["diagnostic"]["code"] == "MT5_ACCOUNT_NOT_READY"
    assert "password" in data["diagnostic"]["explanation"].lower() or any(
        "password" in step.lower() for step in data["diagnostic"]["recovery"]
    )


def test_diagnostics_summary_mt5_windows_required(client):
    """v1.3.0: on a platform without the IPC driver the summary says so.

    Skipped on a supported platform (Windows x64), where the code can
    never appear. The recovery steps must point at the Windows x64
    build — never at installing a terminal on this machine.
    """
    if MT5_PLATFORM_SUPPORTED:
        pytest.skip("MT5_WINDOWS_REQUIRED only exists where the driver cannot load")
    http, ctx, _ = client
    data = _summary(http, ctx)
    assert data["diagnostic"]["code"] == "MT5_WINDOWS_REQUIRED"
    assert data["diagnostic"]["severity"] == "error"
    assert "Windows" in data["diagnostic"]["explanation"]
    assert any("Windows" in step for step in data["diagnostic"]["recovery"])


def test_mt5_terminals_reports_platform_supported(client):
    http, ctx, _ = client
    resp = http.get("/api/v1/mt5/terminals", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["platform_supported"] is MT5_PLATFORM_SUPPORTED
    assert data["can_open"] == (sys.platform == "win32")


def test_diagnostics_summary_mt5_unsupported_version(client):
    http, ctx, _ = client
    ctx.driver._connected = True
    ctx.driver._pkg_version = "500.100.2020-01-01"
    ctx.driver._set_state("CONNECTED", "connected")
    data = _summary(http, ctx)
    assert data["diagnostic"]["code"] == "MT5_UNSUPPORTED_VERSION"
    assert data["diagnostic"]["severity"] == "warning"


def test_diagnostics_summary_healthy_when_connected_current_build(client):
    http, ctx, _ = client
    ctx.driver._connected = True
    ctx.driver._pkg_version = "500.4750.2025-01-01"
    ctx.driver._set_state("CONNECTED", "connected")
    # Wire a real SyncEngine so the summary's sync branch is exercised
    # (checkpoint.iso is a property, not a method).
    from nse_bridge.server.sync import SyncEngine

    ctx.sync_engine = SyncEngine(FakeRestClient(), ctx)
    data = _summary(http, ctx)
    assert data["diagnostic"] is None
    assert data["bridge"]["healthy"] is False  # no live terminal probe on Linux
    assert data["sync"]["available"] is True
    assert data["sync"]["pending_batches"] == 0
    assert isinstance(data["sync"]["checkpoint"], str)


# ---------------------------------------------------------------------------
# Settings: environment selection flags restart
# ---------------------------------------------------------------------------
def test_settings_environment_change_flags_restart(client):
    http, ctx, _ = client
    resp = http.put(
        "/api/v1/settings",
        json={"environment": "staging", "server_base_url": "http://127.0.0.1:9"},
        headers=_auth(ctx),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["restart_required"] is True
    assert "environment" in data["changed"]


def test_settings_rejects_http_base_url_in_production(client):
    http, ctx, _ = client
    resp = http.put(
        "/api/v1/settings",
        json={"server_base_url": "http://example.invalid"},
        headers=_auth(ctx),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["ok"] is False
