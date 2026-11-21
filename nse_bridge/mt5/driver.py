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
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Bound on the serialized worker's pending jobs. Far above any
#: legitimate burst (all pollers, the GUI, and the server-command path
#: are themselves bounded); a full queue means the worker is stalled —
#: callers must fail fast instead of queuing unbounded work.
MT5_JOB_QUEUE_MAX = 256
from typing import Any

from loguru import logger

from ..config import BridgeSettings


def _env_terminal_path() -> str | None:
    env_path = os.environ.get("NSE_MT5_TERMINAL_PATH") or os.environ.get("MT5_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    return None


def _standard_terminal_paths() -> list[str]:
    return [
        r"C:\Program Files\MetaTrader 5\terminal64.exe",
        r"C:\Program Files\MetaTrader 5\terminal.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal64.exe",
        r"C:\Program Files (x86)\MetaTrader 5\terminal.exe",
    ]


def auto_discover_terminal_path() -> str | None:
    """First terminal found (env -> standard -> App Paths).

    Kept for the connect path, which needs a single terminal. The
    full multi-terminal enumeration is :func:`discover_terminals`.
    """
    env_path = _env_terminal_path()
    if env_path:
        return env_path
    for cand in _standard_terminal_paths():
        if Path(cand).is_file():
            return cand
    for cand in _app_paths_candidates():
        return cand
    return None


#: Executable names that identify an MT5 terminal.
TERMINAL_EXE_NAMES = ("terminal64.exe", "terminal.exe")


def _norm_path(path: str) -> str:
    """Case-insensitive, separator-normalised path key for dedup."""
    return os.path.normcase(os.path.normpath(str(path)))


def _app_paths_candidates(winreg_module: Any = None) -> list[str]:
    """Terminal paths from the Windows App Paths registry.

    ``winreg_module`` injects a fake registry (unit tests). With None
    the real ``winreg`` is imported lazily — on Linux the import fails
    and this returns [] (no registry probe, no crash, nothing at
    module import time).
    """
    if winreg_module is None:
        try:
            import winreg as _winreg
        except ImportError:
            return []
        winreg_module = _winreg
    found: list[str] = []
    for exe in TERMINAL_EXE_NAMES:
        subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
        for root in (winreg_module.HKEY_LOCAL_MACHINE, winreg_module.HKEY_CURRENT_USER):
            try:
                with winreg_module.OpenKey(root, subkey) as key:
                    val, _ = winreg_module.QueryValueEx(key, "")
                    if val and Path(str(val)).is_file():
                        found.append(str(val))
            except (OSError, AttributeError):
                continue
    return found


def _custom_dir_candidates(dirs: list[str]) -> list[tuple[str, str]]:
    """Terminal exes inside user-supplied directories.

    A directory containing ``terminal64.exe``/``terminal.exe`` counts
    as a candidate (``custom`` when it is the directory itself,
    ``portable`` when it is an immediate subdirectory — the broker
    portable-install layout). Missing/unreadable directories are
    skipped silently; discovery never fails on a bad custom dir.
    """
    out: list[tuple[str, str]] = []
    for d in dirs:
        base = Path(d)
        for exe in TERMINAL_EXE_NAMES:
            candidate = base / exe
            if candidate.is_file():
                out.append((str(candidate), "custom"))
        try:
            subdirs = [s for s in base.iterdir() if s.is_dir()]
        except OSError:
            continue
        for sub in subdirs:
            for exe in TERMINAL_EXE_NAMES:
                candidate = sub / exe
                if candidate.is_file():
                    out.append((str(candidate), "portable"))
    return out


def _running_terminal_paths(process_list: list[str] | None = None) -> list[str]:
    """Exe paths of currently running MT5 terminals.

    ``process_list`` injects a fake list (unit tests). With None the
    live enumeration runs on Windows only (ctypes Toolhelp32Snapshot
    + QueryFullProcessImageNameW, imported lazily — never at module
    import time). On Linux this is a clearly-marked STUB that returns
    no processes: the bridge never runs MT5 on Linux and Windows
    process enumeration has no Linux equivalent. Enumeration failures
    never break discovery — they degrade to an empty list.
    """
    if process_list is not None:
        return [str(p) for p in process_list]
    if sys.platform != "win32":
        return []  # LINUX STUB: no Windows process enumeration here.
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        def image_path(pid: int) -> str | None:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return buf.value
                return None
            finally:
                kernel32.CloseHandle(handle)

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            return []
        paths: list[str] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() in TERMINAL_EXE_NAMES:
                    path = image_path(entry.th32ProcessID)
                    if path:
                        paths.append(path)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return paths
    except Exception:  # noqa: BLE001 — process enumeration must never break discovery
        return []


def _file_version_windows(path: str) -> str | None:
    """FileVersion via version.dll (Windows only)."""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)  # type: ignore[attr-defined] # Windows-only API
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = wintypes.LPVOID()
        length = wintypes.UINT()
        if not version.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        fixed = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))
        if fixed[0] != 0xFEEF04BD:  # VS_FIXEDFILEINFO signature
            return None
        ms, ls = fixed[2], fixed[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001
        return None


def _pe_file_version(path: str) -> str | None:
    """Best-effort FileVersion from a PE file's version resource.

    Pure-Python parser (works on Linux against a real terminal64.exe):
    DOS header -> PE header -> section table -> resource directory
    (RT_VERSION=16) -> VS_VERSION_INFO -> VS_FIXEDFILEINFO. Any parse
    failure returns None — a missing version is reported, never guessed.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    try:
        if len(data) < 0x40 or data[:2] != b"MZ":
            return None
        e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
        if data[e_lfanew : e_lfanew + 4] != b"PE\0\0":
            return None
        coff = e_lfanew + 4
        num_sections = int.from_bytes(data[coff + 2 : coff + 4], "little")
        opt_size = int.from_bytes(data[coff + 16 : coff + 18], "little")
        opt = coff + 20
        magic = int.from_bytes(data[opt : opt + 2], "little")
        if magic == 0x10B:  # PE32
            dd_base = opt + 96
        elif magic == 0x20B:  # PE32+
            dd_base = opt + 112
        else:
            return None
        rsrc_rva = int.from_bytes(data[dd_base + 16 : dd_base + 20], "little")
        rsrc_size = int.from_bytes(data[dd_base + 20 : dd_base + 24], "little")
        if not rsrc_rva or not rsrc_size:
            return None
        sections: list[tuple[int, int, int]] = []
        sec_base = opt + opt_size
        for i in range(num_sections):
            off = sec_base + i * 40
            va = int.from_bytes(data[off + 12 : off + 16], "little")
            raw_size = int.from_bytes(data[off + 16 : off + 20], "little")
            raw_ptr = int.from_bytes(data[off + 20 : off + 24], "little")
            sections.append((va, raw_size, raw_ptr))

        def rva_to_off(rva: int) -> int | None:
            for va, raw_size, raw_ptr in sections:
                if va <= rva < va + max(raw_size, 1):
                    return raw_ptr + (rva - va)
            return None

        def read_dir(off: int) -> list[tuple[int, int, bool]]:
            if off + 16 > len(data):
                return []
            named = int.from_bytes(data[off + 12 : off + 14], "little")
            ids = int.from_bytes(data[off + 14 : off + 16], "little")
            entries = []
            for i in range(named + ids):
                eoff = off + 16 + i * 8
                if eoff + 8 > len(data):
                    break
                name = int.from_bytes(data[eoff : eoff + 4], "little")
                target = int.from_bytes(data[eoff + 4 : eoff + 8], "little")
                entries.append((name, target & 0x7FFFFFFF, bool(target & 0x80000000)))
            return entries

        def resolve_subdir(dir_off: int, want_id: int) -> int | None:
            for name, target, is_dir in read_dir(dir_off):
                if is_dir and not (name & 0x80000000) and (name & 0xFFFF) == want_id:
                    return rva_to_off(target)
            return None

        def resolve_data(dir_off: int) -> int | None:
            # Language leaf: any language accepted; the entry points at
            # an IMAGE_RESOURCE_DATA_ENTRY (data RVA + size).
            for _name, target, is_dir in read_dir(dir_off):
                if is_dir:
                    continue
                entry_off = rva_to_off(target)
                if entry_off is None or entry_off + 8 > len(data):
                    continue
                data_rva = int.from_bytes(data[entry_off : entry_off + 4], "little")
                return rva_to_off(data_rva)
            return None

        root_off = rva_to_off(rsrc_rva)
        if root_off is None:
            return None
        type_off = resolve_subdir(root_off, 16)  # RT_VERSION
        if type_off is None:
            return None
        name_off = resolve_subdir(type_off, 1)  # VS_VERSION_INFO id
        if name_off is None:
            return None
        blob_off = resolve_data(name_off)
        if blob_off is None or blob_off + 58 > len(data):
            return None
        key = data[blob_off + 6 : blob_off + 38].decode("utf-16-le", errors="ignore")
        if "VS_VERSION_INFO" not in key:
            return None
        # VS_FIXEDFILEINFO follows the key, DWORD-aligned relative to
        # the start of the VS_VERSION_INFO structure.
        val_off = blob_off + 6 + 32
        val_off += (4 - ((val_off - blob_off) % 4)) % 4
        fixed = data[val_off : val_off + 52]
        if len(fixed) < 52:
            return None
        if int.from_bytes(fixed[0:4], "little") != 0xFEEF04BD:
            return None
        ms = int.from_bytes(fixed[8:12], "little")
        ls = int.from_bytes(fixed[12:16], "little")
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (IndexError, ValueError, OverflowError):
        return None


def _terminal_version(path: str) -> str | None:
    """FileVersion of a terminal exe, or None when undetectable.

    Windows: version.dll first, PE-resource parser as fallback.
    Linux: the pure-Python PE parser (works on a real terminal64.exe).
    """
    if sys.platform == "win32":
        ver = _file_version_windows(path)
        if ver:
            return ver
    return _pe_file_version(path)


@dataclass
class TerminalInfo:
    """One discovered MT5 terminal candidate."""

    path: str
    version: str | None = None
    running: bool = False
    source: str = "discovered"  # configured | env | standard | registry | custom | portable | running
    exists: bool = True
    # Spec §3: terminal data directory, when determinable. None means
    # "not determinable from this machine" — never a guessed path.
    data_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "running": self.running,
            "source": self.source,
            "exists": self.exists,
            "data_dir": self.data_dir,
        }


#: Markers of a live MT5 data directory. A portable install keeps its
#: data next to the executable; the install directory itself never
#: carries a ``Logs`` folder, so the MQL5+Logs conjunction is the
#: portable marker. A never-run portable install has no data dir yet
#: and honestly reports None.
_DATA_DIR_MARKERS = ("MQL5", "Logs")


def _portable_data_dir(exe_path: str) -> str | None:
    """Data dir for a portable install: the exe directory itself, but
    only when it carries live data-directory markers. Never guessed:
    a bare exe directory reports None."""
    exe_dir = Path(exe_path).parent
    if all((exe_dir / marker).is_dir() for marker in _DATA_DIR_MARKERS):
        return str(exe_dir)
    return None


def _appdata_terminal_data_dirs(appdata_root: str | None) -> list[str]:
    """Existing MT5 data dirs under ``%APPDATA%\\MetaQuotes\\Terminal``.

    ``appdata_root`` is the ``...\\MetaQuotes\\Terminal`` directory
    itself (injectable for unit tests). With None the Windows %APPDATA%
    location is used; on Linux there is no %APPDATA% and this returns
    [] instead of crashing. A subdirectory only counts when it exists
    and carries an ``MQL5`` folder — a bare hash-named folder is not
    a data directory.
    """
    if appdata_root is None:
        if sys.platform != "win32":
            return []
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return []
        appdata_root = str(Path(appdata) / "MetaQuotes" / "Terminal")
    root = Path(appdata_root)
    try:
        subdirs = [s for s in root.iterdir() if s.is_dir()]
    except OSError:
        return []
    return [str(s) for s in subdirs if (s / "MQL5").is_dir()]


def discover_terminals(
    settings: BridgeSettings | None = None,
    *,
    extra_dirs: list[str] | None = None,
    running_paths: list[str] | None = None,
    winreg_module: Any = None,
    version_fn: Callable[[str], str | None] | None = None,
    appdata_root: str | None = None,
    standard_paths: list[str] | None = None,
) -> list[TerminalInfo]:
    """Enumerate ALL MT5 terminal candidates (Phase 5).

    Sources, in order: configured path (``settings.mt5_terminal_path``),
    ``NSE_MT5_TERMINAL_PATH``/``MT5_PATH`` env, standard install
    locations, App Paths registry (Windows; guarded — no winreg import
    at module load, returns nothing on Linux), custom directories
    (``settings.mt5_custom_terminal_dirs`` + ``extra_dirs``; a
    directory containing terminal64.exe counts, including immediate
    subdirectories for portable installs), running terminal processes
    (Windows process list; Linux stub returns none).

    Each candidate also carries ``data_dir`` (spec §3) when
    determinable, else None — never a guessed path. Portable installs
    resolve to the exe directory when it carries live data markers
    (``MQL5`` + ``Logs``). Standard installs live under
    ``%APPDATA%\\MetaQuotes\\Terminal\\<hash>``; the hash is derived
    from the install path by the terminal and cannot be recovered from
    the bridge, so an AppData dir is attributed ONLY in the unambiguous
    case — exactly one discovered terminal AND exactly one existing
    data dir on the machine. Anything else reports None rather than
    guessing which dir belongs to which terminal. ``appdata_root``
    injects the ``...\\MetaQuotes\\Terminal`` directory (unit tests);
    with None the Windows %APPDATA% location is used, and on Linux
    the AppData lookup degrades to no candidates instead of crashing.

    Discovery only READS — it never writes or overwrites the user's
    terminal configuration; selection stays user-driven (PUT /settings
    ``mt5_terminal_path``). ``running_paths`` / ``winreg_module`` /
    ``version_fn`` inject fakes for unit tests. Safe on Linux: no
    Windows registry/process API is touched at import time or when the
    platform is not Windows.
    """
    version_fn = version_fn or _terminal_version
    found: dict[str, TerminalInfo] = {}

    def add(path: str, source: str) -> None:
        if not path:
            return
        key = _norm_path(path)
        if key in found:
            return
        exists = Path(path).is_file()
        found[key] = TerminalInfo(
            path=str(path),
            version=version_fn(str(path)) if exists else None,
            running=False,
            source=source,
            exists=exists,
            data_dir=_portable_data_dir(str(path)) if exists else None,
        )

    if settings is not None and (settings.mt5_terminal_path or "").strip():
        add(settings.mt5_terminal_path.strip(), "configured")
    env_path = _env_terminal_path()
    if env_path:
        add(env_path, "env")
    for cand in standard_paths if standard_paths is not None else _standard_terminal_paths():
        if Path(cand).is_file():
            add(cand, "standard")
    for cand in _app_paths_candidates(winreg_module):
        add(cand, "registry")
    dirs: list[str] = []
    if settings is not None:
        dirs += [d.strip() for d in (settings.mt5_custom_terminal_dirs or "").split(",") if d.strip()]
    if extra_dirs:
        dirs += list(extra_dirs)
    for cand, source in _custom_dir_candidates(dirs):
        add(cand, source)
    running = _running_terminal_paths(running_paths)
    running_norm = {_norm_path(p): p for p in running}
    for key, info in found.items():
        if key in running_norm:
            info.running = True
    for norm, original in running_norm.items():
        if norm not in found:
            # A running terminal no other source found — still a candidate.
            exists = Path(original).is_file()
            found[norm] = TerminalInfo(
                path=str(original),
                version=version_fn(str(original)) if exists else None,
                running=True,
                source="running",
                exists=exists,
                data_dir=_portable_data_dir(str(original)) if exists else None,
            )
    # Standard-install data dirs (%APPDATA%\MetaQuotes\Terminal\<hash>)
    # are attributed only in the unambiguous case (exactly one terminal
    # AND exactly one existing data dir) — see the docstring. A
    # portable data dir already resolved above is never overwritten.
    appdata_dirs = _appdata_terminal_data_dirs(appdata_root)
    if len(found) == 1 and len(appdata_dirs) == 1:
        sole = next(iter(found.values()))
        if sole.data_dir is None:
            sole.data_dir = appdata_dirs[0]
    return list(found.values())


CONNECTION_STATES = (
    "DISCONNECTED",
    "CONNECTING",
    "CONNECTED",
    "TERMINAL_ERROR",
    "AUTH_ERROR",
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
            "ts": self.ts,
            "op": self.op,
            "ok": self.ok,
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
        # Bounded job queue (see MT5_JOB_QUEUE_MAX): a stalled worker
        # produces fast, clear failures instead of unbounded queued
        # work while every caller waits out its own future timeout.
        self._jobs: queue.Queue[tuple[Callable[[Any], Any], Future]] = queue.Queue(maxsize=MT5_JOB_QUEUE_MAX)
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
        self._diagnostic_code: str | None = None  # e.g. MT5_ACCOUNT_NOT_READY
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
        name: str = op or str(getattr(job, "__name__", "unnamed_job"))
        fut: Future = Future()
        try:
            self._jobs.put_nowait((job, fut))
        except queue.Full as exc:
            # Backpressure, not silent waiting: the worker is stalled
            # or hopelessly behind — fail in milliseconds rather than
            # queue behind 60 s timeouts.
            error = f"MT5 job queue full ({MT5_JOB_QUEUE_MAX}) — the serialized worker is stalled or overloaded"
            self._record(name, False, 0.0, context=context, error=error)
            from ..errors import BridgeError, ErrorCode

            raise BridgeError(ErrorCode.MT5_CALL_FAILED, error) from exc
        t0 = time.perf_counter()
        try:
            result = fut.result(timeout=timeout)
        except Exception as exc:
            self._record(name, False, (time.perf_counter() - t0) * 1000.0, context=context, error=str(exc))
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
                del self._ring[: -self._ring_max]
        if not ok:
            logger.warning(
                "[MT5_CALL] op={} ok=False duration_ms={:.1f} error={} context={}",
                op,
                duration_ms,
                error,
                context or {},
            )

    def _last_error_code(self) -> Any:
        try:
            err = self._mt5.last_error() if self._mt5 else None
            return err[0] if err else None
        except Exception:  # noqa: BLE001
            return None

    def _last_error_message(self) -> str | None:
        try:
            err = self._mt5.last_error() if self._mt5 else None
            return str(err[1]) if err else None
        except Exception:  # noqa: BLE001
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
                + (f": {self._state_detail}" if self._state_detail else "")
                + ")",
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
            "diagnostic_code": self._diagnostic_code,
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
        summary = self.run(
            self._connect_job, op="initialize", timeout=max(60.0, s.mt5_timeout_ms / 1000.0 * s.mt5_retries + 30)
        )
        return summary

    def _connect_job(self, mt5: Any) -> dict[str, Any]:
        s = self.settings
        self._set_state("CONNECTING", "initialize() attempt 1")
        self._last_error = None
        self._diagnostic_code = None
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
                    attempt,
                    s.mt5_retries,
                    last_err,
                    backoff_ms,
                )
                self._set_state("CONNECTING", f"retry {attempt}/{s.mt5_retries} (retcode {last_err})")
                time.sleep(backoff_ms / 1000.0)

        if not connected:
            self._connected = False
            # last_error() may return None (A4) — never index it blindly.
            if last_err:
                self._last_error = {
                    "code": last_err[0],
                    "message": last_err[1] if len(last_err) > 1 else "",
                }
            else:
                self._last_error = {"code": None, "message": "initialize failed (no error detail)"}
            self._set_state("TERMINAL_ERROR", f"initialize failed: {last_err}")
            return {"ok": False, "error": f"initialize failed after {s.mt5_retries} attempts", "last_error": last_err}

        # version + terminal stamps (diagnostics only, never fatal)
        pkg_ver = term_ver = None
        try:
            vers = mt5.version()
            if vers:
                pkg_ver = f"{vers[0]}.{vers[1]}.{vers[2]}" if len(vers) >= 3 else str(vers[0])
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            term_info = mt5.terminal_info()
            term_ver = str(getattr(term_info, "name", "") or "") or None
        except Exception:  # noqa: BLE001
            term_info = None
        try:  # login: terminal_info may not carry it on newer builds — use account
            acc = mt5.account_info()
            if acc is not None and getattr(acc, "login", None):
                self._login = int(acc.login)
        except Exception:  # noqa: BLE001, S110
            pass
        self._pkg_version, self._terminal_version = pkg_ver, term_ver

        # No MT5 password is ever requested or stored by the bridge (the
        # user signs in through MT5 itself). A terminal that attached
        # fine but has no logged-in account is a structured diagnostic,
        # not a crash: MT5_ACCOUNT_NOT_READY, surfaced via the
        # diagnostics/status path for the GUI to guide the user.
        if self._login is None:
            detail = (
                "MT5 terminal is running but no account is logged in "
                "(MT5_ACCOUNT_NOT_READY) — sign in through the MT5 terminal "
                "itself; the bridge never asks for your MT5 password."
            )
            self._connected = False
            self._diagnostic_code = "MT5_ACCOUNT_NOT_READY"
            self._last_error = {"code": None, "message": detail}
            self._set_state("AUTH_ERROR", detail)
            return {
                "ok": False,
                "error": detail,
                "diagnostic_code": "MT5_ACCOUNT_NOT_READY",
                "guidance": "Open the MT5 terminal and log in to your trading account, then retry connect.",
            }

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
            pkg_ver,
            term_ver,
        )
        return {
            "ok": True,
            "pkg_version": pkg_ver,
            "terminal_version": term_ver,
            "login": self._login,
            "diagnostic_code": None,
            "terminal_name": term_ver,
            "terminal_connected": bool(getattr(term_info, "connected", False)) if term_info else None,
        }

    def _detect_broker_offset(self, mt5: Any) -> None:
        """Auto-detect the broker server UTC offset from live tick epochs.

        MT5 epochs (ticks/deals/history boundaries) are SERVER-LOCAL seconds;
        the Python package treats datetime args as UTC (BUG-188 class). The
        offset is measured as (freshest tick epoch - local UTC epoch) and
        applied symmetrically at every history/display boundary. The config
        value remains the fallback when no fresh tick is available.

        Probe symbols are NEVER hardcoded (spec §4): the configured
        ``bot_symbol`` (operator-chosen, never a default) is probed first,
        then the first visible symbols the broker itself reports via
        ``symbols_get()``. When the broker reports no symbols, detection
        is skipped — no symbol name is ever invented by the bridge.
        """
        try:
            candidates: list[str] = []
            if self.settings.bot_symbol:
                candidates.append(self.settings.bot_symbol)
            try:
                rows = mt5.symbols_get() or []
            except Exception:  # noqa: BLE001 — broker list failure skips detection
                rows = []
            visible: list[str] = []
            hidden: list[str] = []
            for row in rows:
                name = str(getattr(row, "name", "") or "")
                if not name or name in candidates:
                    continue
                (visible if bool(getattr(row, "visible", False)) else hidden).append(name)
            # Bound the probe: bot_symbol + the first few broker symbols.
            for name in visible + hidden:
                if len(candidates) >= 5:
                    break
                candidates.append(name)
            freshest = 0.0
            for sym in candidates:
                try:
                    raw = mt5.symbol_info_tick(sym)
                except Exception as exc:  # noqa: BLE001 — one bad probe never blocks the rest
                    logger.warning("[MT5] broker offset probe failed for {}: {}", sym, exc)
                    continue
                t = float(getattr(raw, "time", 0) or 0) if raw is not None else 0.0
                freshest = max(freshest, t)
            if freshest <= 0:
                logger.info("[MT5] broker offset detection skipped — no ticks available")
                return
            # Stale-tick guard (A1): a tick older than 5 minutes under the
            # CURRENTLY configured offset is stale evidence (market
            # closed overnight) and must not override the configured
            # offset. A fresh tick always passes this check: its age
            # under the configured offset is (configured - true), which
            # is <= 0 when the config is at or below the true offset.
            # When the config is wrong HIGH, a fresh tick looks stale
            # and detection is skipped — fail-closed, the operator's
            # explicit value wins and can be corrected in config.
            age_s = (time.time() + self.settings.broker_utc_offset_minutes * 60) - freshest
            if age_s > 300:
                logger.info(
                    "[MT5] broker offset detection skipped — freshest tick is %.0fs old "
                    "under the configured offset (market likely closed); keeping configured offset",
                    age_s,
                )
                return
            offset = round((freshest - time.time()) / 60)
            if not (-1440 <= offset <= 1440):
                logger.warning("[MT5] broker offset {} min out of range — ignored", offset)
                return
            if offset != self.settings.broker_utc_offset_minutes:
                logger.info(
                    "[MT5] broker UTC offset detected: {} minutes (config had {})",
                    offset,
                    self.settings.broker_utc_offset_minutes,
                )
                self.settings.broker_utc_offset_minutes = offset
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MT5] broker offset detection failed: {}", exc)

    def disconnect(self) -> dict[str, Any]:
        def job(mt5: Any) -> None:
            try:
                mt5.shutdown()
                logger.info("MetaTrader 5 IPC connection closed.")
            except Exception:  # noqa: BLE001
                logger.warning("[MT5_DISCONNECT] shutdown() raised; handle already released")

        try:
            self.run(job, op="shutdown")
        except Exception:  # noqa: BLE001, S110
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
        except Exception:  # noqa: BLE001
            return None
        if info and not info["connected"]:
            self._set_state("TERMINAL_ERROR", "terminal reports disconnected")
        elif info and info["connected"] and self._state == "TERMINAL_ERROR":
            # Symmetric healing: a single transient probe flip used to
            # degrade the state permanently (nothing restored CONNECTED
            # short of a manual reconnect) — the UI then showed
            # TERMINAL_ERROR while the account was demonstrably
            # connected (observed 2026-11-21, terminal build 6327). A
            # live probe proving the terminal→broker link is up is
            # exactly the recovery evidence the degradation was waiting
            # for; restore and log it.
            self._set_state("CONNECTED", "terminal link recovered (live probe)")
            logger.info("[MT5] terminal→broker link recovered — state restored to CONNECTED")
        return info

    # ------------------------------------------------------------------
    # symbol accessors (spec §4 / gap C1 — minimal surface for
    # nse_bridge.symbols; no IPC or execution behaviour changes here)
    # ------------------------------------------------------------------
    def symbols_get(self) -> list[Any] | None:
        """All symbols the terminal reports (``mt5.symbols_get()``).

        The inventory built from this is exactly what the driver
        reported — hidden-symbol completeness is driver/broker
        dependent (see ``nse_bridge.symbols.inventory``). Returns
        ``None`` only when MT5 itself returns ``None``.
        """

        def job(mt5: Any) -> list[Any] | None:
            return mt5.symbols_get()

        return self.run(job, op="symbols_get")

    def symbol_info(self, name: str) -> Any | None:
        """One symbol's ``SymbolInfo`` record (``None`` when unknown)."""

        def job(mt5: Any) -> Any | None:
            return mt5.symbol_info(name)

        return self.run(job, op="symbol_info", context={"symbol": name})

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        """Add/remove ``name`` from Market Watch; returns the broker answer."""

        def job(mt5: Any) -> bool:
            return bool(mt5.symbol_select(name, enable))

        return self.run(job, op="symbol_select", context={"symbol": name})
