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

Visible startup (v1.3.0): this is a windowed exe (``console=False``),
so NOTHING about startup may live only in stderr or the log — the
user in v1.2.2 saw no console, no window, no browser while the
process sat in Task Manager. The order is therefore:

    splash window (first, before any slow work) → single-instance
    check (a second launch BRINGS the running UI to front instead of
    dying silently) → port pre-flight (a taken port gets a visible
    dialog, not a 90 s timeout) → server boot → readiness probe →
    tray icon + native window or browser (with retries + explicit
    Edge/Chrome fallback, and a dialog + clipboard copy when every
    browser route fails).

v1.4.0 B1 — non-blocking startup: the splash must NEVER block the
Bridge, and the Bridge must never block on the splash. Server boot
therefore runs on a background thread (:func:`_boot`) while the
main thread owns the UI: it paints the boot thread's named steps
(:class:`nse_bridge.gui.splash.StartupProgress`) onto the splash,
enforces the overall bounded wait, and only then opens the native
window / browser. The dashboard/API server starts independently
of the splash — a splash that fails to show, or is auto-hidden by
its bounded lifetime, changes nothing about boot. Every wait on
the path has a BOUND and a terminal state: the server-boot deadline
fails visibly, the dashboard probe degrades (the bridge keeps
serving with a clear message) instead of killing a running server,
and the native-window open is watchdog-bounded (see
:mod:`nse_bridge.gui.window`) — never an infinite dead "please
wait".

Onefile caveat: no Python runs before a onefile exe finishes
extracting, so the splash cannot cover the cold extraction itself —
see ``nse_bridge.gui.splash`` for the onedir recommendation (S3 owns
``bridge.spec``).
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import uvicorn
from loguru import logger

from . import BRIDGE_VERSION
from .api.app import create_app
from .config import load_settings, persist_settings, validate_bind_host
from .engine.common import BridgeContext
from .gui.browser import dashboard_url, open_dashboard, wait_for_http_ready
from .gui.dialogs import copy_to_clipboard, show_error_dialog, show_info_dialog
from .gui.splash import (
    STARTUP_STEPS,
    StartupProgress,
    hide_splash,
    set_splash_progress,
    show_splash,
    update_splash,
)
from .gui.tray import open_folder, start_tray
from .gui.window import WINDOW_BROWSER, WINDOW_FAILED, WINDOW_TIMEOUT, destroy_native_window, open_window
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


def _fail_visible(message: str, title: str, exit_code: int, *, gui_visible: bool) -> None:
    """Fatal startup error: log it, print it, AND show it.

    A windowed exe has no visible stderr — without the dialog the
    user sees a process that silently vanishes. The dialog is shown
    only when a GUI is expected (``--console`` / ``--gui none`` runs
    already have a visible console or are headless services); the
    exit code is non-zero either way. Never returns.
    """
    logger.error("[BOOT] {}", message)
    print(f"error: {message}", file=sys.stderr)
    if gui_visible:
        show_error_dialog(title, message)
    raise SystemExit(exit_code)


