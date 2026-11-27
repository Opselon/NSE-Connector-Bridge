"""Native window via pywebview, with robust browser fallback.

The v1.2.2 failure: the browser fallback called ``webbrowser.open()``
and ignored its return value, so on a machine where pywebview fails
AND the default-browser association is broken (the reporter's
Windows 11 Insider box), the process served with zero visible UI.
The fallback now goes through :mod:`nse_bridge.gui.browser`, which
checks the return value, retries, and tries Edge/Chrome/Firefox
explicitly — and reports failure honestly so the caller can show the
URL in a dialog and copy it to the clipboard.

v1.4.0 B1 — bounded window open: the 2026-11-26 live failure was a
native window that never became responsive (the dashboard tab died
with ``chrome_unavailable``) while ``webview.start()`` blocked the
caller forever, so Windows flagged the Bridge "(Not Responding)"
and the process sat there with a hung UI. :func:`open_window` now
watches the open: if the window's ``shown`` event never fires
within ``open_timeout_s``, the watchdog closes the stuck window to
unstick ``webview.start()`` and the call returns ``"timeout"`` —
the caller then proceeds degraded (dialog with the dashboard URL +
clipboard, tray keeps serving) instead of hanging. A window that
DID appear is never touched by the watchdog: once the GUI loop is
running, the window is the user's to close.

pywebview constraints (verified against the pinned 6.2.1 source):
``start()`` MUST run on the main thread (it raises otherwise), so
the open stays on the caller's thread and only the watchdog is a
separate thread; ``webview.windows`` is populated by
``create_window`` already, so it cannot tell "loop running" from
"created but hung" — the window's ``events.shown`` is the signal.
``create_window`` takes no icon: the native window's icon comes
from ``webview.start(icon=...)`` — the WinForms backend uses that
file when set and otherwise falls back to the exe's first icon
resource (``ExtractIconW(sys.executable, 0)``) — so on Windows the
bundled ``assets/icon.ico`` is passed explicitly (it is the same
asset ``bridge.spec`` embeds in the exe); other platforms leave
the default.

Destroy gate (2026-11-27 hang audit, §3): ``Window.destroy()``
is decorated with ``@_shown_call`` (``webview/window.py:343``),
which waits up to 20 s for ``events.shown`` and RAISES
``WebViewException('Main window failed to start')`` without
destroying anything when the window never shows — so the
watchdog's unstick step was a no-op in exactly the case it
guards. :func:`destroy_native_window` therefore bypasses the
gate: it calls the backend directly
(``window.gui.destroy_window(window.uid)`` — the same call
``destroy()`` makes once the gate passes, ``webview/window.py:349``,
and the WinForms/GTK/Qt backends expose it as a module-level
function that does not wait for ``shown``), degrading to
``window.destroy()`` when the backend is unavailable. Residual
limit, stated honestly: the WinForms backend registers the form
in ``BrowserView.instances`` only AFTER its constructor returns,
so a stall inside the constructor itself still has no instance
to destroy — no Python-side call can unstick that corner; the
bypass covers every case where the form was constructed but
``shown`` never fired.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from loguru import logger

from .browser import open_dashboard
from .icon import app_icon_path

#: Return values of :func:`open_window`.
WINDOW_WEBVIEW = "webview"
WINDOW_BROWSER = "browser"
WINDOW_FAILED = "failed"
#: The native window never became responsive within the bounded
#: open wait (see :func:`open_window`).
WINDOW_TIMEOUT = "timeout"

#: Bound on waiting for the native window to appear. WebView2
#: initialisation on a cold machine can take seconds; 30 s is far
#: past that, and past it the window is hung, not slow.
WINDOW_OPEN_TIMEOUT_S = 30.0


def _window_shown_event(window: Any) -> Any | None:
    """The window's ``events.shown`` event, or ``None``.

    pywebview sets it when the native window appears. ``None`` when
    the window object has no such event (older pywebview / test
    fakes) — the watchdog then falls back to the ``start(func)``
    callback as its "loop started" signal.
    """
    try:
        events = getattr(window, "events", None)
        shown = getattr(events, "shown", None)
        if shown is not None and callable(getattr(shown, "wait", None)) and callable(getattr(shown, "is_set", None)):
            return shown
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _window_open_watchdog(
    window: Any,
    started: threading.Event,
    done: threading.Event,
    timed_out: threading.Event,
    timeout_s: float,
) -> None:
    """Wait for the native window to appear; unstick a hung open.

    Stands down at once when the window's ``shown`` event fires (or,
    when the window has no such event, when the ``start(func)``
    callback fires), or when :func:`open_window` returns (``done``).
    Only when NO signal arrives within ``timeout_s`` does it set
    ``timed_out`` and destroy the stuck window, which lets
    ``webview.start()`` return so the caller can proceed degraded.
    Never raises.
    """
    shown = _window_shown_event(window)

    def _appeared() -> bool:
        if shown is not None:
            return bool(shown.is_set())
        return started.is_set()

    deadline = time.monotonic() + timeout_s
    while not done.is_set():
        if _appeared():
            return
        if time.monotonic() >= deadline:
            break
        done.wait(0.25)
    if done.is_set() or _appeared():
        return
    timed_out.set()
    logger.error("[GUI] native window did not appear within {:.0f} s — closing the stuck window", timeout_s)
    destroy_native_window()


def open_window(
    url: str, title: str = "NSE MT5 Bridge", size=(1280, 860), open_timeout_s: float = WINDOW_OPEN_TIMEOUT_S
) -> str:
    """Try a native pywebview window; fall back to the default browser.

    Must be called from the main thread (pywebview requires it).
    Returns ``"webview"``, ``"browser"``, ``"failed"`` (both routes
    exhausted — the caller must surface the URL visibly), or
    ``"timeout"`` (the native window never became responsive within
    ``open_timeout_s`` and was closed — the bridge keeps serving).
    """
    try:
        import webview  # pywebview

        started = threading.Event()
        done = threading.Event()
        timed_out = threading.Event()
        try:
            window = webview.create_window(title, url, width=size[0], height=size[1], min_size=(900, 600))
            logger.info("[GUI] opening native window: {}", url)
            watchdog = threading.Thread(
                target=_window_open_watchdog,
                args=(window, started, done, timed_out, open_timeout_s),
                daemon=True,
                name="nse-window-watchdog",
            )
            watchdog.start()
            # ``start(func)`` invokes func as the GUI loop starts;
            # the watchdog's real signal is the window's shown event.
            # Windows only: hand pywebview the bundled icon — its
            # WinForms backend uses it for the window/taskbar icon,
            # falling back to the exe's icon resource without it.
            start_kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                icon_path = app_icon_path()
                if icon_path is not None:
                    start_kwargs["icon"] = str(icon_path)
            webview.start(lambda: started.set(), **start_kwargs)
            if timed_out.is_set():
                return WINDOW_TIMEOUT
            return WINDOW_WEBVIEW
        except Exception as exc:  # noqa: BLE001 — any pywebview failure falls back to the browser
            logger.warning("[GUI] pywebview unavailable ({}) — opening browser instead", exc)
        finally:
            done.set()
    except Exception as exc:  # noqa: BLE001 — any pywebview failure falls back to the browser
        logger.warning("[GUI] pywebview unavailable ({}) — opening browser instead", exc)
    result = open_dashboard(url)
    if result.ok:
        logger.info("[GUI] dashboard opened in browser via {}", result.method)
        return WINDOW_BROWSER
    logger.error("[GUI] could not open the dashboard in any browser: {}", result.error)
    return WINDOW_FAILED


def _destroy_window_via_backend(window: Any) -> bool:
    """Destroy one window via the pywebview backend, bypassing the
    ``@_shown_call`` gate on ``Window.destroy()``.

    Real pywebview 6.2.1 waits up to 20 s for ``events.shown`` in
    ``destroy()`` and raises without destroying anything when the
    window never shows. The backend's own ``destroy_window(uid)``
    is the same call ``destroy()`` makes once the gate passes, and
    it does not wait for ``shown`` (WinForms/GTK/Qt all expose it
    as a module-level function on the ``window.gui`` backend).
    ``True`` when the backend call was attempted; ``False`` when
    the backend is unavailable (no ``gui`` attribute, no
    ``destroy_window``, no ``uid``) or the backend call raised —
    the caller then degrades to ``window.destroy()``. Never raises.
    """
    try:
        gui = getattr(window, "gui", None)
        destroy_window = getattr(gui, "destroy_window", None)
        uid = getattr(window, "uid", None)
        if destroy_window is None or uid is None:
            return False
        destroy_window(uid)
        return True
    except Exception:  # noqa: BLE001 — backend failure degrades to window.destroy()
        return False


def destroy_native_window() -> bool:
    """Best-effort close of any open pywebview window (used by the
    tray's Quit action so the process can exit while the native
    window is up, and by the open watchdog to unstick a hung
    ``webview.start()``). Never raises; ``False`` when no window
    was open.

    Tries the backend destroy first so a window that never showed
    is still destroyed (see the module docstring — plain
    ``window.destroy()`` refuses that case); degrades to
    ``window.destroy()`` per window when the backend is
    unavailable or its call failed.
    """
    try:
        import webview  # pywebview

        windows = list(getattr(webview, "windows", []) or [])
        for window in windows:
            if not _destroy_window_via_backend(window):
                try:
                    window.destroy()
                except Exception:  # noqa: BLE001, S110 — one stuck window must not block the rest
                    pass
        return bool(windows)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "WINDOW_BROWSER",
    "WINDOW_FAILED",
    "WINDOW_OPEN_TIMEOUT_S",
    "WINDOW_TIMEOUT",
    "WINDOW_WEBVIEW",
    "destroy_native_window",
    "open_window",
]
