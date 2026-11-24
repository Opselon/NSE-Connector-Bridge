"""Native window via pywebview, with robust browser fallback.

The v1.2.2 failure: the browser fallback called ``webbrowser.open()``
and ignored its return value, so on a machine where pywebview fails
AND the default-browser association is broken (the reporter's
Windows 11 Insider box), the process served with zero visible UI.
The fallback now goes through :mod:`nse_bridge.gui.browser`, which
checks the return value, retries, and tries Edge/Chrome/Firefox
explicitly — and reports failure honestly so the caller can show the
URL in a dialog and copy it to the clipboard.
"""

from __future__ import annotations

from loguru import logger

from .browser import open_dashboard

#: Return values of :func:`open_window`.
WINDOW_WEBVIEW = "webview"
WINDOW_BROWSER = "browser"
WINDOW_FAILED = "failed"


def open_window(url: str, title: str = "NSE MT5 Bridge", size=(1280, 860)) -> str:
    """Try a native pywebview window; fall back to the default browser.

    Returns ``"webview"``, ``"browser"``, or ``"failed"`` (both routes
    exhausted — the caller must surface the URL visibly).
    """
    try:
        import webview  # pywebview

        webview.create_window(title, url, width=size[0], height=size[1], min_size=(900, 600))
        logger.info("[GUI] opening native window: {}", url)
        webview.start()
        return WINDOW_WEBVIEW
    except Exception as exc:  # noqa: BLE001 — any pywebview failure falls back to the browser
        logger.warning("[GUI] pywebview unavailable ({}) — opening browser instead", exc)
    result = open_dashboard(url)
    if result.ok:
        logger.info("[GUI] dashboard opened in browser via {}", result.method)
        return WINDOW_BROWSER
    logger.error("[GUI] could not open the dashboard in any browser: {}", result.error)
    return WINDOW_FAILED


def destroy_native_window() -> bool:
    """Best-effort close of any open pywebview window (used by the
    tray's Quit action so the process can exit while the native
    window is up). Never raises; ``False`` when no window was open."""
    try:
        import webview  # pywebview

        windows = list(getattr(webview, "windows", []) or [])
        for window in windows:
            window.destroy()
        return bool(windows)
    except Exception:  # noqa: BLE001
        return False


__all__ = ["WINDOW_BROWSER", "WINDOW_FAILED", "WINDOW_WEBVIEW", "destroy_native_window", "open_window"]
