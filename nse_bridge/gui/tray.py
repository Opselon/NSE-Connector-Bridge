"""System-tray icon: Open Dashboard / Show Logs / Quit.

The v1.2.2 failure: the bridge ran with nothing visible at all — no
window, no console, no tray — so a user could only tell it was alive
from Task Manager. The tray icon is the persistent visible presence:
right-click for the menu, double-click to open the dashboard, Quit to
stop the bridge cleanly.

Windows-only implementation via the raw ``Shell_NotifyIconW`` API
(ctypes, no new dependency — pywebview has no tray API and the frozen
exe cannot take a new GUI toolkit). On other platforms
:func:`start_tray` returns an unavailable stub and the caller logs a
warning; the browser remains the visible surface there.

The icon is taken from the running executable itself
(``ExtractIconW(sys.executable, 0)``) — the frozen exe already
carries the NSE icon resource, so no icon file has to be bundled.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from loguru import logger

from . import _win32

_TRAY_CLASS = "NSEBridgeTray"
_TRAY_CALLBACK_MESSAGE = _win32.WM_USER + 1

#: ``Shell_NotifyIcon`` operations.
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

#: ``NOTIFYICONDATA`` flags.
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

#: ``TrackPopupMenu`` flags.
TPM_RIGHTBUTTON = 0x00000002
TPM_RETURNCMD = 0x00000100

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800

#: Menu command ids (must be unique within the tray menu).
MENU_OPEN = 1001
MENU_SHOW_LOGS = 1002
MENU_QUIT = 1003


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", ctypes.c_uint),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon:
    """A running (or unavailable) tray icon."""

    def __init__(
        self,
        on_open: Callable[[], None] | None = None,
        on_show_logs: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        tooltip: str = "NSE MT5 Bridge",
    ) -> None:
        self.available = False
        #: Set when the user picks Quit — the owner polls this (or the
        #: ``on_quit`` callback) to shut the server down.
        self.quit_requested = threading.Event()
        self._on_open = on_open or (lambda: None)
        self._on_show_logs = on_show_logs or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._tooltip = tooltip
        self._user32: Any = None
        self._shell32: Any = None
        self._hwnd = 0
        self._icon = 0
        self._pump_thread: threading.Thread | None = None
        self._wndproc = None
        self._stopped = False

    # -- menu dispatch (pure logic, unit-tested without Windows) -----
    def dispatch_command(self, command_id: int) -> bool:
        """Run the action for a menu ``command_id``; ``False`` when the
        id is unknown. Called from the WndProc / popup-menu path."""
        try:
            if command_id == MENU_OPEN:
                self._on_open()
            elif command_id == MENU_SHOW_LOGS:
                self._on_show_logs()
            elif command_id == MENU_QUIT:
                self.quit_requested.set()
                self._on_quit()
            else:
                return False
            return True
        except Exception:  # noqa: BLE001 — a menu action must never crash the pump thread
            logger.exception("[GUI] tray action failed (command {})", command_id)
            return True

    # -- native lifetime ---------------------------------------------
    def _handler(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == _TRAY_CALLBACK_MESSAGE:
            if lparam == _win32.WM_RBUTTONUP:
                self._show_menu()
                return 0
            if lparam == _win32.WM_LBUTTONDBLCLK:
                self.dispatch_command(MENU_OPEN)
                return 0
            return 0
        if msg == _win32.WM_COMMAND:
            self.dispatch_command(wparam & 0xFFFF)
            return 0
        if msg == _win32.WM_DESTROY:
            try:
                self._user32.PostQuitMessage(0)
            except Exception:  # noqa: BLE001, S110
                pass
            return 0
        return _win32.def_window_proc(self._user32, hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        """Popup menu at the cursor; the selected command runs inline
        (``TPM_RETURNCMD``)."""
        try:
            user32 = self._user32
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            menu = user32.CreatePopupMenu()
            if not menu:
                return
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN, "Open Dashboard")
            user32.AppendMenuW(menu, MF_STRING, MENU_SHOW_LOGS, "Show Logs")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "Quit")
            # The menu must be dismissed by the foreground window or it
            # can linger after the click; the WM_NULL post is the
            # documented companion to SetForegroundWindow here.
            user32.SetForegroundWindow(self._hwnd)
            chosen = int(user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, self._hwnd, None))
            user32.PostMessageW(self._hwnd, _win32.WM_NULL, 0, 0)
            user32.DestroyMenu(menu)
            if chosen:
                self.dispatch_command(chosen)
        except Exception:  # noqa: BLE001 — a failed menu never kills the bridge
            logger.exception("[GUI] tray menu failed")

    def _load_icon(self) -> int:
        """The exe's own icon (frozen) or the stock application icon."""
        try:
            shell32 = self._shell32
            shell32.ExtractIconW.restype = wintypes.HICON
            icon = int(shell32.ExtractIconW(None, sys.executable, 0) or 0)
            if icon:
                return icon
            user32 = self._user32
            user32.LoadIconW.restype = wintypes.HICON
            return int(user32.LoadIconW(None, _win32.IDI_APPLICATION) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _create(self) -> bool:
        windll = _win32.windll()
        if windll is None:
            return False
        self._user32 = windll.user32
        self._shell32 = windll.shell32
        user32 = self._user32
        self._wndproc = _win32.make_wndproc(user32, self._handler)
        hinstance = _win32.module_handle(windll.kernel32)
        _win32.register_class(user32, _TRAY_CLASS, self._wndproc, hinstance)
        # Message-only window: never visible, exists only to receive
        # the tray callbacks and the popup menu.
        self._hwnd = _win32.create_window(
            user32, _TRAY_CLASS, "NSE MT5 Bridge Tray", 0, 0, 0, 0, hinstance, _win32.WS_POPUP
        )
        if not self._hwnd:
            return False
        self._icon = self._load_icon()
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_TIP | (NIF_ICON if self._icon else 0)
        data.uCallbackMessage = _TRAY_CALLBACK_MESSAGE
        data.hIcon = self._icon
        data.szTip = self._tooltip[:127]
        try:
            if not self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                return False
        except Exception:  # noqa: BLE001
            return False
        self._pump_thread = _win32.start_pump_thread(user32, "nse-tray-pump")
        return True

    def update_tooltip(self, text: str) -> None:
        """Change the hover tooltip (no-op when unavailable)."""
        if not self.available or not self._hwnd:
            return
        try:
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = self._hwnd
            data.uID = 1
            data.uFlags = NIF_TIP
            data.szTip = text[:127]
            self._shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        except Exception:  # noqa: BLE001, S110
            pass

    def stop(self) -> None:
        """Remove the icon and close the message window."""
        if self._stopped:
            return
        self._stopped = True
        self.available = False
        if self._hwnd and self._shell32 is not None:
            try:
                data = NOTIFYICONDATAW()
                data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                data.hWnd = self._hwnd
                data.uID = 1
                self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            except Exception:  # noqa: BLE001, S110
                pass
            if self._user32 is not None:
                _win32.close_window(self._user32, self._hwnd)
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=2.0)


def start_tray(
    on_open: Callable[[], None] | None = None,
    on_show_logs: Callable[[], None] | None = None,
    on_quit: Callable[[], None] | None = None,
    tooltip: str = "NSE MT5 Bridge",
) -> TrayIcon:
    """Start the tray icon; never raises. ``icon.available`` is False
    on non-Windows or when the native icon could not be created."""
    tray = TrayIcon(on_open=on_open, on_show_logs=on_show_logs, on_quit=on_quit, tooltip=tooltip)
    if not _win32.is_windows():
        logger.warning("[GUI] tray icon is Windows-only; no tray on {}", sys.platform)
        return tray
    try:
        tray.available = tray._create()
    except Exception:  # noqa: BLE001
        tray.available = False
    if tray.available:
        logger.info("[GUI] tray icon started")
    else:
        logger.warning("[GUI] tray icon could not be created")
    return tray


def open_folder(path: str) -> bool:
    """Open ``path`` in the OS file manager (the tray's "Show Logs"
    action). Never raises."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = ["MENU_OPEN", "MENU_QUIT", "MENU_SHOW_LOGS", "TrayIcon", "open_folder", "start_tray"]
