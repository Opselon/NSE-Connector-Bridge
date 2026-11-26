"""Unit tests for the v1.4.0 B1 non-blocking splash/startup slice.

Covers: the named-step :class:`StartupProgress` model (text,
fraction, terminal states, thread-safety), the splash watchdog
that auto-hides after a bounded lifetime, the bounded Win32 text
update (``SendMessageTimeoutW`` — a hung pump can never freeze the
caller), the bounded native-window open (``"timeout"`` when the
window never shows, ``"webview"`` when it does), and the main-thread
/ boot-thread split in ``main()``: boot runs off the main thread,
the splash receives real step updates, a failed dashboard probe
degrades instead of killing a running server, and a boot crash
fails visibly with the exact error.

Seams are mocked (OS/browser/webview/server); no test claims a
real GUI run. Label: unit.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nse_bridge.gui.splash import STARTUP_STEPS, StartupProgress

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_bridge_env(monkeypatch):
    import os

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
    """A fake ``ctypes.windll`` so the Win32 splash code runs on
    Linux CI. Return values are real ints (handles), never
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
    user32.MessageBoxW.return_value = 1
    windll.kernel32.GetModuleHandleW.return_value = 0x3000
    windll.gdi32.CreateFontW.return_value = 0x6000
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    return windll


# ---------------------------------------------------------------------------
# StartupProgress: named steps with progress, terminal states
# ---------------------------------------------------------------------------


def test_startup_progress_steps_and_fraction():
    progress = StartupProgress(("A", "B", "C"))
    assert progress.text() == "Starting…"
    assert progress.fraction == 0.0
    assert progress.current_step == ""
    progress.advance("A")
    assert progress.text() == "Step 1/3 — A…"
    assert progress.current_step == "A"
    assert progress.fraction == 0.0
    progress.advance("B")
    assert progress.text() == "Step 2/3 — B…"
    assert progress.fraction == pytest.approx(1 / 3)
    progress.advance("C")
    assert progress.fraction == pytest.approx(2 / 3)
    progress.complete()
    assert progress.text() == "Ready"
    assert progress.fraction == 1.0
    assert progress.done is True
    progress.advance("A")  # no-op after complete
    assert progress.text() == "Ready"


def test_startup_progress_fail_is_terminal():
    progress = StartupProgress(("A", "B"))
    progress.advance("A")
    progress.fail("boom")
    assert progress.text() == "Failed: boom"
    assert progress.failed_message == "boom"
    assert progress.done is False
    progress.advance("B")  # no-op after failure
    assert progress.text() == "Failed: boom"
    progress.complete()  # no-op after failure
    assert progress.text() == "Failed: boom"


def test_startup_progress_thread_safe():
    """The boot thread advances while the UI thread reads — no
    torn reads, no exceptions, and the final state is exact."""
    progress = StartupProgress()
    errors: list[BaseException] = []

    def _advance() -> None:
        try:
            for step in STARTUP_STEPS:
                progress.advance(step)
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_advance, daemon=True)
    thread.start()
    while thread.is_alive():
        _ = progress.text()
        _ = progress.fraction
        _ = progress.current_step
        time.sleep(0.001)
    thread.join()
    assert not errors
    assert progress.text() == f"Step {len(STARTUP_STEPS)}/{len(STARTUP_STEPS)} — {STARTUP_STEPS[-1]}…"


# ---------------------------------------------------------------------------
# splash: bounded text update + bounded lifetime watchdog
# ---------------------------------------------------------------------------


def test_set_text_timeout_uses_bounded_send(fake_windll):
    from nse_bridge.gui import _win32

    assert _win32.set_text_timeout(fake_windll.user32, 0x1000, "hello") is True
    call = fake_windll.user32.SendMessageTimeoutW.call_args
    assert call is not None
    args = call.args
    assert args[0] == 0x1000
    assert args[1] == _win32.WM_SETTEXT
    assert args[3].value == "hello"
    assert args[4] == _win32.SMTO_ABORTIFHUNG
    assert args[5] == 500


