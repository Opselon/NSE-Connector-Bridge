"""Unit tests for the visible-startup tranche (v1.3.0, S1).

Covers: the splash window (Windows ctypes path with a fake
``windll`` + the no-tkinter degradation), the tray icon (menu
dispatch + native creation with a fake ``windll``), the robust
browser opener (return-value checks, retries, explicit-browser
fallback), visible error dialogs + clipboard, the second-launch
bring-to-front path and the port pre-flight in ``main()``, the
``/ui#platform`` Connection-screen landing, and the frozen-exe smoke
script's helpers.

Seams are mocked (OS/browser/tray); no test claims a real GUI run —
the frozen exe itself is exercised by scripts/smoke_frozen_exe.py
on Windows (S3 wires it into the release job).
Label: unit.
"""

from __future__ import annotations

import ctypes
import http.server
import os
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nse_bridge.gui.browser import BrowserOpenResult, dashboard_url, open_dashboard, wait_for_http_ready
from nse_bridge.gui.dialogs import copy_to_clipboard, show_error_dialog
from nse_bridge.gui.tray import MENU_OPEN, MENU_QUIT, MENU_SHOW_LOGS, TrayIcon, start_tray

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("NSE_BRIDGE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("NSE_MT5_TERMINAL_PATH", raising=False)


@pytest.fixture(autouse=True)
def _reset_splash():
    from nse_bridge.gui import splash

    yield
    try:
        splash.hide_splash()
    except Exception:  # noqa: BLE001, S110
        pass
    splash._CURRENT = None


@pytest.fixture
def fake_windll(monkeypatch):
    """A fake ``ctypes.windll`` so the Win32 splash/tray/dialog code
    runs on Linux CI. Return values are real ints (handles), never
    MagicMocks, so the ctypes Structure assignments behave."""
    windll = MagicMock()
    user32 = windll.user32
    user32.RegisterClassW.return_value = 1
    user32.CreateWindowExW.return_value = 0x1000
    user32.GetSystemMetrics.return_value = 1920
    user32.GetMessageW.return_value = 0  # pump loop exits immediately
    user32.SetWindowTextW.return_value = 1
    user32.PostMessageW.return_value = 1
    user32.PostQuitMessage.return_value = 1
    user32.ShowWindow.return_value = True
    user32.UpdateWindow.return_value = True
    user32.DefWindowProcW.return_value = 0
    user32.LoadCursorW.return_value = 0
    user32.LoadIconW.return_value = 0x2000
    user32.CreatePopupMenu.return_value = 0x5000
    user32.AppendMenuW.return_value = True
    user32.SetForegroundWindow.return_value = True
    user32.TrackPopupMenu.return_value = 0
    user32.DestroyMenu.return_value = True
    user32.MessageBoxW.return_value = 1
    windll.kernel32.GetModuleHandleW.return_value = 0x3000
    windll.kernel32.GlobalAlloc.return_value = 0x7000
    windll.kernel32.GlobalLock.return_value = 0x7100
    windll.kernel32.GlobalUnlock.return_value = True
    windll.kernel32.GlobalFree.return_value = 0
    windll.shell32.Shell_NotifyIconW.return_value = True
    windll.shell32.ExtractIconW.return_value = 0x4000
    windll.gdi32.CreateFontW.return_value = 0x6000
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    return windll


@pytest.fixture
def no_tkinter(monkeypatch):
    """Block tkinter so the fallbacks degrade exactly like a frozen
    exe (which excludes tkinter) or a headless box."""
    monkeypatch.setitem(sys.modules, "tkinter", None)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", None)


# ---------------------------------------------------------------------------
# dashboard URL / Connection-screen landing
# ---------------------------------------------------------------------------


def test_dashboard_url_configured_lands_on_overview():
    assert dashboard_url("http://127.0.0.1:8788", configured=True) == "http://127.0.0.1:8788/ui"


def test_dashboard_url_unconfigured_lands_on_connection_screen():
    assert dashboard_url("http://127.0.0.1:8788", configured=False) == "http://127.0.0.1:8788/ui#platform"


def test_dashboard_url_strips_trailing_slash():
    assert dashboard_url("http://127.0.0.1:8788/", configured=True) == "http://127.0.0.1:8788/ui"


def test_index_html_honours_screen_hash():
    """The static dashboard must honour /ui#<screen> on load, or the
    #platform deep link from an unconfigured bridge is dead."""
    html = (Path(__file__).resolve().parents[2] / "nse_bridge" / "gui" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "location.hash" in html
    assert "SCREENS.includes(_initial)" in html
    # The default landing is unchanged: no hash → Overview.
    assert "show(SCREENS.includes(_initial) ? _initial : 'overview')" in html


# ---------------------------------------------------------------------------
# robust browser auto-open
# ---------------------------------------------------------------------------


def test_open_dashboard_default_browser_success(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    result = open_dashboard("http://127.0.0.1:8788/ui")
    assert result == BrowserOpenResult(ok=True, method="default", attempts=1)


def test_open_dashboard_retries_default_browser(monkeypatch):
    calls = {"n": 0}

    def _flaky(url: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr("webbrowser.open", _flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    result = open_dashboard("http://127.0.0.1:8788/ui")
    assert result.ok and result.method == "default" and result.attempts == 3


def test_open_dashboard_falls_back_to_explicit_chrome(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/google-chrome" if name == "chrome" else None)
    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: spawned.append(args) or MagicMock())
    result = open_dashboard("http://127.0.0.1:8788/ui")
    assert result.ok and result.method == "chrome"
    assert spawned and spawned[0][0] == "/usr/bin/google-chrome"
    assert spawned[0][1] == "http://127.0.0.1:8788/ui"


def test_open_dashboard_total_failure_reports_it(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("os.path.isfile", lambda path: False)
    result = open_dashboard("http://127.0.0.1:8788/ui")
    assert result.ok is False
    assert result.method == ""
    assert result.attempts >= 3  # the default-browser retries happened
    assert result.error


def test_open_dashboard_explicit_browser_failure_tries_next(monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    monkeypatch.setattr("time.sleep", lambda s: None)

    def _which(name: str):
        return {"msedge": "/usr/bin/msedge", "chrome": "/usr/bin/google-chrome"}.get(name)

    monkeypatch.setattr("shutil.which", _which)

    def _popen(args, **kw):
        if "msedge" in args[0]:
            raise OSError("edge exploded")
        return MagicMock()

    monkeypatch.setattr("subprocess.Popen", _popen)
    result = open_dashboard("http://127.0.0.1:8788/ui")
    assert result.ok and result.method == "chrome"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_200(port: int) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_wait_for_http_ready_true_when_serving():
    port = _free_port()
    server = _serve_200(port)
    try:
        assert wait_for_http_ready(f"http://127.0.0.1:{port}/health", timeout_s=5.0) is True
    finally:
        server.shutdown()


def test_wait_for_http_ready_false_when_nothing_listening():
    assert wait_for_http_ready(f"http://127.0.0.1:{_free_port()}/health", timeout_s=0.5, interval_s=0.1) is False


# ---------------------------------------------------------------------------
# native window fallback
# ---------------------------------------------------------------------------


def test_open_window_prefers_webview_when_it_works(monkeypatch):
    import nse_bridge.gui.window as window_mod

    calls: list[str] = []
    fake_webview = types.SimpleNamespace(
        create_window=lambda *a, **k: calls.append("create"),
        start=lambda *a, **k: calls.append("start"),
        windows=[],
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    assert window_mod.open_window("http://127.0.0.1:8788/ui") == "webview"
    assert calls == ["create", "start"]


def test_open_window_falls_back_to_browser_when_webview_fails(monkeypatch):
    import nse_bridge.gui.window as window_mod

    fake_webview = types.SimpleNamespace(
        create_window=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no WebView2")),
        start=lambda *a, **k: None,
        windows=[],
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(window_mod, "open_dashboard", lambda url: BrowserOpenResult(ok=True, method="default"))
    assert window_mod.open_window("http://127.0.0.1:8788/ui") == "browser"


def test_open_window_reports_failed_when_everything_fails(monkeypatch):
    import nse_bridge.gui.window as window_mod

    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` raises
    monkeypatch.setattr(window_mod, "open_dashboard", lambda url: BrowserOpenResult(ok=False, error="no browser"))
    assert window_mod.open_window("http://127.0.0.1:8788/ui") == "failed"


def test_destroy_native_window_noop_without_webview(monkeypatch):
    import nse_bridge.gui.window as window_mod

    monkeypatch.setitem(sys.modules, "webview", None)
    assert window_mod.destroy_native_window() is False


# ---------------------------------------------------------------------------
# splash
# ---------------------------------------------------------------------------


def test_splash_windows_path_shows_updates_and_hides(fake_windll):
    from nse_bridge.gui import splash

    shown = splash.show_splash("NSE MT5 Bridge v1.3.0", "Starting…")
    assert shown.visible is True
    # Idempotent: the second call returns the same splash.
    assert splash.show_splash() is shown
    splash.update_splash("Starting server…")
    # The status label is the second STATIC child created; the update
    # must reach SetWindowTextW with the new text (any handle).
    texts = [call.args[1] for call in fake_windll.user32.SetWindowTextW.call_args_list]
    assert "Starting server…" in texts
    splash.hide_splash()
    assert shown.visible is False
    fake_windll.user32.PostMessageW.assert_called()
    assert splash.splash_visible() is False


def test_splash_degrades_without_tkinter(monkeypatch, no_tkinter):
    monkeypatch.setattr(sys, "platform", "linux")
    from nse_bridge.gui import splash

    shown = splash.show_splash()
    assert shown.visible is False
    # Updates and hide are no-ops, never raise.
    splash.update_splash("Starting server…")
    splash.hide_splash()
    assert splash.splash_visible() is False


def test_splash_never_raises_when_windll_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    from nse_bridge.gui import splash

    shown = splash.show_splash()
    assert shown.visible is False
    splash.hide_splash()


# ---------------------------------------------------------------------------
# tray
# ---------------------------------------------------------------------------


def test_tray_dispatch_calls_each_menu_action():
    seen: list[str] = []
    tray = TrayIcon(
        on_open=lambda: seen.append("open"),
        on_show_logs=lambda: seen.append("logs"),
        on_quit=lambda: seen.append("quit"),
    )
    assert tray.dispatch_command(MENU_OPEN) is True
    assert tray.dispatch_command(MENU_SHOW_LOGS) is True
    assert tray.dispatch_command(MENU_QUIT) is True
    assert tray.quit_requested.is_set()
    assert seen == ["open", "logs", "quit"]
    assert tray.dispatch_command(9999) is False


def test_tray_dispatch_swallows_callback_errors():
    tray = TrayIcon(on_open=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert tray.dispatch_command(MENU_OPEN) is True


def test_tray_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    tray = start_tray(on_open=lambda: None)
    assert tray.available is False


def test_tray_windows_path_creates_icon_and_menu_quit(fake_windll):
    seen: list[str] = []
    tray = start_tray(
        on_open=lambda: seen.append("open"),
        on_show_logs=lambda: seen.append("logs"),
        on_quit=lambda: seen.append("quit"),
    )
    assert tray.available is True
    fake_windll.shell32.Shell_NotifyIconW.assert_called()
    # Right-click → popup menu → user picks Quit (TrackPopupMenu
    # returns the chosen command id).
    fake_windll.user32.TrackPopupMenu.return_value = MENU_QUIT
    tray._show_menu()
    assert tray.quit_requested.is_set()
    assert seen == ["quit"]
    # Double-click opens the dashboard.
    tray._handler(tray._hwnd, 0x0400 + 1, 0, 0x0203)
    assert seen == ["quit", "open"]
    tray.stop()
    assert tray.available is False


# ---------------------------------------------------------------------------
# dialogs + clipboard
# ---------------------------------------------------------------------------


def test_show_error_dialog_windows(fake_windll):
    assert show_error_dialog("Title", "Something failed") is True
    fake_windll.user32.MessageBoxW.assert_called_once()
    _args, _kwargs = fake_windll.user32.MessageBoxW.call_args
    assert _args[1] == "Something failed"
    assert _args[2] == "Title"


def test_show_error_dialog_degrades_to_stderr_without_gui(monkeypatch, no_tkinter, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert show_error_dialog("Title", "Something failed") is False
    assert "Something failed" in capsys.readouterr().err


def test_copy_to_clipboard_degrades_without_gui(monkeypatch, no_tkinter):
    monkeypatch.setattr(sys, "platform", "linux")
    assert copy_to_clipboard("http://127.0.0.1:8788/ui") is False


def test_copy_to_clipboard_windows(fake_windll, monkeypatch):
    # memmove must NOT run against the fake GlobalLock address
    # (0x7100 is unmapped — a real memmove there segfaults the test
    # process); stub it and assert the copy bytes instead.
    moved: list[bytes] = []
    monkeypatch.setattr(ctypes, "memmove", lambda dst, src, n: moved.append(bytes(src[:n])))
    assert copy_to_clipboard("http://127.0.0.1:8788/ui") is True
    fake_windll.user32.OpenClipboard.assert_called_once()
    fake_windll.user32.SetClipboardData.assert_called_once()
    assert moved and moved[0].decode("utf-16-le").rstrip("\x00") == "http://127.0.0.1:8788/ui"


# ---------------------------------------------------------------------------
# main(): second launch + port pre-flight
# ---------------------------------------------------------------------------


def _fake_settings(**overrides):
    base = {
        "host": "127.0.0.1",
        "port": 8788,
        "gui_mode": "auto",
        "diagnostic_mode": False,
        "log_level": "INFO",
        "allow_non_loopback": False,
        "network_proxy": "",
        "server_token": "",
        "local_api_key": "test-key",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def _patch_main_boot(monkeypatch):
    """Stub the pieces of main() before the single-instance check so
    the tests exercise the startup decisions, not the server boot."""
    import nse_bridge.main as main_mod

    monkeypatch.setattr(main_mod, "load_settings", lambda: _fake_settings())
    monkeypatch.setattr(main_mod, "ensure_local_api_key", lambda settings: None)
    monkeypatch.setattr(main_mod, "register_secret", lambda value: None)
    monkeypatch.setattr(main_mod, "show_splash", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "hide_splash", lambda: None)
    monkeypatch.setattr(main_mod, "update_splash", lambda text: None)
    return main_mod


def test_second_launch_brings_existing_ui_to_front(monkeypatch, _patch_main_boot):
    import nse_bridge.main as main_mod
    import nse_bridge.single_instance as single_mod

    opened: list[str] = []
    monkeypatch.setattr(single_mod, "acquire_single_instance", lambda: (None, False))
    monkeypatch.setattr(
        main_mod, "open_dashboard", lambda url: opened.append(url) or BrowserOpenResult(ok=True, method="default")
    )
    # Returns normally (exit 0) — the running instance shows the UI.
    main_mod.main(["--port", "8788"])
    assert opened == ["http://127.0.0.1:8788/ui#platform"]


def test_second_launch_browser_failure_shows_url_and_copies_it(monkeypatch, _patch_main_boot):
    import nse_bridge.main as main_mod
    import nse_bridge.single_instance as single_mod

    monkeypatch.setattr(single_mod, "acquire_single_instance", lambda: (None, False))
    monkeypatch.setattr(main_mod, "open_dashboard", lambda url: BrowserOpenResult(ok=False, error="no browser"))
    copied: list[str] = []
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(main_mod, "show_info_dialog", lambda t, m: dialogs.append((t, m)) or True)
    main_mod.main(["--port", "8788"])
    assert copied == ["http://127.0.0.1:8788/ui#platform"]
    assert dialogs and "already running" in dialogs[0][0]
    assert "http://127.0.0.1:8788/ui#platform" in dialogs[0][1]


def test_second_launch_headless_never_opens_browser_or_dialog(monkeypatch, _patch_main_boot, capsys):
    """--gui none: the second launch prints the URL and exits 0 without
    touching a browser or a dialog — a modal MessageBox here hung the
    CI smoke until the 40-min job timeout (no user to dismiss it)."""
    import nse_bridge.main as main_mod
    import nse_bridge.single_instance as single_mod

    monkeypatch.setattr(single_mod, "acquire_single_instance", lambda: (None, False))
    opened: list[str] = []
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_mod, "open_dashboard", lambda url: opened.append(url) or BrowserOpenResult(ok=True, method="default")
    )
    monkeypatch.setattr(main_mod, "show_info_dialog", lambda t, m: dialogs.append((t, m)) or True)
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: True)
    # Returns normally (exit 0) — the running instance keeps serving.
    main_mod.main(["--port", "8788", "--gui", "none"])
    assert opened == []
    assert dialogs == []
    out = capsys.readouterr().out
    assert "already running" in out
    assert "http://127.0.0.1:8788/ui#platform" in out


def test_port_taken_fails_visibly_with_exit_2(monkeypatch, _patch_main_boot):
    import nse_bridge.main as main_mod
    import nse_bridge.single_instance as single_mod

    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        monkeypatch.setattr(single_mod, "acquire_single_instance", lambda: (None, True))
        monkeypatch.setattr(main_mod, "load_settings", lambda: _fake_settings(port=port, server_token="tok"))
        dialogs: list[tuple[str, str]] = []
        monkeypatch.setattr(main_mod, "show_error_dialog", lambda t, m: dialogs.append((t, m)) or True)
        with pytest.raises(SystemExit) as excinfo:
            main_mod.main(["--port", str(port)])
        assert excinfo.value.code == 2
        assert dialogs and "already in use" in dialogs[0][1]
        assert str(port) in dialogs[0][1]
    finally:
        blocker.close()


def test_check_port_free():
    from nse_bridge.main import _check_port_free

    port = _free_port()
    assert _check_port_free("127.0.0.1", port) is None
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        error = _check_port_free("127.0.0.1", port)
        assert error is not None and "already in use" in error
    finally:
        blocker.close()


def test_serve_until_quit_returns_when_server_thread_ends():
    from nse_bridge.main import _serve_until_quit

    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    _serve_until_quit(thread, threading.Event())  # must not hang


def test_serve_until_quit_returns_on_quit_event():
    import time as _time

    from nse_bridge.main import _serve_until_quit

    quit_event = threading.Event()
    alive = threading.Event()

    def _run():
        alive.set()
        while not quit_event.is_set():
            _time.sleep(0.01)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert alive.wait(timeout=2)
    quit_event.set()
    started = _time.monotonic()
    _serve_until_quit(thread, quit_event)
    assert _time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# run_bridge.py: splash gating (loaded without executing main())
# ---------------------------------------------------------------------------


def test_run_bridge_splash_gating():
    run_bridge = Path(__file__).resolve().parents[2] / "run_bridge.py"
    source = run_bridge.read_text(encoding="utf-8")
    # Only the definitions — never execute the `if _should_show_splash`
    # block (it would try to show a real splash in the test process).
    head = source.split("if _should_show_splash(sys.argv[1:]):")[0]
    namespace: dict = {}
    exec(compile(head, str(run_bridge), "exec"), namespace)  # noqa: S102 — test-only, own file
    should = namespace["_should_show_splash"]
    assert should([]) is True
    assert should(["--port", "8788"]) is True
    assert should(["status", "--json"]) is False
    assert should(["health"]) is False
    assert should(["mt5", "list"]) is False
    assert should(["--help"]) is False
    assert should(["-h"]) is False
    assert should(["--console"]) is False
    assert should(["--diagnostic"]) is False
    assert should(["--gui", "none"]) is False
    assert should(["--gui=none"]) is False
    assert should(["--gui", "browser"]) is True


# ---------------------------------------------------------------------------
# smoke script helpers
# ---------------------------------------------------------------------------


def test_smoke_script_help_and_helpers(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "smoke_frozen_exe.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0
    assert "smoke" in proc.stdout.lower() or "usage" in proc.stdout.lower()
    # The readiness helper works against a real local server (same
    # code path the script uses for /health).
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_frozen_exe", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    port = _free_port()
    server = _serve_200(port)
    try:
        assert module._wait_ready(f"http://127.0.0.1:{port}", 5.0) >= 0
    finally:
        server.shutdown()
    assert module._free_port() != 0


def test_smoke_script_is_stdlib_only():
    """The release job runs it with the build Python — no third-party
    imports allowed."""
    script = (Path(__file__).resolve().parents[2] / "scripts" / "smoke_frozen_exe.py").read_text(encoding="utf-8")
    third_party = {"fastapi", "uvicorn", "loguru", "pytest", "httpx", "pydantic"}
    imported = set()
    for line in script.splitlines():
        line = line.strip()
        if line.startswith(("import ", "from ")):
            imported.add(line.split()[1].split(".")[0])
    assert not (imported & third_party), f"non-stdlib imports: {imported & third_party}"
