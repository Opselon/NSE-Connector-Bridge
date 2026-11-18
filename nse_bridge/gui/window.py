"""Native window via pywebview, with browser fallback."""

from __future__ import annotations

import webbrowser

from loguru import logger


def open_window(url: str, title: str = "NSE MT5 Bridge", size=(1280, 860)) -> str:
    """Try a native pywebview window; fall back to the default browser.

    Returns "webview" or "browser".
    """
    try:
        import webview  # pywebview

        webview.create_window(title, url, width=size[0], height=size[1], min_size=(900, 600))
        logger.info("[GUI] opening native window: {}", url)
        webview.start()
        return "webview"
    except Exception as exc:  # noqa: BLE001 — any pywebview failure falls back to the browser
        logger.warning("[GUI] pywebview unavailable ({}) — opening browser instead", exc)
        webbrowser.open(url)
        return "browser"