def _check_port_free(host: str, port: int) -> str | None:
    """Pre-flight bind check: ``None`` when the port is free, else a
    human-readable reason.

    The uvicorn boot wait is up to 90 s on a cold frozen start, so a
    taken port must be caught HERE with a specific message — waiting
    for the boot deadline to report "uvicorn failed to start" is how
    v1.2.2 turned a trivial conflict into a silent 90 s hang. (A
    bind-then-release check has a small TOCTOU window; uvicorn
    failing to bind anyway is still caught by the boot deadline.)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return None
    except OSError as exc:
        if exc.errno in (98, 10048):  # EADDRINUSE on Linux / WSAEADDRINUSE on Windows
            return (
                f"Port {port} is already in use on {host} — another program "
                "(possibly another NSE MT5 Bridge) is using it.\n\n"
                "Close the other program, or start the bridge on a free port:\n"
                f"  NSE-MT5-Bridge.exe --port {port + 1}"
            )
        return f"Cannot bind {host}:{port}: {exc}"
    finally:
        sock.close()


def _bring_existing_to_front(base_url: str, *, configured: bool, gui_visible: bool = True) -> None:
    """Second launch while the bridge is already running: open the
    RUNNING instance's dashboard (bringing the UI to front) instead
    of exiting silently — the v1.2.2 behaviour that made every
    relaunch look like "nothing happens".

    When no browser can be opened, the URL is shown in a dialog AND
    copied to the clipboard so the user can paste it manually.

    Headless runs (``--gui none`` / ``--console``) never open a browser
    and never show a dialog: a modal MessageBox here would block the
    process until a user dismisses it, and on a headless runner there
    is no user — the second launch hung until the 40-min CI job
    timeout. The URL is printed (the console IS the surface) and the
    process exits 0 promptly.
    """
    url = dashboard_url(base_url, configured)
    if not gui_visible:
        print(f"NSE MT5 Bridge is already running — dashboard: {url}")
        return
    result = open_dashboard(url)
    if result.ok:
        logger.info("[BOOT] bridge already running — dashboard opened in browser via {}", result.method)
        print(f"NSE MT5 Bridge is already running — dashboard opened in your browser ({url})")
        return
    copy_to_clipboard(url)
    logger.warning("[BOOT] bridge already running — no browser could be opened for {}", url)
    print(
        "NSE MT5 Bridge is already running, but no browser could be opened.\n"
        f"Open the dashboard manually: {url} (copied to the clipboard)",
        file=sys.stderr,
    )
    show_info_dialog(
        "NSE MT5 Bridge is already running",
        "The bridge is already running on this machine.\n\n"
        "The dashboard could not be opened automatically — open this address manually:\n"
        f"{url}\n\n(The address was copied to the clipboard.)",
    )


def _dashboard_unreachable(url: str, detail: str | None = None) -> None:
    """The server is up but no browser could be opened (or the native
    window never became responsive): show the URL in a dialog AND
    copy it to the clipboard. The server keeps running (the tray icon
    stays available); this is a last resort, never a silent failure."""
    copy_to_clipboard(url)
    logger.error("[BOOT] server is up but no browser could be opened for {}", url)
    message = (
        "The bridge is running, but the dashboard could not be opened.\n\n"
        "Open this address manually:\n"
        f"{url}\n\n(The address was copied to the clipboard.)"
    )
    if detail:
        message = (
            "The bridge is running, but the dashboard could not be opened.\n\n"
            f"{detail}\n\n"
            "Open this address manually:\n"
            f"{url}\n\n(The address was copied to the clipboard.)"
        )
    show_error_dialog(
        "NSE MT5 Bridge — dashboard could not be opened",
        message,
    )


def _dashboard_degraded(url: str, warning: str) -> None:
    """The server is up but the dashboard did not answer its readiness
    probe in time: the bridge keeps serving (degraded, not fatal —
    killing a running server over a slow first paint was the v1.3.x
    startup crash), but the user is told exactly what is wrong, with
    the URL on the clipboard to retry manually."""
    copy_to_clipboard(url)
    logger.warning("[BOOT] dashboard degraded: {}", warning)
    show_info_dialog(
        "NSE MT5 Bridge — dashboard is slow to answer",
        "The bridge is running, but the dashboard did not answer yet:\n\n"
        f"{warning}\n\n"
        "Open this address manually:\n"
        f"{url}\n\n(The address was copied to the clipboard.)",
    )


def _serve_until_quit(server_thread: threading.Thread, quit_event: threading.Event) -> None:
    """Serve while polling the tray's Quit event (a plain ``join()``
    would never notice it). Returns when the server thread ends or
    the user picks Quit in the tray."""
    while server_thread.is_alive() and not quit_event.is_set():
        server_thread.join(timeout=0.5)


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Query subcommands are dispatched BEFORE any serve-side setup:
    # they must never acquire the single-instance mutex, touch MT5,
    # bind a port — or show a splash (mission §D4: no second Bridge
    # for status queries).
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

    # The splash goes up as soon as the effective GUI mode is known
    # (settings load is local and fast) and stays up until the
    # dashboard is open — before validation, the secret store, MT5,
    # and the server boot, which are the slow steps. Console and
    # headless runs never show one: their console IS the surface.
    gui_visible = settings.gui_mode != "none" and not args.console
    if gui_visible:
        show_splash(f"NSE MT5 Bridge v{BRIDGE_VERSION}")
    try:
        _run(args, settings, gui_visible)
    finally:
        hide_splash()


@dataclass
class _BootOutcome:
    """Result of the background boot thread (:func:`_boot`).

    ``kind`` is ``"ready"`` (server up, serve it), ``"already_running"``
    (another instance owns the mutex — bring its UI to front), or
    ``"failed"`` (terminal, with the exact ``message`` for the visible
    error dialog + ``exit_code``). A failed boot never raises
    ``SystemExit`` on the boot thread — raising there would only kill
    the thread, not the process — so the main thread performs the
    visible failure. ``warning`` is a non-fatal degraded note (the
    dashboard probe failed but the server is up): the bridge serves
    anyway and the user is told. ``instance_lock`` keeps the
    single-instance mutex handle alive for the process lifetime.
    """

    kind: str
    server: Any = None
    server_thread: threading.Thread | None = None
    driver: Any = None
    ctx: Any = None
    device_id: str = ""
    dash_url: str = ""
    base_url: str = ""
    configured: bool = False
    instance_lock: Any = None
    message: str = ""
    exit_code: int = 1
    title: str = "NSE MT5 Bridge — cannot start"
    warning: str = ""

    @classmethod
    def failed(cls, message: str, exit_code: int = 1, title: str = "NSE MT5 Bridge — cannot start") -> _BootOutcome:
        return cls(kind="failed", message=message, exit_code=exit_code, title=title)


#: Bound on the dashboard readiness probe. The old 5 s failed a
#: cold first paint and killed the process; 10 s still bounds the
#: wait, and a miss is now degraded (serve anyway), not fatal.
_DASHBOARD_PROBE_TIMEOUT_S = 10.0

#: Slack past the server-boot deadline + probe that the main thread
#: allows the boot thread overall — a backstop for steps with no
#: internal bound of their own (settings, app creation).
_BOOT_OVERALL_SLACK_S = 30.0


def _boot(args: argparse.Namespace, settings, progress: StartupProgress) -> _BootOutcome:  # type: ignore[no-untyped-def]
    """Boot the bridge server on the BACKGROUND thread.

    Everything up to "server ready" lives here: validation,
    credentials, single-instance, port pre-flight, driver/app
    creation, uvicorn boot, and the bounded dashboard probe. It
    reports named steps through ``progress`` and returns an outcome
    — it never touches the splash directly, never raises SystemExit,
    and never blocks on any UI. (B2 owns enrolment/health, B3 the v2
    protocol, B4 the sync engine — this function deliberately does
    none of that; MT5 auto-connect is only kicked off, as before.)
    """
    try:
        progress.advance("Validating settings")
        # --host is kept, with guardrails: a non-loopback bind requires
        # explicit authorization (allow_non_loopback) AND the mandatory
        # X-API-Key. Without the flag, refuse to start — never bind
        # 0.0.0.0 by accident.
        try:
            validate_bind_host(settings.host, allow_non_loopback=settings.allow_non_loopback)
        except ValueError as exc:
            return _BootOutcome.failed(f"refusing to start: {exc}", 2)
        from .config import validate_network_proxy

        try:
            validate_network_proxy(settings.network_proxy)
        except ValueError as exc:
            return _BootOutcome.failed(f"refusing to start: {exc}", 2)

        progress.advance("Preparing credentials")
        # Mandatory local API key (auto-generated on first run, stored in
        # the secret store). Fail closed before serving anything.
        ensure_local_api_key(settings)
        register_secret(settings.server_token)
        register_secret(settings.local_api_key)

        base_url = f"http://{settings.host}:{settings.port}"
        # A fresh (unenrolled) bridge lands on the Connection screen
        # (/ui#platform) — setup, not a wall of dashes on the Overview.
        configured = bool(settings.server_token)
        dash_url = dashboard_url(base_url, configured)

        progress.advance("Checking single instance")
        # Single instance, decided BEFORE any MT5 attach or port bind:
        # two processes would fight over the serialized MT5 IPC worker and
        # replace each other's Platform sessions. A second launch now
        # BRINGS THE RUNNING UI TO FRONT (opens its dashboard URL) and
        # exits 0 — the old fast SystemExit(2) with an stderr-only message
        # is what made every relaunch look like "nothing happens".
        from .single_instance import acquire_single_instance

        # Keep the handle on the outcome for the process lifetime: the
        # OS releases the mutex when the owning process exits — and
        # dropping the handle here would release it while we serve.
        instance_lock, acquired = acquire_single_instance()
        if not acquired:
            return _BootOutcome(
                kind="already_running",
                base_url=base_url,
                dash_url=dash_url,
                configured=configured,
                instance_lock=instance_lock,
            )

        progress.advance("Checking port")
        port_error = _check_port_free(settings.host, settings.port)
        if port_error is not None:
            return _BootOutcome.failed(port_error, 2, "NSE MT5 Bridge — port unavailable")

        progress.advance("Preparing bridge")
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

        progress.advance("Starting server")
        config = uvicorn.Config(app, host=settings.host, port=settings.port, log_config=None, access_log=False)
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
        server_thread.start()

        # Boot deadline: a COLD frozen start (PyInstaller onefile extraction
        # + first-launch Defender scan of a fresh unsigned exe) measured
        # >10 s on hosted Windows runners — the old 10 s deadline declared
        # boot failure and exited the process while uvicorn was still
        # starting, which read as "serve never comes up" in CI. 90 s
        # covers extraction + AV + import + bind; genuine bind failures
        # (port taken) are caught earlier by the pre-flight check, and a
        # missed deadline now fails VISIBLY (dialog + exit 1) instead of
        # the old silent `return` (exit 0, process gone, user none the
        # wiser).
        boot_deadline_s = 90 if getattr(sys, "frozen", False) else 15
        deadline = time.time() + boot_deadline_s
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        if not server.started:
            return _BootOutcome.failed(
                f"the bridge server did not start within {boot_deadline_s:.0f} s "
                f"(bind {settings.host}:{settings.port}). Check logs/bridge.log — "
                "another program may be using the port, or the install may be damaged.",
                1,
                "NSE MT5 Bridge — server failed to start",
            )

        progress.advance("Waiting for dashboard")
        # "Started" only means the socket bound — prove the app answers
        # before opening a browser on a dead page. A miss is DEGRADED,
        # not fatal: the server is up and MT5 auto-connect proceeds,
        # the user gets a clear message + the URL, and the browser
        # can load the dashboard when it answers. Killing the process
        # here was the v1.3.x "please wait" startup crash.
        warning = ""
        if not wait_for_http_ready(f"{base_url}/health", timeout_s=_DASHBOARD_PROBE_TIMEOUT_S):
            warning = (
                "the bridge server started but did not answer /health within "
                f"{_DASHBOARD_PROBE_TIMEOUT_S:.0f} s — the dashboard may load slowly. "
                "Check logs/bridge.log if it stays down."
            )
            logger.warning("[BOOT] {}", warning)

        # MT5 auto-connect starts AFTER the API is up (the old 0.5 s sleep
        # raced uvicorn boot): status endpoints are answerable from the
        # first millisecond of connect activity.
        if not args.no_connect:
            threading.Thread(target=_auto_connect, args=(driver,), daemon=True, name="mt5-boot").start()

        progress.complete()
        return _BootOutcome(
            kind="ready",
            server=server,
            server_thread=server_thread,
            driver=driver,
            ctx=ctx,
            device_id=device_id,
            dash_url=dash_url,
            base_url=base_url,
            configured=configured,
            instance_lock=instance_lock,
            warning=warning,
        )
    except SystemExit as exc:
        # A _fail_visible-style abort raised on this thread would only
        # kill the thread: convert it to an outcome for the main thread.
        return _BootOutcome.failed(f"startup aborted (exit {exc.code})", 1)
    except Exception as exc:  # noqa: BLE001 — any boot crash is a visible failure, never a silent thread death
        logger.exception("[BOOT] startup failed: {}", exc)
        return _BootOutcome.failed(f"startup failed: {exc} — check logs/bridge.log.", 1)


def _run(args: argparse.Namespace, settings, gui_visible: bool) -> None:  # type: ignore[no-untyped-def]
    """Own the UI thread: boot on a background thread, serve on this one.

    The main thread never does slow work here — it starts :func:`_boot`
    on a daemon thread, paints that thread's named steps onto the
    splash, and enforces the overall bounded wait. Once the outcome
    lands it either brings the running instance to front, fails
    visibly, or serves (tray + native window / browser) exactly as
    v1.3.x did. The server thread outlives this function only via
    the outcome; shutdown stays in the finally block below.
    """
    progress = StartupProgress(STARTUP_STEPS)
    outcome_box: dict[str, _BootOutcome] = {}

    def _boot_thread_main() -> None:
        outcome_box["outcome"] = _boot(args, settings, progress)

    boot_thread = threading.Thread(target=_boot_thread_main, daemon=True, name="bridge-boot")
    boot_thread.start()

    # Overall bound: the server-boot deadline (90 s frozen / 15 s dev)
    # + the dashboard probe + slack for the unbound steps. If the boot
    # thread is still going past it, the wait itself is the failure —
    # report the last step, never wait forever.
    boot_deadline_s = 90 if getattr(sys, "frozen", False) else 15
    overall_timeout_s = boot_deadline_s + _DASHBOARD_PROBE_TIMEOUT_S + _BOOT_OVERALL_SLACK_S
    overall_deadline = time.monotonic() + overall_timeout_s
    outcome: _BootOutcome | None = None
    last_text: str | None = None
    try:
        while outcome is None:
            text = progress.text()
            if text != last_text:
                update_splash(text)
                set_splash_progress(progress.fraction)
                last_text = text
            outcome = outcome_box.get("outcome")
            if outcome is not None:
                break
            if time.monotonic() >= overall_deadline:
                progress.fail("startup timed out")
                outcome = _BootOutcome.failed(
                    f"startup did not finish within {overall_timeout_s:.0f} s "
                    f"(last step: {progress.current_step or 'starting'}). "
                    "Check logs/bridge.log — the install may be damaged, or another program may be blocking startup.",
                    1,
                )
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        # Ctrl+C during boot: stop waiting; the daemon boot thread
        # dies with the process. The main() finally hides the splash.
        return

    assert outcome is not None
    if outcome.kind == "already_running":
        _bring_existing_to_front(outcome.base_url, configured=outcome.configured, gui_visible=gui_visible)
        return
    if outcome.kind == "failed":
        progress.fail(outcome.message)
        _fail_visible(outcome.message, outcome.title, outcome.exit_code, gui_visible=gui_visible)

    # Ready: the server thread is up (possibly degraded — see below).
    server = outcome.server
    server_thread = outcome.server_thread
    assert server_thread is not None
    driver = outcome.driver
    dash_url = outcome.dash_url
    base_url = outcome.base_url
    device_id = outcome.device_id

    # ASCII-only banner: consoles without a UTF-8 codepage render
    # box-drawing characters as mojibake; plain ASCII is unambiguous
    # everywhere (the console codepage is still set to 65001 on
    # attach, but never rely on it).
    logger.info(
        "======================================================\n"
        "  NSE MT5 Bridge v{} — RUNNING\n"
        "  Dashboard : {}\n"
        "  Swagger   : {}/docs\n"
        "  Device ID : {}\n"
        "======================================================",
        BRIDGE_VERSION,
        dash_url,
        base_url,
        device_id,
    )
    if outcome.warning and gui_visible:
        _dashboard_degraded(dash_url, outcome.warning)

    progress.advance("Opening dashboard")
    update_splash(progress.text())
    set_splash_progress(progress.fraction)

    mode = settings.gui_mode
    quit_event = threading.Event()
    tray = None
    if mode != "none":
        # The tray is the persistent visible presence: without it the
        # bridge is again a Task Manager entry. It starts before the
        # window/browser so there is always a surface, even if the
        # window itself fails.
        def _tray_open() -> None:
            open_dashboard(dash_url)

        def _tray_show_logs() -> None:
            open_folder(str(settings.resolved_log_dir))

        tray = start_tray(
            on_open=_tray_open,
            on_show_logs=_tray_show_logs,
            on_quit=lambda: _request_quit(quit_event),
            tooltip=f"NSE MT5 Bridge — {dash_url}",
        )
    try:
        if mode == "none":
            server_thread.join()
        elif mode == "browser":
            result = open_dashboard(dash_url)
            if not result.ok:
                _dashboard_unreachable(dash_url)
            hide_splash()
            _serve_until_quit(server_thread, quit_event)
        else:
            hide_splash()
            window_result = open_window(dash_url)
            if window_result == WINDOW_BROWSER:
                # Native window unavailable; the browser is the UI —
                # keep serving until killed or tray Quit.
                _serve_until_quit(server_thread, quit_event)
            elif window_result in (WINDOW_FAILED, WINDOW_TIMEOUT):
                detail = None
                if window_result == WINDOW_TIMEOUT:
                    detail = (
                        "The native window stopped responding while opening and was closed — "
                        "the bridge keeps running below."
                    )
                _dashboard_unreachable(dash_url, detail=detail)
                _serve_until_quit(server_thread, quit_event)
            # "webview": open_window returned when the user closed the
            # window → shut down.
    except KeyboardInterrupt:
        pass
    finally:
        hide_splash()
        if tray is not None:
            tray.stop()
        logger.info("[BOOT] shutting down")
        server.should_exit = True
        server_thread.join(timeout=5)
        driver.disconnect()


def _request_quit(quit_event: threading.Event) -> None:
    """Tray → Quit: stop serving and close any native window. The
    server shutdown itself happens in ``_run``'s finally block."""
    quit_event.set()
    destroy_native_window()


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
