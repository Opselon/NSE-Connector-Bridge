"""Shared Win32 plumbing for the visible-startup pieces (splash + tray).

Stdlib-only (``ctypes``). Everything here is best-effort and never
raises into the caller: a window that cannot be created simply means
"no splash/tray on this machine", and startup continues with the
browser as the visible surface instead.

Windows-only by design: every public helper returns ``None``/``False``
on other platforms so the callers stay platform-agnostic and unit
tests can exercise the wiring with a fake ``ctypes.windll``.

64-bit correctness: all Win32 functions used here get explicit
``argtypes``/``restype`` — without them ctypes truncates handles
(``HWND``/``HANDLE``) to ``int`` and the calls silently corrupt.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

#: Message the tray icon uses to report mouse/menu events to its window.
WM_USER = 0x0400
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_SETFONT = 0x0030
WM_SETTEXT = 0x000C
WM_SETICON = 0x0080
WM_QUIT = 0x0012
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

#: ``LoadImageW`` image type / load flag for loading an .ico file.
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

#: ``WM_SETICON`` icon sizes.
ICON_SMALL = 0
ICON_BIG = 1

#: ``SendMessageTimeoutW`` flag: return at once when the target
#: thread is hung instead of blocking the caller.
SMTO_ABORTIFHUNG = 0x0002

#: Progress-bar (``msctls_progress32``) messages.
PBM_SETRANGE = 0x0401
PBM_SETPOS = 0x0402

WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000

SW_SHOWNORMAL = 1
CW_USEDEFAULT = 0x80000000
IDC_ARROW = 32512
IDI_APPLICATION = 32512

#: ``(HBRUSH)(COLOR_BTNFACE + 1)`` — a plain dialog-face background.
_BRUSH_DIALOG = 16

#: ``WNDPROC`` signature: ``LRESULT WndProc(HWND, UINT, WPARAM, LPARAM)``.
#: ``WINFUNCTYPE`` exists only on Windows (stdcall); elsewhere
#: ``CFUNCTYPE`` is the identical shape and lets the wiring be tested.
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WndProcT = _WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


#: Handle types that ``ctypes.wintypes`` only defines on Windows —
#: fall back to ``c_void_p`` elsewhere so the module imports (and its
#: wiring is testable) on Linux CI. The real Win32 code only ever
#: runs on Windows, where the real types exist.
_HCURSOR = getattr(wintypes, "HCURSOR", ctypes.c_void_p)
_HICON = getattr(wintypes, "HICON", ctypes.c_void_p)
_HINSTANCE = getattr(wintypes, "HINSTANCE", ctypes.c_void_p)
_HBRUSH = getattr(wintypes, "HBRUSH", ctypes.c_void_p)
_HMENU = getattr(wintypes, "HMENU", ctypes.c_void_p)
_HMODULE = getattr(wintypes, "HMODULE", ctypes.c_void_p)
_HWND = getattr(wintypes, "HWND", ctypes.c_void_p)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WndProcT),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", _HINSTANCE),
        ("hIcon", _HICON),
        ("hCursor", _HCURSOR),
        ("hbrBackground", _HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", ctypes.c_uint),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", ctypes.c_ulong),
        ("pt", wintypes.POINT),
    ]


def is_windows() -> bool:
    return sys.platform == "win32"


def windll() -> Any | None:
    """``ctypes.windll`` on Windows, else ``None`` (also ``None`` when
    the attribute is missing — e.g. a test rig without it)."""
    return getattr(ctypes, "windll", None)


def _configure(user32: Any) -> None:
    """Set 64-bit-safe prototypes once per ``user32`` object."""
    if getattr(user32, "_nse_prototypes_set", False):
        return
    try:
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        user32.SetWindowTextW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32._nse_prototypes_set = True
    except Exception:  # noqa: BLE001, S110 — a fake/test user32 may not accept prototypes; calls still go through
        pass


def def_window_proc(user32: Any, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    """Default handling for messages a custom WndProc does not consume."""
    _configure(user32)
    return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))


def register_class(user32: Any, name: str, wndproc: Any, hinstance: int, hicon: int = 0) -> bool:
    """Register a window class; ``False`` when registration failed
    (e.g. the class already exists — which is fine, the caller retries
    creation anyway and treats failure there as unavailable).

    ``hicon`` is the class icon (0 = none: Windows then shows the
    generic application icon in the title bar). A class registered
    earlier in the process keeps ITS icon — re-registering with a
    different icon does not replace it — so callers must also set
    the per-window icon with :func:`set_window_icon`, which always
    wins for that window.
    """
    _configure(user32)
    try:
        cls = WNDCLASSW()
        cls.style = 0
        cls.lpfnWndProc = wndproc
        cls.cbClsExtra = 0
        cls.cbWndExtra = 0
        cls.hInstance = hinstance
        cls.hIcon = hicon or None
        cls.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        cls.hbrBackground = _BRUSH_DIALOG
        cls.lpszMenuName = None
        cls.lpszClassName = name
        atom = user32.RegisterClassW(ctypes.byref(cls))
        return bool(atom)
    except Exception:  # noqa: BLE001 — registration failure means "no native window"
        return False


def module_handle(kernel32: Any) -> int:
    try:
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    except Exception:  # noqa: BLE001, S110 — a fake/test kernel32 may not accept prototypes
        pass
    try:
        return int(kernel32.GetModuleHandleW(None))
    except Exception:  # noqa: BLE001
        return 0


def create_window(
    user32: Any,
    class_name: str,
    title: str,
    x: int,
    y: int,
    width: int,
    height: int,
    hinstance: int,
    style: int,
) -> int:
    """Create a top-level window; ``0`` on failure."""
    _configure(user32)
    try:
        hwnd = user32.CreateWindowExW(0, class_name, title, style, x, y, width, height, None, None, hinstance, None)
        return int(hwnd or 0)
    except Exception:  # noqa: BLE001
        return 0


def create_child(
    user32: Any,
    class_name: str,
    text: str,
    parent: int,
    x: int,
    y: int,
    width: int,
    height: int,
    hinstance: int,
    style: int,
) -> int:
    """Create a child control (e.g. a ``STATIC`` label); ``0`` on failure."""
    _configure(user32)
    try:
        hwnd = user32.CreateWindowExW(0, class_name, text, style, x, y, width, height, parent, None, hinstance, None)
        return int(hwnd or 0)
    except Exception:  # noqa: BLE001
        return 0


def load_icon_file(user32: Any, path: str, size: int = 0) -> int:
    """Load an ``.ico`` file as an ``HICON`` (``0`` on failure).

    ``size`` 0 loads the icon's native (largest) size. Best-effort:
    a missing file or an unloadable icon is "no icon", never an error.
    """
    try:
        user32.LoadImageW.restype = ctypes.c_void_p
    except Exception:  # noqa: BLE001, S110 — a fake/test user32 may not accept prototypes
        pass
    try:
        icon = user32.LoadImageW(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        return int(icon or 0)
    except Exception:  # noqa: BLE001
        return 0


def load_icon_from_executable(shell32: Any, exe_path: str) -> int:
    """The first icon resource embedded in ``exe_path`` (``0`` when
    the exe carries none — e.g. a dev run from a plain python.exe)."""
    try:
        shell32.ExtractIconW.restype = _HICON
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        return int(shell32.ExtractIconW(None, exe_path, 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def set_window_icon(user32: Any, hwnd: int, hicon: int) -> None:
    """Set a window's big (title bar / taskbar / Alt-Tab) and small
    (title bar) icons. Must be called from the window's owning thread
    — a cross-thread ``SendMessageW`` to a hung owner blocks the
    caller, which is exactly the hang this module exists to avoid.
    No-op for a zero icon."""
    if not hicon:
        return
    try:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
    except Exception:  # noqa: BLE001, S110
        pass


def center_on_screen(user32: Any, width: int, height: int) -> tuple[int, int]:
    """Top-left coordinates that centre a ``width`` x ``height`` window."""
    try:
        screen_w = int(user32.GetSystemMetrics(0))
        screen_h = int(user32.GetSystemMetrics(1))
        return max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 2)
    except Exception:  # noqa: BLE001
        return CW_USEDEFAULT, CW_USEDEFAULT


def show_window(user32: Any, hwnd: int) -> None:
    try:
        user32.ShowWindow(hwnd, SW_SHOWNORMAL)
        user32.UpdateWindow(hwnd)
    except Exception:  # noqa: BLE001, S110
        pass


def set_text(user32: Any, hwnd: int, text: str) -> bool:
    try:
        return bool(user32.SetWindowTextW(hwnd, text))
    except Exception:  # noqa: BLE001
        return False


def set_text_timeout(user32: Any, hwnd: int, text: str, timeout_ms: int = 500) -> bool:
    """Set a window/control's text, but never block the caller.

    ``SetWindowTextW`` sends ``WM_SETTEXT`` synchronously to the
    window's thread — if that thread ever stops pumping (a hung
    splash pump), the caller blocks forever and the bridge looks
    "(Not Responding)" for a status-line update. ``SendMessageTimeoutW``
    with ``SMTO_ABORTIFHUNG`` bounds the wait instead: a hung target
    costs at most ``timeout_ms`` and the startup continues.
    """
    _configure(user32)
    try:
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            ctypes.c_uint,
            wintypes.WPARAM,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_ssize_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    except Exception:  # noqa: BLE001, S110 — a fake/test user32 may not accept prototypes
        pass
    try:
        result = ctypes.c_ssize_t(0)
        ret = user32.SendMessageTimeoutW(
            hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(text), SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result)
        )
        return bool(ret)
    except Exception:  # noqa: BLE001
        return False


def set_progress_range(user32: Any, hwnd: int, maximum: int = 100) -> None:
    """Set a progress bar's range to ``0..maximum`` (best-effort)."""
    try:
        user32.SendMessageW(hwnd, PBM_SETRANGE, 0, (maximum << 16) | 0)
    except Exception:  # noqa: BLE001, S110
        pass


