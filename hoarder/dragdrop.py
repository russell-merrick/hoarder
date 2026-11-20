"""Drag one file into another program as CF_HDROP.

A browser drag cannot carry a real file path. Ableton rejects that drop.
DoDragDrop from this process is the same file drop Explorer uses, which
Ableton accepts. If the mouse button is already up when the call starts,
the drag is cancelled so the file is not dropped on whatever is underneath.
"""

from __future__ import annotations

import sys
from ctypes import wintypes
from pathlib import Path

import ctypes

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
DVASPECT_CONTENT = 1
TYMED_HGLOBAL = 1
MK_LBUTTON = 0x0001
S_OK = 0
S_FALSE = 1
E_NOTIMPL = 0x80004001
E_NOINTERFACE = 0x80004002
DRAGDROP_S_DROP = 0x00040100
DRAGDROP_S_CANCEL = 0x00040101
DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102
DROPEFFECT_COPY = 1

_alive: list[object] = []


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", wintypes.BYTE * 8),
    ]


def _guid(data1: int) -> _GUID:
    return _GUID(data1, 0, 0, (wintypes.BYTE * 8)(0xC0, 0, 0, 0, 0, 0, 0, 0x46))


_IID_IUNKNOWN = _guid(0x00000000)
_IID_IDATAOBJECT = _guid(0x0000010E)
_IID_IDROPSOURCE = _guid(0x00000121)
_IID_IENUMFORMATETC = _guid(0x00000103)


def _same(left: _GUID, right: _GUID) -> bool:
    return bytes(left) == bytes(right)


def _hr(code: int) -> int:
    return ctypes.c_long(code).value


def _addr(value) -> int:
    if isinstance(value, int):
        return value
    return ctypes.cast(value, ctypes.c_void_p).value or 0


class _DropFiles(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


class _FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", wintypes.WORD),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", wintypes.DWORD),
        ("lindex", wintypes.LONG),
        ("tymed", wintypes.DWORD),
    ]


class _STGMEDIUM(ctypes.Structure):
    _fields_ = [
        ("tymed", wintypes.DWORD),
        ("hGlobal", ctypes.c_void_p),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


def _file_format() -> _FORMATETC:
    fmt = _FORMATETC()
    fmt.cfFormat = CF_HDROP
    fmt.dwAspect = DVASPECT_CONTENT
    fmt.lindex = -1
    fmt.tymed = TYMED_HGLOBAL
    return fmt


def _matches(fmt: _FORMATETC) -> bool:
    return fmt.cfFormat == CF_HDROP and (fmt.tymed & TYMED_HGLOBAL) != 0


def hdrop_bytes(path: Path) -> bytes:
    encoded = str(path.resolve()).encode("utf-16-le") + b"\x00\x00\x00\x00"
    header = _DropFiles()
    header.pFiles = ctypes.sizeof(_DropFiles)
    header.fWide = True
    return bytes(header) + encoded


def _alloc_hdrop(path: Path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    payload = hdrop_bytes(path)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(payload))
    if not handle:
        raise OSError("could not allocate the drag file")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise OSError("could not lock the drag file")
    ctypes.memmove(locked, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    return handle


def paths_from_hdrop(handle: int) -> list[str]:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    found = []
    for index in range(count):
        size = shell32.DragQueryFileW(handle, index, None, 0)
        buffer = ctypes.create_unicode_buffer(size + 1)
        shell32.DragQueryFileW(handle, index, buffer, size + 1)
        found.append(buffer.value)
    return found


_QI = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
_ADDREF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_GETDATA = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM))
_GETDATAHERE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM))
_QUERYGETDATA = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC))
_CANONICAL = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC), ctypes.POINTER(_FORMATETC))
_SETDATA = ctypes.WINFUNCTYPE(
    ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC), ctypes.POINTER(_STGMEDIUM), wintypes.BOOL
)
_ENUM = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
_ADVISE = ctypes.WINFUNCTYPE(
    ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_FORMATETC), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)
)
_UNADVISE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.DWORD)
_ENUMADVISE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_CONTINUE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD)
_FEEDBACK = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.DWORD)
_NEXT = ctypes.WINFUNCTYPE(
    ctypes.HRESULT, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(_FORMATETC), ctypes.POINTER(wintypes.ULONG)
)
_SKIP = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.ULONG)
_RESET = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)
_CLONE = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))


class _EnumVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _RELEASE),
        ("Next", _NEXT),
        ("Skip", _SKIP),
        ("Reset", _RESET),
        ("Clone", _CLONE),
    ]


class _EnumObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_EnumVtbl))]


class _DataVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _RELEASE),
        ("GetData", _GETDATA),
        ("GetDataHere", _GETDATAHERE),
        ("QueryGetData", _QUERYGETDATA),
        ("GetCanonicalFormatEtc", _CANONICAL),
        ("SetData", _SETDATA),
        ("EnumFormatEtc", _ENUM),
        ("DAdvise", _ADVISE),
        ("DUnadvise", _UNADVISE),
        ("EnumDAdvise", _ENUMADVISE),
    ]


