"""Unit tests for Phase 5 multi-terminal MT5 discovery.

Covers: enumeration from every source (env, standard, registry,
custom/portable directories, running processes), dedup, version
detection (pure-Python PE resource parser — Linux-runnable), and the
Linux safety rules (no winreg import, no process enumeration, no
crash when the registry APIs are absent). The GET /api/v1/mt5/terminals
payload shape (path, version, running, selected) is covered too.
Label: unit.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nse_bridge.api.app import create_app
from nse_bridge.config import BridgeSettings
from nse_bridge.engine.common import BridgeContext
from nse_bridge.mt5.driver import (
    Mt5Driver,
    _pe_file_version,
    _terminal_version,
    auto_discover_terminal_path,
    discover_terminals,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NSE_MT5_TERMINAL_PATH", raising=False)
    monkeypatch.delenv("MT5_PATH", raising=False)


@pytest.fixture
def settings(tmp_path):
    return BridgeSettings(_env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs"))


def _fake_terminal(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x00" * 62)
    return path


# ---------------------------------------------------------------------------
# synthetic PE with a version resource (for _pe_file_version)
# ---------------------------------------------------------------------------
def _build_versioned_pe(version=(5, 0, 4750, 0)):
    """Minimal PE32+ with one .rsrc section carrying VS_VERSION_INFO."""
    key = "VS_VERSION_INFO\0".encode("utf-16-le")
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,
        (version[0] << 16) | version[1],
        (version[2] << 16) | version[3],
        0, 0, 0, 0, 0, 0, 0, 0, 0,
    )
    blob = struct.pack("<HHH", 92, 52, 0) + key + b"\x00\x00" + fixed
    assert len(blob) == 92

    def rsrc_dir(entries):
        hdr = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entries))
        body = b"".join(
            struct.pack("<II", name, (0x80000000 | target) if is_dir else target)
            for name, target, is_dir in entries
        )
        return hdr + body

    root = rsrc_dir([(16, 0x1018, True)])
    type_dir = rsrc_dir([(1, 0x1030, True)])
    name_dir = rsrc_dir([(0x0409, 0x1048, False)])
    data_entry = struct.pack("<IIII", 0x1058, len(blob), 0, 0)
    rsrc = root + type_dir + name_dir + data_entry + blob
    assert len(rsrc) == 0xB4  # root/type/name at 0x200/0x218/0x230, entry 0x248, blob 0x258

    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = bytearray(0xF0)
    struct.pack_into("<H", opt, 0, 0x20B)
    struct.pack_into("<I", opt, 108, 16)  # NumberOfRvaAndSizes
    struct.pack_into("<II", opt, 112 + 2 * 8, 0x1000, 0xB4)  # resource dir RVA/size
    sec = struct.pack("<8sIIIIIIHHI", b".rsrc\0\0\0", 0xB4, 0x1000, 0xB4, 0x200, 0, 0, 0, 0, 0x40000040)
    header = bytes(dos) + b"PE\0\0" + coff + bytes(opt) + sec
    return header + b"\x00" * (0x200 - len(header)) + rsrc


def test_pe_file_version_detects_version(tmp_path):
    pe = tmp_path / "terminal64.exe"
    pe.write_bytes(_build_versioned_pe((5, 0, 4750, 0)))
    assert _pe_file_version(str(pe)) == "5.0.4750.0"
    assert _terminal_version(str(pe)) == "5.0.4750.0"


def test_pe_file_version_returns_none_for_non_pe(tmp_path):
    fake = _fake_terminal(tmp_path / "terminal64.exe")
    assert _pe_file_version(str(fake)) is None
    text = tmp_path / "other.exe"
    text.write_bytes(b"not a PE file at all")
    assert _pe_file_version(str(text)) is None
    assert _pe_file_version(str(tmp_path / "missing.exe")) is None


# ---------------------------------------------------------------------------
# fake winreg (App Paths)
# ---------------------------------------------------------------------------
class _FakeKey:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    HKEY_LOCAL_MACHINE = 1
    HKEY_CURRENT_USER = 2

    def __init__(self, mapping):
        self._mapping = mapping  # (root, subkey) -> path

    def OpenKey(self, root, subkey):
        if (root, subkey) in self._mapping:
            return _FakeKey(self._mapping[(root, subkey)])
        raise OSError("no such key")

    def QueryValueEx(self, key, name):
        return (key._value, 1)


def _app_paths_subkey(exe):
    return rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def test_discover_terminals_empty_on_linux_without_sources(settings):
    # No env, no config, no registry on Linux, no Windows paths: empty,
    # and no crash from the absent winreg / process APIs.
    assert discover_terminals(settings) == []
    assert auto_discover_terminal_path() is None


def test_discover_terminals_env_path(settings, tmp_path, monkeypatch):
    fake = _fake_terminal(tmp_path / "terminal64.exe")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(fake))
    found = discover_terminals(settings)
    assert len(found) == 1
    term = found[0]
    assert term.path == str(fake)
    assert term.source == "env"
    assert term.exists is True
    assert term.running is False
    assert term.version is None  # stub MZ file carries no version resource
    assert auto_discover_terminal_path() == str(fake)


def test_discover_terminals_multiple_sources(settings, tmp_path, monkeypatch):
    env_term = _fake_terminal(tmp_path / "env" / "terminal64.exe")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(env_term))
    custom_a = _fake_terminal(tmp_path / "custom-a" / "terminal64.exe")
    custom_b = _fake_terminal(tmp_path / "custom-b" / "terminal.exe")
    reg_term = _fake_terminal(tmp_path / "reg" / "terminal64.exe")
    fake_reg = _FakeWinreg(
        {(1, _app_paths_subkey("terminal64.exe")): str(reg_term)}
    )
    settings.mt5_custom_terminal_dirs = f"{tmp_path / 'custom-a'},{tmp_path / 'custom-b'}"
    found = discover_terminals(
        settings, running_paths=[str(custom_b)], winreg_module=fake_reg
    )
    by_path = {t.path: t for t in found}
    assert set(by_path) == {str(env_term), str(custom_a), str(custom_b), str(reg_term)}
    assert by_path[str(env_term)].source == "env"
    assert by_path[str(custom_a)].source == "custom"
    assert by_path[str(custom_b)].source == "custom"
    assert by_path[str(reg_term)].source == "registry"
    assert by_path[str(custom_b)].running is True
    assert by_path[str(env_term)].running is False


def test_discover_terminals_portable_subdir(settings, tmp_path):
    portable = _fake_terminal(tmp_path / "portable" / "Broker MT5" / "terminal64.exe")
    found = discover_terminals(settings, extra_dirs=[str(tmp_path / "portable")])
    assert len(found) == 1
    assert found[0].path == str(portable)
    assert found[0].source == "portable"


def test_discover_terminals_dedupes_same_path(settings, tmp_path):
    term = _fake_terminal(tmp_path / "dup" / "terminal64.exe")
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "dup"), str(tmp_path / "dup")]
    )
    assert [t.path for t in found] == [str(term)]


def test_discover_terminals_running_process_becomes_candidate(settings, tmp_path):
    running = _fake_terminal(tmp_path / "running" / "terminal64.exe")
    found = discover_terminals(settings, running_paths=[str(running)])
    assert len(found) == 1
    assert found[0].path == str(running)
    assert found[0].source == "running"
    assert found[0].running is True


def test_discover_terminals_never_overwrites_configured_path(settings, tmp_path):
    configured = _fake_terminal(tmp_path / "cfg" / "terminal64.exe")
    settings.mt5_terminal_path = str(configured)
    found = discover_terminals(settings)
    assert len(found) == 1
    assert found[0].path == str(configured)
    assert found[0].source == "configured"
    assert settings.mt5_terminal_path == str(configured)  # unchanged


def test_discover_terminals_version_from_pe(settings, tmp_path):
    pe = tmp_path / "ver" / "terminal64.exe"
    pe.parent.mkdir(parents=True, exist_ok=True)
    pe.write_bytes(_build_versioned_pe((5, 0, 4750, 0)))
    found = discover_terminals(settings, extra_dirs=[str(tmp_path / "ver")])
    assert len(found) == 1
    assert found[0].version == "5.0.4750.0"


def test_discover_terminals_ignores_missing_custom_dirs(settings, tmp_path):
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "does-not-exist")]
    )
    assert found == []


# ---------------------------------------------------------------------------
# endpoint payload
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(
        _env_file=None, data_dir=tmp_path, log_dir=str(tmp_path / "logs")
    )
    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    app = create_app(ctx)
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client, ctx, app
    driver.disconnect()


def _auth(ctx):
    return {"X-API-Key": ctx.settings.local_api_key}


def test_mt5_terminals_endpoint_reports_version_running_selected(client, tmp_path, monkeypatch):
    http, ctx, _ = client
    env_term = _fake_terminal(tmp_path / "env" / "terminal64.exe")
    monkeypatch.setenv("NSE_MT5_TERMINAL_PATH", str(env_term))
    custom = _fake_terminal(tmp_path / "custom" / "terminal64.exe")
    ctx.settings.mt5_custom_terminal_dirs = str(tmp_path / "custom")
    resp = http.get("/api/v1/mt5/terminals", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    by_path = {t["path"]: t for t in data["terminals"]}
    assert set(by_path) == {str(env_term), str(custom)}
    for term in by_path.values():
        assert set(term) >= {"path", "version", "running", "selected", "exists", "source", "data_dir"}
        assert term["running"] is False
    assert by_path[str(env_term)]["selected"] is True
    assert by_path[str(env_term)]["source"] == "env"
    assert by_path[str(custom)]["selected"] is False
    assert data["selected_path"] == str(env_term)
    assert data["can_open"] == (sys.platform == "win32")


# ---------------------------------------------------------------------------
# terminal data directories (spec §3, gap §5 Phase B2)
# ---------------------------------------------------------------------------
def _portable_terminal(path):
    """Fake terminal whose exe dir carries live data-dir markers."""
    term = _fake_terminal(path)
    (path.parent / "MQL5").mkdir(parents=True, exist_ok=True)
    (path.parent / "Logs").mkdir(parents=True, exist_ok=True)
    return term


def test_discover_terminals_portable_data_dir(settings, tmp_path):
    base = tmp_path / "portable" / "Broker MT5"
    term = _portable_terminal(base / "terminal64.exe")
    found = discover_terminals(settings, extra_dirs=[str(tmp_path / "portable")])
    assert len(found) == 1
    assert found[0].path == str(term)
    assert found[0].source == "portable"
    assert found[0].data_dir == str(base)


def test_discover_terminals_data_dir_none_without_markers(settings, tmp_path):
    _fake_terminal(tmp_path / "plain" / "terminal64.exe")
    found = discover_terminals(settings, extra_dirs=[str(tmp_path / "plain")])
    assert len(found) == 1
    assert found[0].data_dir is None


def test_discover_terminals_data_dir_needs_both_markers_as_dirs(settings, tmp_path):
    base = tmp_path / "partial"
    _fake_terminal(base / "terminal64.exe")
    (base / "MQL5").mkdir()
    # Logs missing entirely -> not a data dir.
    found = discover_terminals(settings, extra_dirs=[str(base)])
    assert found[0].data_dir is None
    # MQL5 as a FILE (not a directory) -> still not a data dir.
    (base / "MQL5").rmdir()
    (base / "MQL5").write_text("not a directory")
    (base / "Logs").mkdir()
    found = discover_terminals(settings, extra_dirs=[str(base)])
    assert found[0].data_dir is None


def _appdata_tree(tmp_path, *hash_names):
    """Fake %APPDATA%\\MetaQuotes\\Terminal tree; each hash-named dir
    gets an MQL5 folder (i.e. it really is a data directory)."""
    root = tmp_path / "AppData" / "MetaQuotes" / "Terminal"
    dirs = []
    for name in hash_names:
        d = root / name
        (d / "MQL5").mkdir(parents=True)
        dirs.append(d)
    return root, dirs


def test_discover_terminals_appdata_data_dir_single_terminal(settings, tmp_path):
    _fake_terminal(tmp_path / "term" / "terminal64.exe")
    root, (data,) = _appdata_tree(tmp_path, "D0E8209F77C8CF37AD8BF550E51FF075")
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "term")], appdata_root=str(root)
    )
    assert len(found) == 1
    assert found[0].data_dir == str(data)


def test_discover_terminals_appdata_data_dir_ambiguous_two_terminals(settings, tmp_path):
    _fake_terminal(tmp_path / "a" / "terminal64.exe")
    _fake_terminal(tmp_path / "b" / "terminal64.exe")
    root, _ = _appdata_tree(tmp_path, "D0E8209F77C8CF37AD8BF550E51FF075")
    found = discover_terminals(
        settings,
        extra_dirs=[str(tmp_path / "a"), str(tmp_path / "b")],
        appdata_root=str(root),
    )
    assert len(found) == 2
    # One data dir cannot be attributed to either of two terminals.
    assert all(t.data_dir is None for t in found)


def test_discover_terminals_appdata_data_dir_ambiguous_two_dirs(settings, tmp_path):
    _fake_terminal(tmp_path / "term" / "terminal64.exe")
    root, _ = _appdata_tree(
        tmp_path,
        "D0E8209F77C8CF37AD8BF550E51FF075",
        "A1B2C3D4E5F60718293A4B5C6D7E8F90A",
    )
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "term")], appdata_root=str(root)
    )
    assert len(found) == 1
    assert found[0].data_dir is None


def test_discover_terminals_appdata_dir_without_mql5_ignored(settings, tmp_path):
    _fake_terminal(tmp_path / "term" / "terminal64.exe")
    root = tmp_path / "AppData" / "MetaQuotes" / "Terminal"
    (root / "D0E8209F77C8CF37AD8BF550E51FF075").mkdir(parents=True)  # no MQL5 inside
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "term")], appdata_root=str(root)
    )
    assert len(found) == 1
    assert found[0].data_dir is None


def test_discover_terminals_portable_data_dir_wins_over_appdata(settings, tmp_path):
    base = tmp_path / "portable" / "Broker MT5"
    _portable_terminal(base / "terminal64.exe")
    root, _ = _appdata_tree(tmp_path, "D0E8209F77C8CF37AD8BF550E51FF075")
    found = discover_terminals(
        settings, extra_dirs=[str(tmp_path / "portable")], appdata_root=str(root)
    )
    assert len(found) == 1
    assert found[0].data_dir == str(base)


def test_mt5_terminals_endpoint_reports_data_dir(client, tmp_path):
    http, ctx, _ = client
    base = tmp_path / "portable" / "Broker MT5"
    _portable_terminal(base / "terminal64.exe")
    ctx.settings.mt5_custom_terminal_dirs = str(tmp_path / "portable")
    resp = http.get("/api/v1/mt5/terminals", headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["terminals"]) == 1
    assert data["terminals"][0]["data_dir"] == str(base)


# ---------------------------------------------------------------------------
# broker-offset probe symbols: broker-discovered, never hardcoded (spec §4)
# ---------------------------------------------------------------------------
class _FakeMt5:
    """Fake MetaTrader5 module for offset detection.

    Fixture symbol names (DISC1/DISC2/OPS1) are invented test labels,
    never real broker symbols — the point is the driver must use
    whatever names the broker reports via symbols_get().
    """

    def __init__(self, ticks, visible=None, fail_symbols_get=False):
        self._ticks = dict(ticks)  # name -> tick epoch seconds
        self._visible = set(self._ticks) if visible is None else set(visible)
        self._fail_symbols_get = fail_symbols_get
        self.symbols_get_called = False
        self.tick_calls: list[str] = []

    def symbols_get(self):
        self.symbols_get_called = True
        if self._fail_symbols_get:
            raise RuntimeError("symbols_get failed")
        return [
            SimpleNamespace(name=name, visible=name in self._visible)
            for name in self._ticks
        ]

    def symbol_info_tick(self, name):
        self.tick_calls.append(name)
        epoch = self._ticks.get(name)
        return SimpleNamespace(time=epoch) if epoch else None


def test_detect_broker_offset_uses_broker_discovered_symbols(settings):
    driver = Mt5Driver(settings)
    try:
        now = int(time.time())
        fake = _FakeMt5({"DISC1": now + 120 * 60, "DISC2": now + 60 * 60})
        driver._detect_broker_offset(fake)
        assert fake.symbols_get_called is True
        assert set(fake.tick_calls) <= {"DISC1", "DISC2"}
        assert fake.tick_calls, "at least one discovered symbol must be probed"
        assert settings.broker_utc_offset_minutes == 120
    finally:
        driver.disconnect()


def test_detect_broker_offset_prefers_configured_bot_symbol_first(settings):
    settings.bot_symbol = "OPS1"
    driver = Mt5Driver(settings)
    try:
        now = int(time.time())
        fake = _FakeMt5({"OPS1": now + 30 * 60, "DISC1": now + 120 * 60})
        driver._detect_broker_offset(fake)
        assert fake.tick_calls[0] == "OPS1"
        assert settings.broker_utc_offset_minutes == 120
    finally:
        driver.disconnect()


def test_detect_broker_offset_prefers_visible_symbols(settings):
    driver = Mt5Driver(settings)
    try:
        now = int(time.time())
        # Only DISC1 is visible; the hidden one must not be probed first.
        fake = _FakeMt5(
            {"DISC1": now + 90 * 60, "HID1": now + 30 * 60}, visible={"DISC1"}
        )
        driver._detect_broker_offset(fake)
        assert fake.tick_calls[0] == "DISC1"
        assert settings.broker_utc_offset_minutes == 90
    finally:
        driver.disconnect()


def test_detect_broker_offset_skips_when_broker_reports_no_symbols(settings):
    driver = Mt5Driver(settings)
    try:
        fake = _FakeMt5({})
        driver._detect_broker_offset(fake)
        assert fake.symbols_get_called is True
        assert fake.tick_calls == []
        assert settings.broker_utc_offset_minutes == 0
    finally:
        driver.disconnect()


def test_detect_broker_offset_survives_symbols_get_failure(settings):
    driver = Mt5Driver(settings)
    try:
        fake = _FakeMt5({}, fail_symbols_get=True)
        driver._detect_broker_offset(fake)  # must not raise
        assert settings.broker_utc_offset_minutes == 0
    finally:
        driver.disconnect()


# ---------------------------------------------------------------------------
# re-discovery after PUT /settings (spec §3)
# ---------------------------------------------------------------------------
def test_put_settings_terminal_change_rediscovery(client, tmp_path):
    http, ctx, _ = client
    term_a = _fake_terminal(tmp_path / "a" / "terminal64.exe")
    term_b = _fake_terminal(tmp_path / "b" / "terminal64.exe")
    resp = http.put(
        "/api/v1/settings",
        json={
            "mt5_terminal_path": str(term_a),
            "mt5_custom_terminal_dirs": str(tmp_path / "b"),
        },
        headers=_auth(ctx),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data["changed"]) == {"mt5_terminal_path", "mt5_custom_terminal_dirs"}
    assert data["terminals_rediscovered"] is True
    assert data["driver_disconnected"] is False
    discovery = data["discovery"]
    by_path = {t["path"]: t for t in discovery["terminals"]}
    assert set(by_path) == {str(term_a), str(term_b)}
    assert discovery["selected_path"] == str(term_a)
    assert by_path[str(term_a)]["source"] == "configured"
    assert by_path[str(term_b)]["source"] == "custom"
    # A subsequent GET reflects the same change.
    data2 = http.get("/api/v1/mt5/terminals", headers=_auth(ctx)).json()["data"]
    assert data2["selected_path"] == str(term_a)
    assert {t["path"] for t in data2["terminals"]} == {str(term_a), str(term_b)}
    # And the custom dirs are persisted + visible via GET /settings.
    settings_data = http.get("/api/v1/settings", headers=_auth(ctx)).json()["data"]
    assert settings_data["mt5_custom_terminal_dirs"] == str(tmp_path / "b")
    assert settings_data["mt5_terminal_path"] == str(term_a)


def test_put_settings_terminal_path_change_disconnects_stale_driver(client, tmp_path):
    http, ctx, _ = client
    term_a = _fake_terminal(tmp_path / "a" / "terminal64.exe")
    term_b = _fake_terminal(tmp_path / "b" / "terminal64.exe")
    ctx.settings.mt5_terminal_path = str(term_a)
    ctx.driver._connected = True
    ctx.driver._set_state("CONNECTED", "connected")
    resp = http.put(
        "/api/v1/settings",
        json={"mt5_terminal_path": str(term_b)},
        headers=_auth(ctx),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["terminals_rediscovered"] is True
    assert data["driver_disconnected"] is True
    assert data["discovery"]["selected_path"] == str(term_b)
    # The driver no longer claims to be connected to the old terminal.
    assert ctx.driver.connected is False
    assert ctx.driver.connection_state_dict()["state"] == "DISCONNECTED"


def test_put_settings_custom_dirs_only_does_not_disconnect_driver(client, tmp_path):
    http, ctx, _ = client
    term_a = _fake_terminal(tmp_path / "a" / "terminal64.exe")
    ctx.settings.mt5_terminal_path = str(term_a)
    ctx.driver._connected = True
    ctx.driver._set_state("CONNECTED", "connected")
    resp = http.put(
        "/api/v1/settings",
        json={"mt5_custom_terminal_dirs": str(tmp_path / "b")},
        headers=_auth(ctx),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["terminals_rediscovered"] is True
    assert data["driver_disconnected"] is False
    assert ctx.driver.connected is True  # selection did not move


def test_put_settings_unrelated_change_does_not_rediscover(client):
    http, ctx, _ = client
    resp = http.put("/api/v1/settings", json={"magic": 12345}, headers=_auth(ctx))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["changed"] == ["magic"]
    assert data["terminals_rediscovered"] is False
    assert data["discovery"] is None
    assert data["driver_disconnected"] is False
