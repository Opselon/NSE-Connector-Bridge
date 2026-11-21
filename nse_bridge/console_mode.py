"""Console preparation for foreground/CLI modes on Windows.

A PyInstaller windowed exe (``console=False``) has no console and its
``sys.stdout``/``sys.stderr`` are ``None`` — logs vanish. Two modes
need a working console:

- ``--console``: foreground server with structured operational logs.
- ``--diagnostic``: DEBUG telemetry + visible logs (existing flag,
  kept backwards-compatible).

Strategy: attach to the PARENT console first (launched from a terminal
→ logs land in that terminal), allocate a fresh console only as a
fallback (double-clicked exe). Then rebind the std streams to
``CONOUT$``/``CONIN$`` so loguru/uvicorn/print reach it. On non-Windows
or unfrozen runs a console already exists — everything is a no-op.

Every step is best-effort: mode semantics (foreground serving, DEBUG
telemetry) work with or without an attached console.
"""

from __future__ import annotations

import io
import os
import sys

ATTACH_PARENT_PROCESS = -1  # win32 ATTACH_PARENT_PROCESS


def prepare_console() -> bool:
    """Attach/allocate a console and rebind std streams.

    Returns True when a console is (probably) available for output.
    Never raises — console cosmetics must not break startup.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return True  # a real console already exists
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Attach the caller's terminal when unattached; allocate one
        # only as a fallback (double-clicked exe).
        if kernel32.GetConsoleWindow() == 0 and not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            kernel32.AllocConsole()
        # UTF-8 codepage: the streams we bind below WRITE utf-8, and
        # the default codepage (e.g. CP850) renders that as mojibake
        # (box-drawing → "ΓòÉ…"). 65001 makes the console read it back
        # correctly; SetConsoleCP covers typed input the same way.
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
        _rebind_stream("stdin", "CONIN$", "r")
        _rebind_stream("stdout", "CONOUT$", "w")
        _rebind_stream("stderr", "CONOUT$", "w")
        return True
    except Exception:  # noqa: BLE001 — console allocation is best-effort only
        return False


def _rebind_stream(name: str, device: str, mode: str) -> None:
    """Point sys.<name> at a win32 console device (windowed exe: None)."""
    try:
        # O_BINARY is Windows-only (POSIX has no binary/text split);
        # getattr keeps this importable and type-checkable on Linux.
        binary = getattr(os, "O_BINARY", 0)
        flags = (os.O_RDONLY if mode == "r" else os.O_WRONLY) | binary
        fd = os.open(device, flags)
        stream = io.TextIOWrapper(
            io.FileIO(fd, mode, closefd=True),
            encoding="utf-8",
            errors="replace",
            write_through=True,
        )
        setattr(sys, name, stream)
    except Exception:  # noqa: BLE001, S110 — keep whatever stream exists
        pass
