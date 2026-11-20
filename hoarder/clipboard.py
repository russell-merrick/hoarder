"""Put one file on the Windows clipboard as CF_HDROP.

Handles from GlobalAlloc are pointers. On 64-bit Windows they must use
c_void_p, or the top half is dropped and SetClipboardData fails.
"""

from __future__ import annotations

import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import ctypes

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

_lock = threading.Lock()


class _DropFiles(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


def _libraries():
    if sys.platform != "win32":
        raise OSError("file clipboard is only available on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    return kernel32, user32, shell32


def _open(user32) -> None:
    # Another app can hold the clipboard for a few seconds. Error 5 is that wait.
    for _ in range(100):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("clipboard is busy")


def copy_file(path: Path) -> None:
    """Replace the clipboard with this one file."""
    kernel32, user32, _shell32 = _libraries()
    encoded = str(path.resolve()).encode("utf-16-le") + b"\x00\x00\x00\x00"
    header = _DropFiles()
    header.pFiles = ctypes.sizeof(_DropFiles)
    header.fWide = True
    payload = bytes(header) + encoded
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(payload))
    if not handle:
        raise OSError("could not allocate clipboard data")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise OSError("could not lock clipboard data")
    ctypes.memmove(locked, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    with _lock:
        _open(user32)
        try:
            if not user32.EmptyClipboard():
                kernel32.GlobalFree(handle)
                raise OSError("could not empty the clipboard")
            if not user32.SetClipboardData(CF_HDROP, handle):
                kernel32.GlobalFree(handle)
                raise OSError("could not set the clipboard file")
        finally:
            user32.CloseClipboard()


def files_on_clipboard() -> list[str]:
    """File paths currently on the clipboard. Empty when the clipboard holds something else."""
    _kernel32, user32, shell32 = _libraries()
    with _lock:
        _open(user32)
        try:
            handle = user32.GetClipboardData(CF_HDROP)
            if not handle:
                return []
            count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
            files: list[str] = []
            for index in range(count):
                length = shell32.DragQueryFileW(handle, index, None, 0)
                buffer = ctypes.create_unicode_buffer(length + 1)
                shell32.DragQueryFileW(handle, index, buffer, length + 1)
                files.append(buffer.value)
            return files
        finally:
            user32.CloseClipboard()


def clear_clipboard() -> None:
    """Drop whatever is on the clipboard."""
    _kernel32, user32, _shell32 = _libraries()
    with _lock:
        _open(user32)
        try:
            if not user32.EmptyClipboard():
                raise OSError("could not empty the clipboard")
        finally:
            user32.CloseClipboard()
