"""MT5 driver: a dedicated worker thread serializing every MetaTrader5 call.

The official MetaTrader5 C++ extension owns ONE process-global IPC channel
and is not thread-safe; every broker call in this process funnels through a
single worker thread (queue of jobs -> future). This preserves the exact
semantics of the production Python adapter while letting FastAPI/asyncio
serve concurrent requests.

Connection lifecycle ports the production fixes:
- BUG-310: shutdown() is ALWAYS attempted on disconnect (even after a failed
  connect) — the terminal otherwise keeps rejecting re-attaches with
  (-6, 'Terminal: Authorization failed').
- BUG-130: bounded connect retries with linear backoff + structured telemetry.
- Account-mismatch fail-safe: when a login is configured and the terminal is
  logged into another account, connect fails closed (no live trading on the
  wrong account).
"""

from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from ..config import BridgeSettings


def auto_discover_terminal_path() -> str | None:
    env_path = os.environ.get("NSE_MT5_TERMINAL_PATH") or os.environ.get("MT5_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    candidates = (
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\MetaTrader 5\terminal.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal.exe",
    )
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    try:  # registry probe (App Paths)
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\terminal64.exe"
                ) as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if val and Path(str(val)).is_file():
                        return str(val)
            except OSError:
                continue
    except Exception:
        pass
    return None


CONNECTION_STATES = (
    "DISCONNECTED", "CONNECTING", "CONNECTED", "TERMINAL_ERROR", "AUTH_ERROR",
)


@dataclass
class CallDiagnostic:
    ts: str
    op: str
    ok: bool
    duration_ms: float
    mt5_error_code: Any = None
    mt5_error_message: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts, "op": self.op, "ok": self.ok,
            "duration_ms": round(self.duration_ms, 3),
            "mt5_error_code": self.mt5_error_code,
            "mt5_error_message": self.mt5_error_message,
            "context": self.context,
        }


@dataclass
class OpMetrics:
    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_ms: float = 0.0
    last_error: str | None = None

    def record(self, ok: bool, ms: float, error: str | None = None) -> None:
        self.calls += 1
        self.last_ms = ms
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)
        if not ok:
            self.failures += 1
            self.last_error = error

    def to_dict(self) -> dict[str, Any]:
        avg = self.total_ms / self.calls if self.calls else 0.0
        return {
            "calls": self.calls,
            "failures": self.failures,
            "avg_ms": round(avg, 3),
            "max_ms": round(self.max_ms, 3),
            "last_ms": round(self.last_ms, 3),
            "last_error": self.last_error,
        }