def test_splash_auto_hides_after_bounded_lifetime(fake_windll):
    """A splash that is never hidden by the caller still detaches
    after its bounded lifetime — no permanent dead window."""
    from nse_bridge.gui import splash

    shown = splash.show_splash(max_lifetime_s=0.3)
    assert shown.visible is True
    assert splash.splash_visible() is True
    deadline = time.monotonic() + 5
    while splash.splash_visible() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert splash.splash_visible() is False


def test_splash_watchdog_does_not_hide_a_newer_splash(fake_windll):
    """The watchdog only fires for the splash it was started for:
    hiding + re-showing inside the lifetime leaves the new splash
    up (its own watchdog owns its lifetime)."""
    from nse_bridge.gui import splash

    first = splash.show_splash(max_lifetime_s=0.3)
    assert first.visible is True
    splash.hide_splash()
    second = splash.show_splash(max_lifetime_s=30.0)
    assert second.visible is True
    assert second is not first
    time.sleep(0.6)  # past the FIRST splash's lifetime
    assert splash.splash_visible() is True
    splash.hide_splash()


# ---------------------------------------------------------------------------
# native window: bounded open
# ---------------------------------------------------------------------------


def test_open_window_timeout_when_window_never_shows(monkeypatch):
    """webview.start() stuck before the window appears: the watchdog
    closes the stuck window after the bound and open_window returns
    "timeout" instead of hanging forever."""
    import nse_bridge.gui.window as window_mod

    released = threading.Event()
    destroyed: list[str] = []
    shown_event = threading.Event()  # never set: the window never appears
    fake_window = SimpleNamespace(
        events=SimpleNamespace(shown=shown_event),
        destroy=lambda: (destroyed.append("x"), released.set()),
    )
    fake_webview = SimpleNamespace(
        create_window=lambda *a, **k: fake_window,
        start=lambda *a, **k: released.wait(timeout=10),
        windows=[fake_window],
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    started = time.monotonic()
    assert window_mod.open_window("http://127.0.0.1:8788/ui", open_timeout_s=0.5) == "timeout"
    assert time.monotonic() - started < 5.0
    assert destroyed == ["x"]


def test_open_window_webview_when_window_shows(monkeypatch):
    """The window's shown event fires: the watchdog stands down and
    open_window returns "webview" even with a short bound."""
    import nse_bridge.gui.window as window_mod

    shown_event = threading.Event()
    calls: list[str] = []
    fake_window = SimpleNamespace(events=SimpleNamespace(shown=shown_event), destroy=lambda: None)

    def _start(*a, **k):
        calls.append("start")
        shown_event.set()

    fake_webview = SimpleNamespace(
        create_window=lambda *a, **k: fake_window,
        start=_start,
        windows=[fake_window],
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    assert window_mod.open_window("http://127.0.0.1:8788/ui", open_timeout_s=0.5) == "webview"
    assert calls == ["start"]


# ---------------------------------------------------------------------------
# main(): boot on a background thread, UI thread paints + serves
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_boot(monkeypatch):
    """Stub the pieces of main() so _run exercises the boot-thread
    split and the outcome handling, not a real server boot. The
    fake create_app sleeps briefly so the UI thread observes real
    intermediate steps instead of racing past them."""
    import uvicorn

    import nse_bridge.main as main_mod
    import nse_bridge.single_instance as single_mod

    state: dict = {
        "update_texts": [],
        "fractions": [],
        "opened": [],
        "boot_threads": [],
        "disconnected": [],
        "tray_stopped": [],
        "errors": [],
        "infos": [],
        "copied": [],
        "probe_ok": True,
        "create_app_error": None,
    }

    settings = SimpleNamespace(
        host="127.0.0.1",
        port=8788,
        gui_mode="auto",
        diagnostic_mode=False,
        log_level="INFO",
        allow_non_loopback=False,
        network_proxy="",
        server_token="",
        local_api_key="test-key",
        resolved_log_dir="/tmp/b1-logs",
    )

    class FakeDriver:
        def __init__(self, settings):
            state["boot_threads"].append(threading.current_thread().name)

        def connect(self):
            return {"ok": True}

        def disconnect(self):
            state["disconnected"].append(True)

    fake_ctx = SimpleNamespace(logs=SimpleNamespace(sink=None), get_or_create_device_id=lambda: "dev-1")

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = True
            self.should_exit = False

        def run(self):
            return None

    def _create_app(ctx):
        if state["create_app_error"] is not None:
            raise state["create_app_error"]
        time.sleep(0.25)  # let the UI thread observe "Preparing bridge…"
        return SimpleNamespace()

    monkeypatch.setattr(main_mod, "load_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "ensure_local_api_key", lambda s: None)
    monkeypatch.setattr(main_mod, "register_secret", lambda v: None)
    monkeypatch.setattr(main_mod, "show_splash", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "hide_splash", lambda: None)
    monkeypatch.setattr(main_mod, "update_splash", lambda text: state["update_texts"].append(text))
    monkeypatch.setattr(main_mod, "set_splash_progress", lambda f: state["fractions"].append(f))
    monkeypatch.setattr(single_mod, "acquire_single_instance", lambda: (object(), True))
    monkeypatch.setattr(main_mod, "_check_port_free", lambda h, p: None)
    monkeypatch.setattr(main_mod, "Mt5Driver", FakeDriver)
    monkeypatch.setattr(main_mod, "BridgeContext", lambda settings, driver: fake_ctx)
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "persist_settings", lambda s: None)
    monkeypatch.setattr(main_mod, "create_app", _create_app)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(main_mod, "wait_for_http_ready", lambda url, timeout_s=5.0: state["probe_ok"])
    monkeypatch.setattr(main_mod, "open_window", lambda url, **k: state["opened"].append(url) or "webview")
    monkeypatch.setattr(
        main_mod,
        "start_tray",
        lambda **k: SimpleNamespace(stop=lambda: state["tray_stopped"].append(True), available=True),
    )
    monkeypatch.setattr(main_mod, "show_error_dialog", lambda t, m: state["errors"].append((t, m)) or True)
    monkeypatch.setattr(main_mod, "show_info_dialog", lambda t, m: state["infos"].append((t, m)) or True)
    monkeypatch.setattr(main_mod, "copy_to_clipboard", lambda text: state["copied"].append(text) or True)
    return state


def test_boot_runs_off_main_thread_with_real_step_updates(_patch_boot):
    import nse_bridge.main as main_mod

    state = _patch_boot
    main_mod.main(["--port", "8788"])
    # The server boot ran on the background thread, not the UI thread.
    assert state["boot_threads"] == ["bridge-boot"]
    # The splash received real named steps while booting.
    assert any(text.startswith("Step ") for text in state["update_texts"])
    assert any("Preparing bridge" in text for text in state["update_texts"])
    assert state["fractions"] and all(0.0 <= f <= 1.0 for f in state["fractions"])
    assert any(f > 0.0 for f in state["fractions"])
    # And the bridge went on to open its dashboard and shut down cleanly.
    assert state["opened"] == ["http://127.0.0.1:8788/ui#platform"]
    assert state["disconnected"] == [True]
    assert state["tray_stopped"] == [True]
    assert state["errors"] == []


def test_dashboard_probe_failure_degrades_instead_of_killing_server(_patch_boot):
    """The server is up but /health does not answer in time: the
    bridge keeps serving with a clear message — it must NOT exit."""
    import nse_bridge.main as main_mod

    state = _patch_boot
    state["probe_ok"] = False
    main_mod.main(["--port", "8788"])  # returns normally — no SystemExit
    assert state["infos"] and "slow to answer" in state["infos"][0][0]
    assert "did not answer /health" in state["infos"][0][1]
    assert state["copied"] == ["http://127.0.0.1:8788/ui#platform"]
    assert state["opened"] == ["http://127.0.0.1:8788/ui#platform"]
    assert state["errors"] == []


def test_boot_crash_fails_visibly_with_exact_error(_patch_boot):
    """A crash inside boot (here: app creation) is a visible failure
    on the main thread: error dialog with the exact message, exit 1,
    and no dashboard open attempt."""
    import nse_bridge.main as main_mod

    state = _patch_boot
    state["create_app_error"] = RuntimeError("app exploded")
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["--port", "8788"])
    assert excinfo.value.code == 1
    assert state["errors"] and "app exploded" in state["errors"][0][1]
    assert state["opened"] == []
