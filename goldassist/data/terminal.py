"""The MetaTrader 5 terminal process, as seen from outside it.

``mt5.initialize`` does not just attach to a terminal: when none is running it
*starts* one. That is convenient at launch and a problem everywhere else -- a
reconnect after the trader closes MT5 opens it straight back up, and a terminal
the application started outlives the application that needed it.

So the source records which terminal processes appeared during its own
``initialize`` calls, and the application closes exactly those on exit. A
terminal the trader already had open is never touched.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

TERMINAL_IMAGE = "terminal64.exe"


def running() -> set[int]:
    """Process ids of every running MetaTrader 5 terminal. Empty off Windows."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:
        return set()

    TH32CS_SNAPPROCESS = 0x2
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return set()

    found: set[int] = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == TERMINAL_IMAGE:
                found.add(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def close(pids: set[int], timeout: float = 8.0) -> None:
    """Close the given terminals: politely first, then by force.

    WM_CLOSE lets MT5 save its profile and chart layout the way it does when
    the trader closes it by hand. Only a terminal that ignores that is killed.
    Positions live on the broker's server, so neither path affects a trade.
    """
    pids = set(pids) & running()
    if not pids:
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    WM_CLOSE = 0x0010

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _post_close(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    log.info("Closing the MetaTrader 5 terminal this application started "
             "(pid %s)", ", ".join(map(str, sorted(pids))))
    user32.EnumWindows(_post_close, 0)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pids & running():
            return
        time.sleep(0.25)

    PROCESS_TERMINATE = 0x0001
    kernel32.OpenProcess.restype = wintypes.HANDLE
    for pid in pids & running():
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            log.warning("MetaTrader 5 (pid %s) did not close; terminating it", pid)
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
