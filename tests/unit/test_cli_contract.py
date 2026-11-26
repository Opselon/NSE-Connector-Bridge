"""Boot/lifecycle + CLI contract tests (mission §C5/§H5).

What is proven here:
- single-instance: a second acquire on the same session is refused,
  and the OS releases the mutex when the handle is closed (a crashed
  process never blocks the next start);
- --console parser semantics: console mode forces GUI off; query
  subcommands parse standalone;
- CLI JSON contract: against a REAL in-process Bridge server (uvicorn
  on a loopback port), `status/health/mt5 list --json` return exactly
  one parseable JSON document with the stable envelope and exit 0;
  a query against a dead port answers the honest BRIDGE_NOT_RUNNING
  error envelope with exit 3.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

pytestmark = pytest.mark.unit

from nse_bridge import cli as bridge_cli
from nse_bridge.config import BridgeSettings
from nse_bridge.mt5.driver import MT5_PLATFORM_SUPPORTED

# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="single-instance enforcement is win32-only")
def test_single_instance_mutex_refuses_second_and_releases(tmp_path: Path):
    import ctypes

    from nse_bridge.single_instance import acquire_single_instance

    # Test-scoped mutex name: a REAL Bridge may legitimately be running
    # on this machine holding the production mutex (observed 2026-11-21
    # while the user's own instance was live) — the test must be
    # hermetic against that; the production name is never touched.
    name = rf"Local\NSE-MT5-Bridge-Test-{os.getpid()}"

    handle, acquired = acquire_single_instance(name)
    assert acquired is True, "first acquire must succeed"
    assert handle is not None
    try:
        _handle2, acquired2 = acquire_single_instance(name)
        assert acquired2 is False, "second acquire in the same session must be refused"
    finally:
        # Explicit release (the OS does the same on process exit — a
        # crashed process never blocks the next start).
        assert ctypes.windll.kernel32.CloseHandle(handle) == 1
    _handle3, acquired3 = acquire_single_instance(name)
    assert acquired3 is True, "a released mutex must be acquirable again"
    if acquired3:
        import ctypes as _ctypes

        _ctypes.windll.kernel32.CloseHandle(_handle3)


# ---------------------------------------------------------------------------
# Parser: --console + subcommands
# ---------------------------------------------------------------------------


def test_console_mode_forces_gui_off(monkeypatch):
    import nse_bridge.main as bridge_main

    captured: dict[str, BridgeSettings] = {}

    class _Sentinel(Exception):
        pass

    # Probe at the _run boundary: v1.4.0 B1 boots the server on a
    # background thread, and a crash inside boot is converted to a
    # visible failure (dialog + exit) instead of propagating — so a
    # sentinel raised inside boot no longer escapes main(). Probing
    # _run still proves the property: --console forces gui off
    # before any boot work happens.
    def fake_run(args, settings, gui_visible):
        captured["settings"] = settings
        raise _Sentinel()

    monkeypatch.setattr(bridge_main, "_run", fake_run)
    with pytest.raises(_Sentinel):
        bridge_main.main(["--console"])  # boots no server: sentinel fires first
    assert captured["settings"].gui_mode == "none", "--console must disable the GUI"


def test_query_subcommands_parse_with_json_flag():
    from nse_bridge.main import build_parser

    for argv in (["status", "--json"], ["health", "--json"], ["diagnostics", "--json"], ["mt5", "list", "--json"]):
        args = build_parser().parse_args(argv)
        assert args.json is True, f"{argv} must parse --json"
    args = build_parser().parse_args(["--console"])
    assert args.subcommand is None and args.console is True


# ---------------------------------------------------------------------------
# CLI JSON contract against a real running bridge
# ---------------------------------------------------------------------------


@pytest.fixture()
def running_bridge(tmp_path: Path):
    """Boot the real FastAPI app on a loopback port (no MT5 attached)."""
    from nse_bridge.api.app import create_app
    from nse_bridge.engine.common import BridgeContext
    from nse_bridge.mt5.driver import Mt5Driver
    from nse_bridge.secrets import ensure_local_api_key

    settings = BridgeSettings(data_dir=tmp_path, port=8791)
    settings.host = "127.0.0.1"
    ensure_local_api_key(settings)
    ctx = BridgeContext(settings=settings, driver=Mt5Driver(settings))
    app = create_app(ctx)
    config = uvicorn.Config(app, host="127.0.0.1", port=8791, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("test bridge did not start")
    yield settings
    server.should_exit = True
    thread.join(timeout=5)


def _run_cli_query(tmp_path: Path, argv: list[str]) -> tuple[int, str]:
    """Run the CLI query path in-process with an isolated settings dir."""
    import nse_bridge.main as bridge_main

    settings = BridgeSettings(data_dir=tmp_path, port=8791)
    original_load = bridge_main.load_settings
    # _dispatch_query uses main.py's module-level load_settings binding.
    bridge_main.load_settings = lambda: settings  # type: ignore[assignment]
    stdout = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(stdout):
            try:
                exit_code = bridge_main._dispatch_query(argv)
                code = 0 if exit_code is None else exit_code
            except SystemExit as exc:  # argparse usage errors
                code = int(exc.code or 0)
    finally:
        bridge_main.load_settings = original_load
    return code, stdout.getvalue()


def test_cli_status_json_against_running_bridge(running_bridge, tmp_path: Path):
    code, out = _run_cli_query(tmp_path, ["status", "--json"])
    assert code == 0, out
    # Exactly ONE complete JSON document with the stable envelope.
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, "one-shot CLI ops must print exactly one JSON document"
    doc = json.loads(lines[0])
    assert doc["ok"] is True
    data = doc["data"]
    assert data["bridge_version"]
    assert data["commands_registered"] == 91
    assert doc["timestamp"].endswith("Z")


def test_cli_health_and_mt5_list_json(running_bridge, tmp_path: Path):
    for argv in (["health", "--json"], ["mt5", "list", "--json"]):
        code, out = _run_cli_query(tmp_path, argv)
        assert code == 0, out
        doc = json.loads(out.strip().splitlines()[-1])
        assert doc["ok"] is True and "data" in doc


def test_cli_diagnostics_json_against_running_bridge(running_bridge, tmp_path: Path):
    # S2: diagnostics --json must work against the RUNNING instance,
    # not just against unit mocks — same live fixture as status/health.
    code, out = _run_cli_query(tmp_path, ["diagnostics", "--json"])
    assert code == 0, out
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, "one-shot CLI ops must print exactly one JSON document"
    doc = json.loads(lines[0])
    assert doc["ok"] is True
    data = doc["data"]
    assert data["bridge"]["healthy"] is False  # no MT5 on this host
    # v1.3.0 honest gate (S3 platform gating): on a platform that cannot
    # load the IPC driver the code is MT5_WINDOWS_REQUIRED, not
    # MT5_NOT_FOUND. On a supported platform (Windows x64) the old code
    # still applies. Same pattern as test_gui_control_center.py.
    expected = "MT5_WINDOWS_REQUIRED" if not MT5_PLATFORM_SUPPORTED else "MT5_NOT_FOUND"
    assert data["diagnostic"]["code"] == expected
    assert data["diagnostic"]["recovery"]
    assert "mt5" in data and "websocket" in data and "enrolment" in data
    assert data["log_file"].endswith("bridge.log")
    assert doc["timestamp"].endswith("Z")


def test_cli_refuses_when_bridge_is_down(tmp_path: Path):
    code, out = _run_cli_query(tmp_path, ["status", "--json"])
    assert code == bridge_cli.EXIT_NOT_RUNNING
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["ok"] is False
    assert doc["error"]["code"] == "BRIDGE_NOT_RUNNING"
    assert "start it first" in doc["error"]["message"]
