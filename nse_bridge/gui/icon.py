"""Resolve the bundled application icon (``assets/icon.ico``).

The single .ico is consumed by three packaging/runtime paths and
must resolve in all of them:

* frozen exe (PyInstaller onefile/onedir): ``bridge.spec`` bundles
  ``assets/icon.ico`` as data, so it lives under ``sys._MEIPASS``;
* dev runs: the repo checkout's ``assets/icon.ico``;
* anything else: ``None`` — the caller falls back to the exe's own
  icon resource (``ExtractIconW``) or the stock application icon.

Pure path logic, no Win32 calls, so it is unit-testable on Linux.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The icon file name inside the bundled ``assets`` folder.
ICON_FILE = "icon.ico"


def app_icon_path() -> Path | None:
    """Absolute path of the bundled ``assets/icon.ico``, or ``None``
    when it cannot be found (the icon is best-effort — a missing
    file must never block startup)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen = Path(meipass) / "assets" / ICON_FILE
        if frozen.is_file():
            return frozen
    dev = Path(__file__).resolve().parents[2] / "assets" / ICON_FILE
    if dev.is_file():
        return dev
    return None


__all__ = ["ICON_FILE", "app_icon_path"]
