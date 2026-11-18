"""Bridge entrypoint: python -m nse_bridge [--port 8788] [--gui auto|webview|browser|none]."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

import uvicorn
from loguru import logger

from . import BRIDGE_VERSION
from .api.app import create_app
from .config import load_settings, persist_settings, validate_bind_host
from .engine.common import BridgeContext
from .gui.dashboard import register_dashboard
from .gui.window import open_window
from .logging_setup import register_secret, setup_logging
from .mt5.driver import Mt5Driver
from .secrets import ensure_local_api_key


def build_parser() -> argparse.ArgumentParser:
    """CLI parser (Phase 6: factored out so the --diagnostic flag is
    unit-testable without booting the bridge)."""
    parser = argparse.ArgumentParser(prog="nse-bridge", description="NSE MT5 Bridge")
    parser.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8788)")
    parser.add_argument(
        "--gui", choices=["auto", "webview", "browser", "none"], default=None,
        help="GUI mode (default: auto — native window, browser fallback)",
    )
    parser.add_argument("--no-connect", action="store_true", help="skip auto MT5 connect at boot")
    parser.add_argument(
        "--diagnostic", action="store_true",
        help="diagnostic console mode: enable DEBUG telemetry events "
        "(ARCHITECTURE-CONTRACT §8) and DEBUG logging; on a frozen "
        "Windows build (console=False exe) also allocates a console "
        "window so the logs are visible",
    )
    return parser


def _enable_diagnostic_console() -> None:
    """Best-effort console for a windowed (console=False) frozen exe.

    On Windows, a PyInstaller windowed exe has no console: AllocConsole
    gives --diagnostic somewhere to write. Non-Windows and unfrozen
    runs already have a console, so this is a no-op there. Any failure
    is swallowed — diagnostic mode itself (telemetry + log level)
    works with or without the console.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.AllocConsole()
    except Exception:  # noqa: BLE001, S110 — console allocation is best-effort only
        pass


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    settings = load_settings()
    if args.diagnostic:
        settings.diagnostic_mode = True
        # INFO is the default level; --diagnostic wants the verbose
        # logs visible. An explicitly non-INFO level is respected.
        if settings.log_level == "INFO":
            settings.log_level = "DEBUG"
        _enable_diagnostic_console()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.gui:
        settings.gui_mode = args.gui

    # --host is kept, with guardrails: a non-loopback bind requires
    # explicit authorization (allow_non_loopback) AND the mandatory
    # X-API-Key. Without the flag, refuse to start — never bind
    # 0.0.0.0 by accident.
    try:
        validate_bind_host(settings.host, allow_non_loopback=settings.allow_non_loopback)
    except ValueError as exc:
        print(f"error: refusing to start: {exc}", file=sys.stderr)
        raise SystemExit(2)
    # Mandatory local API key (auto-generated on first run, stored in
    # the secret store). Fail closed before serving anything.
    ensure_local_api_key(settings)
    register_secret(settings.server_token)
    register_secret(settings.local_api_key)

    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    setup_logging(settings.resolved_log_dir, settings.log_level, extra_sink=ctx.logs.sink)
    if settings.diagnostic_mode:
        logger.info("[BOOT] diagnostic mode ON: DEBUG telemetry enabled, log level {}", settings.log_level)
    device_id = ctx.get_or_create_device_id()
    persist_settings(settings)

    app = create_app(ctx)
    register_dashboard(app)

    if not args.no_connect:
        threading.Thread(target=_auto_connect, args=(driver,), daemon=True, name="mt5-boot").start()

    config = uvicorn.Config(
        app, host=settings.host, port=settings.port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    server_thread.start()

    base_url = f"http://{settings.host}:{settings.port}"
    deadline = __import__("time").time() + 10
    while not server.started and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    if not server.started:
        logger.error("[BOOT] uvicorn failed to start — exiting")
        return

    logger.info(
        "╔══════════════════════════════════════════════════╗\n"
        "║  NSE MT5 Bridge v{} — RUNNING                    ║\n"
        "║  Dashboard : {}/ui                               ║\n"
        "║  Swagger   : {}/docs                             ║\n"
        "║  Device ID : {}                  ║\n"
        "╚══════════════════════════════════════════════════╝",
        BRIDGE_VERSION, base_url, base_url, device_id,
    )

    mode = settings.gui_mode
    try:
        if mode == "none":
            server_thread.join()
        elif mode == "browser":
            webbrowser.open(f"{base_url}/ui")
            server_thread.join()
        else:
            result = open_window(f"{base_url}/ui")
            if result == "browser" and mode == "auto":
                server_thread.join()  # keep serving until killed
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("[BOOT] shutting down")
        server.should_exit = True
        server_thread.join(timeout=5)
        driver.disconnect()


def _auto_connect(driver: Mt5Driver) -> None:
    import time

    time.sleep(0.5)  # let uvicorn boot first
    try:
        summary = driver.connect()
        if summary.get("ok"):
            logger.info("[BOOT] MT5 auto-connect OK (terminal={})", summary.get("terminal_version"))
        else:
            logger.error("[BOOT] MT5 auto-connect FAILED: {}", summary.get("error"))
    except Exception as exc:
        logger.error("[BOOT] MT5 auto-connect crashed: {}", exc)


if __name__ == "__main__":
    main()
