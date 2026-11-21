"""Bridge entrypoint: python -m nse_bridge [--port 8788] [--gui auto|webview|browser|none].

Serve modes (default experience is the desktop application):

    NSE-MT5-Bridge.exe                native desktop application
    NSE-MT5-Bridge.exe --console      foreground console service (no GUI)
    NSE-MT5-Bridge.exe --console --diagnostic
    NSE-MT5-Bridge.exe --gui none     headless service mode

Machine-readable operations (talk to a RUNNING Bridge — never a second
process; see nse_bridge.cli):

    NSE-MT5-Bridge.exe status --json
    NSE-MT5-Bridge.exe health --json
    NSE-MT5-Bridge.exe diagnostics --json
    NSE-MT5-Bridge.exe mt5 list --json
"""

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
from .gui.window import open_window
from .logging_setup import register_secret, setup_logging
from .mt5.driver import Mt5Driver
from .secrets import ensure_local_api_key


def build_parser() -> argparse.ArgumentParser:
    """CLI parser (Phase 6: factored out so flags are unit-testable
    without booting the bridge).

    Serve flags stay top-level and unchanged; query subcommands are
    optional — no subcommand means serve mode. Query subcommands never
    boot a Bridge; they talk to the running instance's loopback API.
    """
    parser = argparse.ArgumentParser(
        prog="nse-bridge",
        description="NSE MT5 Bridge — local client for NSE Platform (default: desktop application)",
    )
    parser.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8788)")
    parser.add_argument(
        "--gui",
        choices=["auto", "webview", "browser", "none"],
        default=None,
        help="GUI mode (default: auto — native window, browser fallback)",
    )
    parser.add_argument("--no-connect", action="store_true", help="skip auto MT5 connect at boot")
    parser.add_argument(
        "--console",
        action="store_true",
        help="foreground console mode: structured operational logs on the "
        "console (attached or allocated), NO GUI window; Ctrl+C stops cleanly",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="diagnostic console mode: enable DEBUG telemetry events "
        "(ARCHITECTURE-CONTRACT §8) and DEBUG logging; on a frozen "
        "Windows build (console=False exe) also attaches/allocates a "
        "console window so the logs are visible",
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="{status,health,diagnostics,mt5}")

    def _leaf(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        # One flag, one meaning: machine-readable JSON + predictable
        # exit codes (see nse_bridge.cli). Added to every leaf parser
        # so both `status --json` and `mt5 list --json` parse.
        p.add_argument("--json", action="store_true", help="machine-readable JSON output")
        return p

    _leaf(sub.add_parser("status", help="Bridge status (of the RUNNING instance)"))
    _leaf(sub.add_parser("health", help="Component health (of the RUNNING instance)"))
    _leaf(sub.add_parser("diagnostics", help="Guided diagnostics summary (of the RUNNING instance)"))
    mt5 = sub.add_parser("mt5", help="MetaTrader terminal operations (via the RUNNING instance)")
    mt5_sub = mt5.add_subparsers(dest="mt5_command", metavar="{list}")
    _leaf(mt5_sub.add_parser("list", help="List discovered MetaTrader terminals"))
    return parser


def _dispatch_query(argv: list[str]) -> int | None:
    """Run a query subcommand against the RUNNING bridge, if any.

    Returns the process exit code, or None when argv is a serve
    request (no subcommand) and the caller should boot the app.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.subcommand is None:
        return None
    if args.subcommand == "mt5" and args.mt5_command != "list":
        parser.error("mt5 requires a subcommand: list")
    from .cli import (
        CliError,
        emit_cli_error,
        run_diagnostics,
        run_health,
        run_mt5_list,
        run_status,
    )

    settings = load_settings()
    as_json = bool(getattr(args, "json", False))
    try:
        if args.subcommand == "status":
            return run_status(settings, as_json)
        if args.subcommand == "health":
            return run_health(settings, as_json)
        if args.subcommand == "diagnostics":
            return run_diagnostics(settings, as_json)
        return run_mt5_list(settings, as_json)
    except CliError as exc:
        return emit_cli_error(exc)


def _enable_diagnostic_console() -> None:
    """Best-effort console for a windowed (console=False) frozen exe.

    On Windows, a PyInstaller windowed exe has no console: attach the
    parent's console first (launched from a terminal → logs land
    there), allocate one only as a fallback, and rebind the std
    streams. Non-Windows and unfrozen runs already have a console, so
    this is a no-op there. Any failure is swallowed — diagnostic mode
    itself (telemetry + log level) works with or without the console.
    """
    from .console_mode import prepare_console

    prepare_console()


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Query subcommands are dispatched BEFORE any serve-side setup:
    # they must never acquire the single-instance mutex, touch MT5, or
    # bind a port (mission §D4: no second Bridge for status queries).
    query_exit = _dispatch_query(argv)
    if query_exit is not None:
        raise SystemExit(query_exit)

    args = build_parser().parse_args(argv)

    settings = load_settings()
    if args.diagnostic:
        settings.diagnostic_mode = True
        # INFO is the default level; --diagnostic wants the verbose
        # logs visible. An explicitly non-INFO level is respected.
        if settings.log_level == "INFO":
            settings.log_level = "DEBUG"
        _enable_diagnostic_console()
    if args.console:
        # Foreground console service: attach/allocate a console for
        # the structured logs, never open a GUI window.
        from .console_mode import prepare_console

        prepare_console()
        settings.gui_mode = "none"
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

    # Single instance, decided BEFORE any MT5 attach or port bind:
    # two processes would fight over the serialized MT5 IPC worker and
    # replace each other's Platform sessions. A second launch exits
    # fast with an actionable message instead of the generic bind
    # failure after the 10 s boot wait.
    from .single_instance import acquire_single_instance

    instance_lock, acquired = acquire_single_instance()
    if not acquired:
        print(
            "error: NSE MT5 Bridge is already running on this machine "
            f"(127.0.0.1:{settings.port}). Use `NSE-MT5-Bridge.exe status --json` "
            "to query it, or close the running instance first.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    driver = Mt5Driver(settings)
    ctx = BridgeContext(settings=settings, driver=driver)
    setup_logging(settings.resolved_log_dir, settings.log_level, extra_sink=ctx.logs.sink)
    if settings.diagnostic_mode:
        logger.info("[BOOT] diagnostic mode ON: DEBUG telemetry enabled, log level {}", settings.log_level)
    if args.console:
        logger.info("[BOOT] console mode: foreground service, GUI disabled (--gui none)")
    device_id = ctx.get_or_create_device_id()
    persist_settings(settings)

    app = create_app(ctx)  # register_dashboard happens inside create_app

    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None, access_log=False)
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
    # MT5 auto-connect starts AFTER the API is up (the old 0.5 s sleep
    # raced uvicorn boot): status endpoints are answerable from the
    # first millisecond of connect activity.
    if not args.no_connect:
        threading.Thread(target=_auto_connect, args=(driver,), daemon=True, name="mt5-boot").start()

    logger.info(
        "╔══════════════════════════════════════════════════╗\n"
        "║  NSE MT5 Bridge v{} — RUNNING                    ║\n"
        "║  Dashboard : {}/ui                               ║\n"
        "║  Swagger   : {}/docs                             ║\n"
        "║  Device ID : {}                  ║\n"
        "╚══════════════════════════════════════════════════╝",
        BRIDGE_VERSION,
        base_url,
        base_url,
        device_id,
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
    try:
        summary = driver.connect()
        if summary.get("ok"):
            logger.info("[BOOT] MT5 auto-connect OK (terminal={})", summary.get("terminal_version"))
        else:
            logger.error("[BOOT] MT5 auto-connect FAILED: {}", summary.get("error"))
    except Exception as exc:  # noqa: BLE001 — the boot thread must never crash the process; log and continue
        logger.error("[BOOT] MT5 auto-connect crashed: {}", exc)


if __name__ == "__main__":
    main()
