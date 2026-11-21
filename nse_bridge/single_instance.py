"""Single-instance enforcement for the Bridge.

Two Bridge processes on one machine would both attach to the MT5
terminal (one IPC client wins, the other's calls fail unpredictably)
and both open outbound WS sessions to the Platform (device session
replacement fights). A named OS mutex prevents that deterministically
— before any socket bind or MT5 attach — with a clear message instead
of the generic "uvicorn failed to start" after a 10 s bind wait.

Windows: a named mutex under the ``Local\\`` namespace (per login
session — two Windows users may each run their own Bridge; a
``Global\\`` mutex would wrongly block that). Held for process
lifetime; the OS releases it on exit, so a crashed process never
blocks the next start.

Non-Windows (dev/Linux CI): no enforcement — concurrent dev runs are
legitimate there, and the port bind remains the natural guard.
"""

from __future__ import annotations

import sys
from typing import Any

MUTEX_NAME = r"Local\NSE-MT5-Bridge-SingleInstance"

ERROR_ALREADY_EXISTS = 183


def acquire_single_instance() -> tuple[Any, bool]:
    """Try to become the single Bridge instance.

    Returns ``(handle, acquired)``. ``handle`` is the OS object to keep
    alive for the process lifetime (a win32 HANDLE, or None on
    non-Windows); ``acquired`` False means another instance owns it.
    """
    if sys.platform != "win32":
        return None, True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return None, True  # cannot enforce — do not block startup
        already = kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        if already:
            kernel32.CloseHandle(handle)
            return None, False
        return handle, True
    except Exception:  # noqa: BLE001 — enforcement must never block startup
        return None, True
