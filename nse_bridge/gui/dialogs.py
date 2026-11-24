"""Visible dialogs + clipboard for a windowed (``console=False``) exe.

The v1.2.2 failure mode: every startup error went to stderr or the
log file, and a windowed exe has neither visible — the user saw
nothing while the process sat in Task Manager. Any error that decides
whether the user can USE the bridge must therefore also appear in a
native dialog, and a dashboard URL the browser refused to open must
additionally land on the clipboard so the user can paste it manually.

Best-effort everywhere, never raising: a dialog that cannot be shown
is a degraded UX, not a startup failure — the caller still logs the
error and (for fatal errors) exits with a non-zero code.
"""

from __future__ import annotations

import sys

from loguru import logger

MB_OK = 0x00000000
MB_ICONERROR = 0x00000010
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _win32_message_box(title: str, message: str, icon: int) -> bool:
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        result = windll.user32.MessageBoxW(None, message, title, MB_OK | icon | MB_TOPMOST)
        return result != 0
    except Exception:  # noqa: BLE001 — a dialog must never break startup
        return False


def _tkinter_message_box(title: str, message: str, error: bool) -> bool:
    """Fallback dialog via tkinter (dev/Linux runs; the frozen exe
    excludes tkinter, so on Windows this path is never taken)."""
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        try:
            if error:
                messagebox.showerror(title, message, parent=root)
            else:
                messagebox.showinfo(title, message, parent=root)
        finally:
            root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def show_error_dialog(title: str, message: str) -> bool:
    """Show a native error dialog; ``True`` when one was actually shown.

    Falls back to stderr when no GUI dialog is available (headless
    Linux, no tkinter) — the message is never dropped silently.
    """
    if sys.platform == "win32":
        if _win32_message_box(title, message, MB_ICONERROR):
            return True
    elif _tkinter_message_box(title, message, error=True):
        return True
    logger.error("[GUI] (no dialog available) {}: {}", title, message)
    print(f"error: {title}: {message}", file=sys.stderr)
    return False


def show_info_dialog(title: str, message: str) -> bool:
    """Show a native info dialog; ``True`` when one was actually shown."""
    if sys.platform == "win32":
        if _win32_message_box(title, message, MB_ICONINFORMATION):
            return True
    elif _tkinter_message_box(title, message, error=False):
        return True
    logger.info("[GUI] (no dialog available) {}: {}", title, message)
    print(f"{title}: {message}", file=sys.stderr)
    return False


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard; ``True`` on success.

    Windows uses the raw clipboard API (no dependency); elsewhere
    tkinter is the fallback. Never raises.
    """
    if sys.platform == "win32":
        return _win32_copy_to_clipboard(text)
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()  # push the clipboard content out before destroy
        finally:
            root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def _win32_copy_to_clipboard(text: str) -> bool:
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        kernel32 = windll.kernel32
        user32 = windll.user32
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(locked, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            # Ownership of the handle transfers to the clipboard on
            # success — do NOT free it afterwards.
            return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        return False


__all__ = ["copy_to_clipboard", "show_error_dialog", "show_info_dialog"]