def set_progress_pos(user32: Any, hwnd: int, pos: int, timeout_ms: int = 500) -> None:
    """Move a progress bar to ``pos`` (best-effort, never raises).

    Bounded via ``SendMessageTimeoutW``/``SMTO_ABORTIFHUNG`` like
    :func:`set_text_timeout`: a plain ``SendMessageW`` to a window
    whose owner thread stopped pumping blocks the caller forever —
    the v1.4.0 splash called this from the main thread while its
    pump was wedged, so a progress update could freeze startup.
    """
    _configure(user32)
    try:
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            ctypes.c_uint,
            wintypes.WPARAM,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_ssize_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    except Exception:  # noqa: BLE001, S110 — a fake/test user32 may not accept prototypes
        pass
    try:
        result = ctypes.c_ssize_t(0)
        user32.SendMessageTimeoutW(hwnd, PBM_SETPOS, pos, None, SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
    except Exception:  # noqa: BLE001, S110
        pass


def close_window(user32: Any, hwnd: int) -> None:
    """Ask a window to close (its WndProc's default handling destroys
    it, which ends the message pump)."""
    try:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:  # noqa: BLE001, S110
        pass


def pump_messages(user32: Any) -> None:
    """Standard message loop; returns when the owning window posts
    ``WM_QUIT`` (via ``WM_DESTROY``). Runs on the pump thread."""
    _configure(user32)
    msg = MSG()
    try:
        while True:
            ret = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
            if ret <= 0:
                return
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:  # noqa: BLE001 — a dead pump thread only loses the window, never the bridge
        return


def start_owner_thread(
    user32: Any, name: str, create_fn: Callable[[], bool]
) -> tuple[threading.Thread, threading.Event]:
    """Create a window on a dedicated thread and pump its messages
    there.

    Win32 thread affinity (the v1.4.0 splash hang): a window's
    messages live on the message queue of the thread that CREATED
    it. ``GetMessageW`` only ever retrieves the CALLING thread's
    messages, so a pump running on any other thread blocks forever
    on its own empty queue while the window's real messages are
    never dispatched — Windows then flags the window
    "(Not Responding)", ``WM_CLOSE`` is never delivered, and no
    watchdog post can rescue it. B1 created the splash on the main
    thread and pumped it on ``nse-splash-pump`` — that exact
    mismatch. The fix is to create AND pump on the SAME thread:
    ``create_fn`` runs on the new thread (it builds the window and
    returns whether one exists) and the thread then runs
    :func:`pump_messages` until ``WM_QUIT``.

    Returns ``(thread, started)``; ``started`` is set once
    ``create_fn`` returns, so the caller can bound its wait for the
    window to exist. Never raises.
    """
    started = threading.Event()

    def _run() -> None:
        created = False
        try:
            created = bool(create_fn())
        except Exception:  # noqa: BLE001 — a failed create is "no window", never a crash
            created = False
        finally:
            started.set()
        if created:
            pump_messages(user32)

    thread = threading.Thread(target=_run, daemon=True, name=name)
    thread.start()
    return thread, started


def quit_pump_thread(user32: Any, thread: threading.Thread) -> bool:
    """Post ``WM_QUIT`` to a pump thread's message queue so its
    ``GetMessageW`` returns 0 and the thread exits. Asynchronous and
    never blocks. Windows destroys the windows a thread created
    when that thread exits, so this also destroys a window whose
    ``WM_CLOSE`` was never dispatched — the last-resort close.
    ``False`` when the thread has no queue yet / no native id."""
    try:
        native_id = thread.native_id
        if not native_id:
            return False
        return bool(user32.PostThreadMessageW(native_id, WM_QUIT, 0, 0))
    except Exception:  # noqa: BLE001
        return False


def close_window_and_wait(user32: Any, hwnd: int, pump_thread: threading.Thread | None, timeout_s: float = 2.0) -> bool:
    """Close a window owned by ``pump_thread`` and wait for its pump
    to exit (i.e. the window is actually destroyed). ``True`` when
    the pump thread is gone afterwards.

    Order: polite ``WM_CLOSE`` first (the window's own handling
    destroys it, ``WM_DESTROY`` posts ``WM_QUIT``), then — only if
    the pump is still alive past ``timeout_s`` — ``WM_QUIT`` posted
    straight to the pump thread, which exits and takes its windows
    with it. Every step is bounded; a stuck window can delay the
    caller by at most ~``2 * timeout_s`` and never forever.
    """
    close_window(user32, hwnd)
    if pump_thread is None:
        return True
    pump_thread.join(timeout=timeout_s)
    if not pump_thread.is_alive():
        return True
    quit_pump_thread(user32, pump_thread)
    pump_thread.join(timeout=timeout_s)
    return not pump_thread.is_alive()


#: A WndProc implementation: ``(hwnd, msg, wparam, lparam) -> LRESULT``.
WndProcFn = Callable[[int, int, int, int], int]


def make_wndproc(user32: Any, handler: WndProcFn) -> Any:
    """Wrap a plain-Python message handler as a ``WNDPROC`` callback.

    The returned object MUST be kept alive for as long as the window
    class is registered — if it is garbage-collected, Windows calls
    into freed memory. Callers store it on the owning object.
    """

    def _proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            return int(handler(hwnd, msg, wparam, lparam))
        except Exception:  # noqa: BLE001 — a WndProc must never raise into Windows
            return def_window_proc(user32, hwnd, msg, wparam, lparam)

    return WndProcT(_proc)
