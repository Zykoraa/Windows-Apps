"""Send files to the Recycle Bin instead of deleting them outright.

Anything that removes a user's music should be undoable. os.remove is not;
the Recycle Bin is, and Windows exposes it through SHFileOperationW without
needing an extra dependency.
"""

import ctypes
import ctypes.wintypes as wintypes
import os

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def available():
    return os.name == "nt" and hasattr(ctypes, "windll")


def send_to_recycle_bin(paths):
    """Recycle every path given. Returns (recycled, failed).

    Falls back to a permanent delete only where recycling is impossible --
    a file on a network share, say -- and reports which those were.
    """
    paths = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
    if not paths:
        return [], []

    if available():
        # The API takes a double-NUL-terminated list of NUL-separated paths.
        buffer = "\0".join(paths) + "\0\0"
        op = _SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=FO_DELETE,
            pFrom=buffer,
            pTo=None,
            fFlags=(FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                    | FOF_NOERRORUI | FOF_SILENT),
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        try:
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if result == 0 and not op.fAnyOperationsAborted:
                return [p for p in paths if not os.path.exists(p)], \
                       [p for p in paths if os.path.exists(p)]
        except Exception:
            pass

    recycled, failed = [], []
    for path in paths:
        try:
            os.remove(path)
            recycled.append(path)
        except OSError:
            failed.append(path)
    return recycled, failed