class Mt5Driver:
    """Serialized, thread-safe access to the MetaTrader5 C++ IPC driver."""

    def __init__(self, settings: BridgeSettings) -> None:
        self.settings = settings
        self._jobs: queue.Queue[tuple[Callable[[Any], Any], Future]] = queue.Queue()
        self._thread = threading.Thread(name="mt5-worker", target=self._work, daemon=True)
        self._mt5: Any = None
        self._connected = False
        self._state = "DISCONNECTED"
        self._state_detail = ""
        self._state_since = datetime.now(UTC)
        self._last_error: dict[str, Any] | None = None
        self._pkg_version: str | None = None
        self._terminal_version: str | None = None
        self._login: int | None = None
        self._metrics: dict[str, OpMetrics] = {}
        self._ring: list[CallDiagnostic] = []
        self._ring_max = 50
        self._lock = threading.Lock()
        self._thread.start()

    # ------------------------------------------------------------------
    # worker plumbing
    # ------------------------------------------------------------------
    def _work(self) -> None:  # pragma: no cover — infinite worker loop
        while True:
            job, fut = self._jobs.get()
            try:
                fut.set_result(job(self._module()))
            except BaseException as exc:  # noqa: BLE001 — isolate the worker
                fut.set_exception(exc)

    def _module(self) -> Any:
        if self._mt5 is None:
            import MetaTrader5 as mt5  # type: ignore

            self._mt5 = mt5
        return self._mt5

    def run(
        self,
        job: Callable[[Any], Any],
        op: str | None = None,
        context: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        """Execute job(mt5) on the worker thread; record diagnostics + metrics."""
        name = op or getattr(job, "__name__", "unnamed_job")
        fut: Future = Future()
        self._jobs.put((job, fut))
        t0 = time.perf_counter()
        try:
            result = fut.result(timeout=timeout)
        except Exception as exc:
            self._record(name, False, (time.perf_counter() - t0) * 1000.0,
                         context=context, error=str(exc))
            raise
        duration_ms = (time.perf_counter() - t0) * 1000.0
        self._record(name, True, duration_ms, context=context)
        return result

    def _record(
        self,
        op: str,
        ok: bool,
        duration_ms: float,
        *,
        context: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        m = self._metrics.setdefault(op, OpMetrics())
        m.record(ok, duration_ms, error)
        with self._lock:
            self._ring.append(
                CallDiagnostic(
                    ts=datetime.now(UTC).isoformat(),
                    op=op,
                    ok=ok,
                    duration_ms=duration_ms,
                    mt5_error_code=self._last_error_code(),
                    mt5_error_message=self._last_error_message(),
                    context=context or {},
                )
            )
            if len(self._ring) > self._ring_max:
                del self._ring[:-self._ring_max]
        if not ok:
            logger.warning(
                "[MT5_CALL] op={} ok=False duration_ms={:.1f} error={} context={}",
                op, duration_ms, error, context or {},
            )

    def _last_error_code(self) -> Any:
        try:
            err = self._mt5.last_error() if self._mt5 else None
            return err[0] if err else None
        except Exception:
            return None

    def _last_error_message(self) -> str | None:
        try:
            err = self._mt5.last_error() if self._mt5 else None
            return str(err[1]) if err else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # connection state
    # ------------------------------------------------------------------
    def _set_state(self, state: str, detail: str = "") -> None:
        self._state = state
        self._state_detail = detail
        self._state_since = datetime.now(UTC)

    @property
    def connected(self) -> bool:
        return self._connected

    def ensure_connected(self) -> None:
        if not self._connected:
            from ..errors import BridgeError, ErrorCode

            raise BridgeError(
                ErrorCode.NOT_CONNECTED,
                f"MT5 adapter is not connected (state={self._state}"
                + (f": {self._state_detail}" if self._state_detail else "") + ")",
            )

    def connection_state_dict(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "detail": self._state_detail,
            "since": self._state_since.isoformat(),
            "connected": self._connected,
            "pkg_version": self._pkg_version,
            "terminal_version": self._terminal_version,
            "login": self._login,
            "last_error": self._last_error,
        }

    def metrics_dict(self) -> dict[str, Any]:
        return {k: v.to_dict() for k, v in sorted(self._metrics.items())}

    def ring_dict(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [d.to_dict() for d in self._ring[-limit:]]

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------
    def connect(self) -> dict[str, Any]:
        """Resilient connect (runs on the worker thread). Returns a summary."""
        s = self.settings
        summary = self.run(self._connect_job, op="initialize", timeout=max(60.0, s.mt5_timeout_ms / 1000.0 * s.mt5_retries + 30))
        return summary

    def _connect_job(self, mt5: Any) -> dict[str, Any]:
        s = self.settings
        self._set_state("CONNECTING", "initialize() attempt 1")
        self._last_error = None
        mt5.shutdown()  # BUG-310: release any stale attach before re-initializing

        last_err: Any = None
        connected = False
        for attempt in range(1, max(1, s.mt5_retries) + 1):
            kwargs: dict[str, Any] = {"timeout": s.mt5_timeout_ms}
            path = s.mt5_terminal_path or auto_discover_terminal_path()
            if path:
                kwargs["path"] = path
            if mt5.initialize(**kwargs):
                connected = True
                break
            last_err = mt5.last_error()
            if attempt < s.mt5_retries:
                backoff_ms = 250 * attempt
                logger.warning(
                    "[MT5_CONNECT] event=RETRY attempt={}/{} retcode={} backoff_ms={}",
                    attempt, s.mt5_retries, last_err, backoff_ms,
                )
                self._set_state("CONNECTING", f"retry {attempt}/{s.mt5_retries} (retcode {last_err})")
                time.sleep(backoff_ms / 1000.0)

        if not connected:
            self._connected = False
            self._last_error = {"code": last_err[0], "message": last_err[1]}
            self._set_state("TERMINAL_ERROR", f"initialize failed: {last_err}")
            return {"ok": False, "error": f"initialize failed after {s.mt5_retries} attempts", "last_error": last_err}

        # version + terminal stamps (diagnostics only, never fatal)
        pkg_ver = term_ver = None
        try:
            vers = mt5.version()
            if vers:
                pkg_ver = f"{vers[0]}.{vers[1]}.{vers[2]}" if len(vers) >= 3 else str(vers[0])
        except Exception:
            pass
        try:
            term_info = mt5.terminal_info()
            term_ver = str(getattr(term_info, "name", "") or "") or None
        except Exception:
            term_info = None
        try:  # login: terminal_info may not carry it on newer builds — use account
            acc = mt5.account_info()
            if acc is not None and getattr(acc, "login", None):
                self._login = int(acc.login)
        except Exception:
            pass
        self._pkg_version, self._terminal_version = pkg_ver, term_ver

        # optional explicit login
        if s.mt5_login and s.mt5_password and s.mt5_server:
            if not mt5.login(login=s.mt5_login, password=s.mt5_password, server=s.mt5_server):
                err = mt5.last_error()
                mt5.shutdown()
                self._connected = False
                self._last_error = {"code": err[0], "message": err[1]}
                self._set_state("AUTH_ERROR", f"login failed: {err}")
                return {"ok": False, "error": f"login failed: {err}"}

        # account mismatch fail-safe
        if s.mt5_login:
            acc = mt5.account_info()
            actual = getattr(acc, "login", None) if acc else None
            if actual is not None and int(actual) != int(s.mt5_login):
                mt5.shutdown()
                self._connected = False
                detail = f"account mismatch: expected {s.mt5_login}, got {actual}"
                self._last_error = {"code": None, "message": detail}
                self._set_state("AUTH_ERROR", detail)
                logger.critical("MT5 ACCOUNT MISMATCH: {}", detail)
                return {"ok": False, "error": detail}

        self._connected = True
        self._set_state("CONNECTED", "connected")
        self._detect_broker_offset(mt5)
        logger.info(
            "Successfully connected to MT5 terminal process (pkg={} terminal={}).",
            pkg_ver, term_ver,
        )
        return {
            "ok": True,
            "pkg_version": pkg_ver,
            "terminal_version": term_ver,
            "login": self._login,
            "terminal_name": term_ver,
            "terminal_connected": bool(getattr(term_info, "connected", False)) if term_info else None,
        }

    def _detect_broker_offset(self, mt5: Any) -> None:
        """Auto-detect the broker server UTC offset from live tick epochs.

        MT5 epochs (ticks/deals/history boundaries) are SERVER-LOCAL seconds;
        the Python package treats datetime args as UTC (BUG-188 class). The
        offset is measured as (freshest tick epoch - local UTC epoch) and
        applied symmetrically at every history/display boundary. The config
        value remains the fallback when no fresh tick is available (market
        closed on all probe symbols).
        """
        try:
            freshest = 0.0
            for sym in (self.settings.bot_symbol, "EURUSD", "GBPUSD", "USDJPY"):
                raw = mt5.symbol_info_tick(sym)
                t = float(getattr(raw, "time", 0) or 0) if raw is not None else 0.0
                if t > freshest:
                    freshest = t
            if freshest <= 0:
                logger.info("[MT5] broker offset detection skipped — no ticks available")
                return
            offset = round((freshest - time.time()) / 60)
            if not (-1440 <= offset <= 1440):
                logger.warning("[MT5] broker offset {} min out of range — ignored", offset)
                return
            if offset != self.settings.broker_utc_offset_minutes:
                logger.info(
                    "[MT5] broker UTC offset detected: {} minutes (config had {})",
                    offset, self.settings.broker_utc_offset_minutes,
                )
                self.settings.broker_utc_offset_minutes = offset
        except Exception as exc:
            logger.warning("[MT5] broker offset detection failed: {}", exc)

    def disconnect(self) -> dict[str, Any]:
        def job(mt5: Any) -> None:
            try:
                mt5.shutdown()
                logger.info("MetaTrader 5 IPC connection closed.")
            except Exception:
                logger.warning("[MT5_DISCONNECT] shutdown() raised; handle already released")

        try:
            self.run(job, op="shutdown")
        except Exception:
            pass
        self._connected = False
        self._set_state("DISCONNECTED", "disconnected")
        return {"ok": True, "state": self._state}

    def refresh_terminal(self) -> dict[str, Any] | None:
        """Live liveness probe: terminal_info().connected updates the state."""
        if not self._connected:
            return None

        def job(mt5: Any) -> dict[str, Any] | None:
            raw = mt5.terminal_info()
            if raw is None:
                return None
            return {
                "connected": bool(getattr(raw, "connected", False)),
                "name": getattr(raw, "name", None),
                "trade_allowed": bool(getattr(raw, "trade_allowed", False)),
                "login": getattr(raw, "login", None),
            }

        try:
            info = self.run(job, op="terminal_info")
        except Exception:
            return None
        if info and not info["connected"]:
            self._set_state("TERMINAL_ERROR", "terminal reports disconnected")
        return info