class _DataObject(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_DataVtbl))]


class _SourceVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _RELEASE),
        ("QueryContinueDrag", _CONTINUE),
        ("GiveFeedback", _FEEDBACK),
    ]


class _DropSource(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_SourceVtbl))]


def _notimpl(*_args):
    return _hr(E_NOTIMPL)


def _ref(_this):
    return 1


def _data_qi(this, iid, out):
    guid = iid.contents
    if _same(guid, _IID_IUNKNOWN) or _same(guid, _IID_IDATAOBJECT):
        out[0] = this
        return S_OK
    out[0] = None
    return _hr(E_NOINTERFACE)


def _source_qi(this, iid, out):
    guid = iid.contents
    if _same(guid, _IID_IUNKNOWN) or _same(guid, _IID_IDROPSOURCE):
        out[0] = this
        return S_OK
    out[0] = None
    return _hr(E_NOINTERFACE)


def _enum_qi(this, iid, out):
    guid = iid.contents
    if _same(guid, _IID_IUNKNOWN) or _same(guid, _IID_IENUMFORMATETC):
        out[0] = this
        return S_OK
    out[0] = None
    return _hr(E_NOINTERFACE)


def make_data_object(path: Path):
    """IDataObject that offers this one file. The object stays alive with the returned tuple."""
    state = {"path": path, "down": False}

    def get_data(_this, fmt, medium):
        if not _matches(fmt.contents):
            return _hr(0x80040064)  # DV_E_FORMATETC
        medium.contents.tymed = TYMED_HGLOBAL
        medium.contents.hGlobal = _alloc_hdrop(state["path"])
        medium.contents.pUnkForRelease = None
        return S_OK

    def query_get(_this, fmt):
        return S_OK if _matches(fmt.contents) else _hr(0x80040064)

    def enum_formats(_this, _direction, out):
        enum, enum_vtbl = _make_enum()
        _alive.append((enum, enum_vtbl))
        out[0] = ctypes.addressof(enum)
        return S_OK

    vtbl = _DataVtbl(
        _QI(_data_qi),
        _ADDREF(_ref),
        _RELEASE(_ref),
        _GETDATA(get_data),
        _GETDATAHERE(_notimpl),
        _QUERYGETDATA(query_get),
        _CANONICAL(_notimpl),
        _SETDATA(_notimpl),
        _ENUM(enum_formats),
        _ADVISE(_notimpl),
        _UNADVISE(_notimpl),
        _ENUMADVISE(_notimpl),
    )
    obj = _DataObject(ctypes.pointer(vtbl))
    _alive.append((obj, vtbl, state))
    return obj, vtbl


def _make_enum():
    fmt = _file_format()
    done = {"value": False}

    def next_item(_this, count, items, fetched):
        if done["value"] or count < 1:
            if fetched:
                fetched[0] = 0
            return S_FALSE
        items[0] = fmt
        done["value"] = True
        if fetched:
            fetched[0] = 1
        return S_OK

    def reset(_this):
        done["value"] = False
        return S_OK

    vtbl = _EnumVtbl(
        _QI(_enum_qi),
        _ADDREF(_ref),
        _RELEASE(_ref),
        _NEXT(next_item),
        _SKIP(_notimpl),
        _RESET(reset),
        _CLONE(_notimpl),
    )
    return _EnumObject(ctypes.pointer(vtbl)), vtbl


def make_drop_source():
    state = {"down": False}

    def query_continue(_this, escape, keys):
        if escape:
            return DRAGDROP_S_CANCEL
        if keys & MK_LBUTTON:
            state["down"] = True
            return S_OK
        if not state["down"]:
            return DRAGDROP_S_CANCEL
        return DRAGDROP_S_DROP

    def feedback(_this, _effect):
        return DRAGDROP_S_USEDEFAULTCURSORS

    vtbl = _SourceVtbl(
        _QI(_source_qi),
        _ADDREF(_ref),
        _RELEASE(_ref),
        _CONTINUE(query_continue),
        _FEEDBACK(feedback),
    )
    obj = _DropSource(ctypes.pointer(vtbl))
    _alive.append((obj, vtbl, state))
    return obj, vtbl


def drag_file(path: Path) -> int:
    """Run a copy drag of this file. Returns the drop effect, or 0 when cancelled."""
    if sys.platform != "win32":
        raise OSError("file drag is only available on Windows")
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = ctypes.HRESULT
    ole32.OleUninitialize.argtypes = []
    ole32.OleUninitialize.restype = None
    ole32.DoDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ole32.DoDragDrop.restype = ctypes.HRESULT
    started = ole32.OleInitialize(None) == S_OK
    try:
        data, _data_vtbl = make_data_object(path)
        source, _source_vtbl = make_drop_source()
        effect = wintypes.DWORD(0)
        ole32.DoDragDrop(
            ctypes.addressof(data),
            ctypes.addressof(source),
            DROPEFFECT_COPY,
            ctypes.byref(effect),
        )
        return int(effect.value)
    finally:
        if started:
            ole32.OleUninitialize()
