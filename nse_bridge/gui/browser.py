"""Robust browser auto-open for the dashboard.

The v1.2.2 failure: :func:`webbrowser.open` returns ``False`` on
failure WITHOUT raising, and the old code ignored the return value —
so a broken default-browser association left the process serving on
``127.0.0.1:8788`` with zero visible UI. This module treats the return
value as the signal it is: retry the default browser, then launch
Edge/Chrome/Firefox explicitly, and only then report failure so the
caller can show the URL in a dialog and copy it to the clipboard.

Also owns the dashboard URL itself: a bridge that is not enrolled yet
lands on the Connection screen (``/ui#platform`` — the Platform
screen holds the connection configuration and the guided setup
overlay opens over it) instead of the Overview, so a first-run user
starts at setup, not at a wall of dashes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass

from loguru import logger

#: How many times to retry the default browser before falling back.
DEFAULT_BROWSER_RETRIES = 3
#: Pause between default-browser attempts (seconds).
RETRY_DELAY_S = 0.5

#: Explicit-browser candidates, in preference order. ``shutil.which``
#: covers the PATH case; the Windows ``Program Files`` paths cover a
#: browser that is installed but not on PATH (the common Edge case).
_EXPLICIT_BROWSERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("edge", ("msedge", "microsoft-edge")),
    ("chrome", ("chrome", "google-chrome", "google-chrome-stable")),
    ("firefox", ("firefox",)),
)


def _windows_known_paths() -> list[tuple[str, str]]:
    """Well-known install locations for Edge/Chrome on Windows."""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LocalAppData", ""),
    ]
    candidates: list[tuple[str, str]] = []
    for name, rel in (
        ("edge", r"Microsoft\Edge\Application\msedge.exe"),
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
    ):
        for root in roots:
            if root:
                candidates.append((name, os.path.join(root, rel)))
    return candidates


@dataclass
class BrowserOpenResult:
    """Outcome of :func:`open_dashboard`."""

    ok: bool
    #: How the browser was opened: ``default``, ``edge``, ``chrome``,
    #: ``firefox`` — empty when nothing worked.
    method: str = ""
    #: Total open attempts made (default-browser retries + explicit).
    attempts: int = 0
    #: Last failure detail, for the log / error dialog.
    error: str = ""


def dashboard_url(base_url: str, configured: bool) -> str:
    """Dashboard URL for ``base_url``.

    ``configured`` is False on a fresh (unenrolled) bridge: the URL
    deep-links to the Connection screen (``/ui#platform``) so the
    user lands on setup instead of the Overview.
    """
    url = f"{base_url.rstrip('/')}/ui"
    return url if configured else f"{url}#platform"


def _try_default_browser(url: str, retries: int = DEFAULT_BROWSER_RETRIES) -> BrowserOpenResult:
    attempts = 0
    last_error = ""
    for attempt in range(retries):
        attempts += 1
        try:
            if webbrowser.open(url):
                logger.info("[GUI] browser opened via default browser (attempt {})", attempt + 1)
                return BrowserOpenResult(ok=True, method="default", attempts=attempts)
            last_error = "webbrowser.open() returned False"
        except Exception as exc:  # noqa: BLE001 — any opener failure falls through to the next attempt
            last_error = str(exc)
        logger.warning("[GUI] default browser open failed (attempt {}): {}", attempt + 1, last_error)
        if attempt + 1 < retries:
            time.sleep(RETRY_DELAY_S)
    return BrowserOpenResult(ok=False, attempts=attempts, error=last_error)


def _try_explicit_browser(url: str) -> BrowserOpenResult:
    """Launch Edge/Chrome/Firefox directly, by executable.

    ``subprocess.Popen`` returning without an exception means the
    browser process was spawned — that is the strongest signal
    available without a browser-automation channel.
    """
    attempts = 0
    last_error = "no explicit browser executable found"
    candidates: list[tuple[str, str]] = []
    for method, names in _EXPLICIT_BROWSERS:
        for name in names:
            path = shutil.which(name)
            if path:
                candidates.append((method, path))
    if sys.platform == "win32":
        for method, path in _windows_known_paths():
            if os.path.isfile(path):
                candidates.append((method, path))
    seen: set[str] = set()
    for method, path in candidates:
        if path in seen:
            continue
        seen.add(path)
        attempts += 1
        try:
            subprocess.Popen(  # nosec B603 — list argv, no shell; exe resolved, URL is the bridge's own dashboard
                [path, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            logger.info("[GUI] browser opened via explicit {} ({})", method, path)
            return BrowserOpenResult(ok=True, method=method, attempts=attempts)
        except Exception as exc:  # noqa: BLE001 — try the next candidate
            last_error = f"{method} ({path}): {exc}"
            logger.warning("[GUI] explicit browser launch failed: {}", last_error)
    return BrowserOpenResult(ok=False, attempts=attempts, error=last_error)


def open_dashboard(url: str, retries: int = DEFAULT_BROWSER_RETRIES) -> BrowserOpenResult:
    """Open ``url`` in a browser, with retries + explicit fallbacks.

    Never raises. ``result.ok`` False means every route failed — the
    caller must then show the URL in a dialog AND copy it to the
    clipboard, never fail silently.
    """
    result = _try_default_browser(url, retries=retries)
    if result.ok:
        return result
    logger.warning(
        "[GUI] default browser failed after {} attempt(s); trying Edge/Chrome/Firefox directly", result.attempts
    )
    fallback = _try_explicit_browser(url)
    fallback.attempts += result.attempts
    if fallback.ok:
        return fallback
    fallback.error = fallback.error or result.error
    logger.error("[GUI] could not open a browser for {}: {}", url, fallback.error)
    return fallback


def wait_for_http_ready(url: str, timeout_s: float = 5.0, interval_s: float = 0.5) -> bool:
    """Poll ``url`` until it answers HTTP 2xx, or ``timeout_s`` elapses.

    ``uvicorn`` reporting "started" only means the socket bound; the
    app itself can still be failing (e.g. an exception at startup),
    in which case opening the browser shows a dead page. The caller
    treats False as a startup failure with a visible dialog.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # nosec B310 — localhost readiness probe
                if 200 <= response.status < 300:
                    return True
        except Exception:  # noqa: BLE001, S110 — not up yet; retry until the deadline
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


__all__ = ["BrowserOpenResult", "dashboard_url", "open_dashboard", "wait_for_http_ready"]
