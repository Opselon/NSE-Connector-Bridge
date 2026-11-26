"""PyInstaller entrypoint for the NSE MT5 Bridge exe.

The splash window goes up HERE, before ``nse_bridge.main`` (and its
heavy imports: fastapi, uvicorn, numpy, MT5 driver) is even imported
— that import time is part of the invisible-startup gap v1.2.2 had.
``nse_bridge.gui.splash`` is stdlib-only, so showing it costs almost
nothing; ``main()`` takes over the same splash (idempotent) once it
runs. No splash for query subcommands (``status``/``health``/…),
``--help``, console/diagnostic runs, or ``--gui none`` — those have
their own visible surface (a console, or none by design). v1.4.0 B1:
the splash auto-hides after a bounded lifetime and bridge startup
runs on a background thread, so a stuck boot can never leave a dead
"please wait" window — the splash is a progress display, never a
gate on the server.
"""

import sys

_QUERY_SUBCOMMANDS = ("status", "health", "diagnostics", "mt5")


def _should_show_splash(argv: list[str]) -> bool:
    if not argv:
        return True
    if argv[0] in _QUERY_SUBCOMMANDS:
        return False
    if "-h" in argv or "--help" in argv:
        return False
    if "--console" in argv or "--diagnostic" in argv:
        return False
    if "--gui=none" in argv:
        return False
    if "--gui" in argv:
        try:
            if argv[argv.index("--gui") + 1] == "none":
                return False
        except IndexError:
            pass
    return True


if _should_show_splash(sys.argv[1:]):
    try:
        from nse_bridge.gui.splash import show_splash

        show_splash()
    except Exception:  # noqa: BLE001, S110 — a splash must never block the exe from starting
        pass

from nse_bridge.main import main

main()
