"""Splash / loading window, shown IMMEDIATELY on launch.

Why this module exists: v1.2.2 showed nothing for up to 90 s after a
double-click (PyInstaller onefile extraction + a Defender scan of an
unsigned exe), so the user double-clicked again — and the second
launch died silently on the single-instance mutex. The splash is the
first thing the process shows, before settings, the server, or MT5.

Hard truth, documented for the packaging decision (S3 owns
``bridge.spec``): in a **onefile** exe no Python runs before
extraction finishes, so even this splash cannot appear during the
cold extraction. The fix for cold-start visibility is therefore
**onedir**: the installer already exists, onedir extracts once at
install time, and later launches start Python in ~1–2 s — the splash
then appears within a few seconds of double-click. Trade-off: onedir
ships a folder of files instead of one portable ``.exe`` (it must go
through the installer or a zip), and the first launch after install
can still be slowed by a Defender scan — but the splash appears as
soon as Python starts, and its note tells the user what is happening.
This module is the in-process half of that fix; the spec change is
S3's. It is written so it also works under onefile once extraction
is done (the splash then covers the remaining import + server boot).

v1.4.0 B1 — non-blocking startup: the splash NEVER blocks the
Bridge. Bridge startup runs on a background thread
(:mod:`nse_bridge.main`) and reports named steps through
:class:`StartupProgress`; the main thread paints them here. Every
splash wait is bounded: status updates use a timeout
(:func:`_win32.set_text_timeout` — a hung pump can never freeze the
caller), and a watchdog hides the splash after
``SPLASH_MAX_LIFETIME_S`` even if startup is still going, so a
stuck boot can never leave a permanent dead "please wait" window.
The API server starts independently of the splash: a splash that
cannot be shown (or is auto-hidden) changes nothing about boot.

Implementation: stdlib-only, no third-party imports, so
``run_bridge.py`` can show it before importing the (heavy) bridge
package. Windows uses a raw Win32 window via ctypes (no tkinter —
the frozen exe excludes it, and pywebview is both slow to import
and the very thing that fails on the reporter's machine); other
platforms try tkinter and otherwise degrade to no splash.

Never raises: a splash that cannot be shown is a degraded UX, not a
startup failure.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from loguru import logger

from . import _win32

_SPLASH_CLASS = "NSEBridgeSplash"
_SPLASH_WIDTH = 460
_SPLASH_HEIGHT = 180

DEFAULT_TITLE = "NSE MT5 Bridge"
DEFAULT_STATUS = "Starting…"

#: The named startup steps, in order. ``main`` advances through them
#: on the boot thread; the splash renders "Step i/N — name" plus a
#: progress bar. Keep the names short — they are shown verbatim.
STARTUP_STEPS: tuple[str, ...] = (
    "Validating settings",
    "Preparing credentials",
    "Checking single instance",
    "Checking port",
    "Preparing bridge",
    "Starting server",
    "Waiting for dashboard",
    "Opening dashboard",
)

#: Bound on how long the splash may stay up on its own. The boot
#: deadline is 90 s on a frozen start (see ``main``) plus a bounded
#: dashboard probe, so 180 s outlives any healthy boot with margin —
#: and a stuck boot still detaches the splash instead of leaving a
#: dead window up forever. The tray icon remains the visible surface
#: after the splash detaches.
SPLASH_MAX_LIFETIME_S = 180.0


class StartupProgress:
    """Thread-safe named-step progress for bridge startup.

    The boot thread calls :meth:`advance` (and :meth:`complete` /
    :meth:`fail` for terminal states); the main (UI) thread reads
    :meth:`text` and :meth:`fraction` and paints the splash. This
    class makes no Win32/tkinter calls at all, so advancing it can
    never block on — or be blocked by — the splash window.
    """

    def __init__(self, steps: tuple[str, ...] = STARTUP_STEPS) -> None:
        self._steps = tuple(steps)
        self._lock = threading.Lock()
        self._position = 0  # 1-based index of the current step; 0 = not started
        self._current = ""
        self._done = False
        self._failed: str | None = None

    def advance(self, step: str) -> None:
        """Move to ``step`` (a name from ``STARTUP_STEPS``)."""
        with self._lock:
            if self._done or self._failed is not None:
                return
            self._current = step
            if step in self._steps:
                self._position = self._steps.index(step) + 1

    def complete(self) -> None:
        """Terminal state: startup finished."""
        with self._lock:
            self._done = True
            self._position = len(self._steps)

    def fail(self, message: str) -> None:
        """Terminal state: startup failed with ``message``."""
        with self._lock:
            self._failed = message

    @property
    def current_step(self) -> str:
        """Name of the step in progress ("" when done/failed)."""
        with self._lock:
            return "" if (self._done or self._failed is not None) else self._current

    @property
    def failed_message(self) -> str | None:
        with self._lock:
            return self._failed

    @property
    def done(self) -> bool:
        with self._lock:
            return self._done

    @property
    def fraction(self) -> float:
        """0.0–1.0: completed steps / total steps."""
        with self._lock:
            if self._done:
                return 1.0
            if not self._steps:
                return 0.0
            return max(0, self._position - 1) / len(self._steps)

    def text(self) -> str:
        """One-line status for the splash, e.g.
        ``"Step 3/8 — Checking port…"``. Never raises."""
        with self._lock:
            if self._failed is not None:
                return f"Failed: {self._failed}"
            if self._done:
                return "Ready"
            total = len(self._steps)
            if not self._current:
                return DEFAULT_STATUS
            return f"Step {self._position}/{total} — {self._current}…"


class Splash:
    """A shown (or unavailable) splash window."""

    def __init__(self, visible: bool = False) -> None:
        self.visible = visible
        self._status = DEFAULT_STATUS
        self._progress = 0.0

    def update(self, text: str) -> None:
        """Change the status line (no-op when unavailable)."""
        self._status = text

    def set_progress(self, fraction: float) -> None:
        """Move the progress bar (no-op when unavailable)."""
        try:
            self._progress = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            self._progress = 0.0

    def hide(self) -> None:
        """Close the window (no-op when unavailable)."""


class _Win32Splash(Splash):
    """Native Win32 splash window (ctypes, no dependencies)."""

    def __init__(self, title: str, status: str) -> None:
        super().__init__(visible=False)
        self._user32: Any = None
        self._hwnd = 0
        self._status_hwnd = 0
        self._progress_hwnd = 0
        self._pump_thread: threading.Thread | None = None
        self._wndproc = None
        self._fonts: list[int] = []
        self._closed = False
        try:
            self._create(title, status)
        except Exception:  # noqa: BLE001 — splash failure never blocks startup
            self.visible = False

    def _handler(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == _win32.WM_DESTROY:
            try:
                self._user32.PostQuitMessage(0)
            except Exception:  # noqa: BLE001, S110
                pass
            return 0
        return _win32.def_window_proc(self._user32, hwnd, msg, wparam, lparam)

    def _create(self, title: str, status: str) -> None:
        windll = _win32.windll()
        if windll is None:
            return
        user32 = windll.user32
        kernel32 = windll.kernel32
        self._user32 = user32
        self._wndproc = _win32.make_wndproc(user32, self._handler)
        hinstance = _win32.module_handle(kernel32)
        _win32.register_class(user32, _SPLASH_CLASS, self._wndproc, hinstance)
        x, y = _win32.center_on_screen(user32, _SPLASH_WIDTH, _SPLASH_HEIGHT)
        style = _win32.WS_OVERLAPPED | _win32.WS_CAPTION | _win32.WS_SYSMENU | _win32.WS_VISIBLE
        self._hwnd = _win32.create_window(
            user32, _SPLASH_CLASS, title, x, y, _SPLASH_WIDTH, _SPLASH_HEIGHT, hinstance, style
        )
        if not self._hwnd:
            return
        label_style = _win32.WS_CHILD | _win32.WS_VISIBLE
        title_hwnd = _win32.create_child(user32, "STATIC", title, self._hwnd, 20, 16, 420, 30, hinstance, label_style)
        self._status_hwnd = _win32.create_child(
            user32, "STATIC", status, self._hwnd, 20, 52, 420, 24, hinstance, label_style
        )
        # Real progress bar under the status line: the v1.3.x splash
        # showed only a text line, so a stuck boot looked identical
        # to a healthy slow one. The bar moves with StartupProgress.
        self._progress_hwnd = _win32.create_child(
            user32, "msctls_progress32", "", self._hwnd, 20, 82, 420, 14, hinstance, label_style
        )
        if self._progress_hwnd:
            _win32.set_progress_range(user32, self._progress_hwnd, 100)
        note1 = _win32.create_child(
            user32,
            "STATIC",
            "First launch can take up to a minute — Windows scans the app.",
            self._hwnd,
            20,
            106,
            420,
            20,
            hinstance,
            label_style,
        )
        note2 = _win32.create_child(
            user32,
            "STATIC",
            "The dashboard opens automatically when the bridge is ready.",
            self._hwnd,
            20,
            128,
            420,
            20,
            hinstance,
            label_style,
        )
        self._apply_fonts(user32, title_hwnd, self._status_hwnd, note1, note2)
        _win32.show_window(user32, self._hwnd)
        self._pump_thread = _win32.start_pump_thread(user32, "nse-splash-pump")
        self.visible = True

    def _apply_fonts(self, user32: Any, title_hwnd: int, status_hwnd: int, note1: int, note2: int) -> None:
        """Best-effort Segoe UI fonts; the default system font is fine
        when this fails (e.g. under a test fake)."""
        try:
            import ctypes

            windll = _win32.windll()
            if windll is None:
                return
            gdi32 = windll.gdi32
            gdi32.CreateFontW.restype = ctypes.c_void_p
            for hwnd, height, weight in (
                (title_hwnd, -18, 700),
                (status_hwnd, -14, 400),
                (note1, -12, 400),
                (note2, -12, 400),
            ):
                if not hwnd:
                    continue
                font = int(gdi32.CreateFontW(height, 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI"))
                if font:
                    self._fonts.append(font)
                    user32.SendMessageW(hwnd, _win32.WM_SETFONT, font, 1)
        except Exception:  # noqa: BLE001, S110
            pass

    def update(self, text: str) -> None:
        self._status = text
        if self.visible and self._status_hwnd and self._user32 is not None:
            # Bounded send (500 ms): a plain SetWindowTextW blocks
            # forever when the pump thread stops pumping — that is
            # how a status update could freeze the whole startup.
            _win32.set_text_timeout(self._user32, self._status_hwnd, text)

    def set_progress(self, fraction: float) -> None:
        super().set_progress(fraction)
        if self.visible and self._progress_hwnd and self._user32 is not None:
            _win32.set_progress_pos(self._user32, self._progress_hwnd, int(self._progress * 100))

    def hide(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.visible = False
        if self._hwnd and self._user32 is not None:
            _win32.close_window(self._user32, self._hwnd)
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)


class _TkSplash(Splash):
    """tkinter splash for non-Windows dev runs (never used frozen:
    the exe excludes tkinter). Tk calls run on their own thread and
    status updates cross via a queue — tkinter widgets are not
    thread-safe, so the main thread never touches them directly."""

    def __init__(self, title: str, status: str) -> None:
        super().__init__(visible=False)
        self._updates: queue.Queue[str] = queue.Queue()
        self._progress_updates: queue.Queue[float] = queue.Queue()
        self._close = threading.Event()
        self._root: Any = None
        self._status_var: Any = None
        self._progress_bar: Any = None
        self._thread = threading.Thread(target=self._run, args=(title, status), daemon=True, name="nse-splash-tk")
        self._thread.start()
        # The thread sets visible once the window exists; give it a
        # brief moment — a splash that appears a fraction late is
        # still infinitely better than a failed import blocking boot.
        self._thread.join(timeout=0.5)
        self.visible = self._root is not None

    def _run(self, title: str, status: str) -> None:
        try:
            import tkinter

            root = tkinter.Tk()
            self._root = root
            root.title(title)
            width, height = _SPLASH_WIDTH, _SPLASH_HEIGHT
            x = max(0, (root.winfo_screenwidth() - width) // 2)
            y = max(0, (root.winfo_screenheight() - height) // 2)
            root.geometry(f"{width}x{height}+{x}+{y}")
            root.resizable(False, False)
            tkinter.Label(root, text=title, font=("TkDefaultFont", 16, "bold")).pack(pady=(18, 4))
            self._status_var = tkinter.StringVar(value=status)
            tkinter.Label(root, textvariable=self._status_var, font=("TkDefaultFont", 11)).pack(pady=4)
            try:
                from tkinter import ttk

                self._progress_bar = ttk.Progressbar(
                    root, orient="horizontal", length=400, mode="determinate", maximum=100
                )
                self._progress_bar.pack(pady=4)
            except Exception:  # noqa: BLE001 — no ttk: text status still shows the step
                self._progress_bar = None
            tkinter.Label(root, text="First launch can take a while — the dashboard opens automatically.").pack(pady=4)
            root.protocol("WM_DELETE_WINDOW", self._close.set)
            self._poll(root)
            root.mainloop()
        except Exception:  # noqa: BLE001 — no tkinter / no display: no splash, startup continues
            self._root = None

    def _poll(self, root) -> None:  # type: ignore[no-untyped-def]
        if self._close.is_set():
            root.destroy()
            return
        try:
            while True:
                self._status_var.set(self._updates.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                value = self._progress_updates.get_nowait()
                if self._progress_bar is not None:
                    self._progress_bar["value"] = value
        except queue.Empty:
            pass
        root.after(100, self._poll, root)

    def update(self, text: str) -> None:
        self._status = text
        if self.visible:
            self._updates.put(text)

    def set_progress(self, fraction: float) -> None:
        super().set_progress(fraction)
        if self.visible:
            self._progress_updates.put(self._progress * 100)

    def hide(self) -> None:
        if self._closed_flag():
            return
        self.visible = False
        self._close.set()
        self._thread.join(timeout=2.0)

    def _closed_flag(self) -> bool:
        return self._close.is_set()


#: The currently shown splash, if any (module-level singleton so
#: ``run_bridge.py`` can show it before ``main()`` takes over).
_CURRENT: Splash | None = None


def _start_splash_watchdog(splash: Splash, max_lifetime_s: float) -> None:
    """Hide ``splash`` after ``max_lifetime_s`` even if startup is
    still going — the bounded wait that guarantees the splash can
    never become a permanent dead window. Only fires while this exact
    splash is still the current one; a normal hide/close (dashboard
    up) cancels it implicitly. Daemon thread: never blocks exit."""

    def _watch() -> None:
        time.sleep(max_lifetime_s)
        if _CURRENT is splash and splash.visible:
            logger.warning("[GUI] splash auto-hidden after {:.0f} s bounded wait", max_lifetime_s)
            hide_splash()

    threading.Thread(target=_watch, daemon=True, name="nse-splash-watchdog").start()


def show_splash(
    title: str = DEFAULT_TITLE, status: str = DEFAULT_STATUS, max_lifetime_s: float = SPLASH_MAX_LIFETIME_S
) -> Splash:
    """Show the splash window; idempotent — a second call returns the
    existing splash. Never raises. The splash auto-hides after
    ``max_lifetime_s`` (bounded wait) if the caller never hides it."""
    global _CURRENT
    if _CURRENT is not None and _CURRENT.visible:
        return _CURRENT
    try:
        if _win32.is_windows():
            splash: Splash = _Win32Splash(title, status)
        else:
            splash = _TkSplash(title, status)
    except Exception:  # noqa: BLE001
        splash = Splash(visible=False)
    _CURRENT = splash
    if splash.visible:
        logger.debug("[GUI] splash shown")
        _start_splash_watchdog(splash, max_lifetime_s)
    return splash


def update_splash(text: str) -> None:
    """Update the splash status line; no-op when no splash is shown."""
    if _CURRENT is not None:
        try:
            _CURRENT.update(text)
        except Exception:  # noqa: BLE001, S110
            pass


def set_splash_progress(fraction: float) -> None:
    """Move the splash progress bar; no-op when no splash is shown."""
    if _CURRENT is not None:
        try:
            _CURRENT.set_progress(fraction)
        except Exception:  # noqa: BLE001, S110
            pass


def hide_splash() -> None:
    """Close the splash window, if one is shown. Never raises."""
    global _CURRENT
    if _CURRENT is not None:
        try:
            _CURRENT.hide()
        except Exception:  # noqa: BLE001, S110
            pass
        _CURRENT = None


def splash_visible() -> bool:
    return _CURRENT is not None and _CURRENT.visible


__all__ = [
    "DEFAULT_STATUS",
    "DEFAULT_TITLE",
    "SPLASH_MAX_LIFETIME_S",
    "STARTUP_STEPS",
    "Splash",
    "StartupProgress",
    "hide_splash",
    "set_splash_progress",
    "show_splash",
    "splash_visible",
    "update_splash",
]
