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
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

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
        user32._nse_prototypes_set = True
    except Exception:  # noqa: BLE001, S110 — a fake/test user32 may not accept prototypes; calls still go through
        pass


def def_window_proc(user32: Any, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    """Default handling for messages a custom WndProc does not consume."""
    _configure(user32)
    return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))


def register_class(user32: Any, name: str, wndproc: Any, hinstance: int) -> bool:
    """Register a window class; ``False`` when registration failed
    (e.g. the class already exists — which is fine, the caller retries
    creation anyway and treats failure there as unavailable)."""
    _configure(user32)
    try:
        cls = WNDCLASSW()
        cls.style = 0
        cls.lpfnWndProc = wndproc
        cls.cbClsExtra = 0
        cls.cbWndExtra = 0
        cls.hInstance = hinstance
        cls.hIcon = None
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


def start_pump_thread(user32: Any, name: str) -> threading.Thread:
    """Run :func:`pump_messages` on a daemon thread."""
    thread = threading.Thread(target=pump_messages, args=(user32,), daemon=True, name=name)
    thread.start()
    return thread


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
