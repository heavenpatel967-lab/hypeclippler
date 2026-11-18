"""Free memory checks, so heavy clip work waits instead of crashing with
"[WinError 8] Not enough memory resources are available to process this command"."""
import ctypes
import gc
import logging
import os
import time

log = logging.getLogger("resources")


def memory_mb():
    """(free_mb, total_mb). On Windows 'free' is the smaller of free RAM and free commit
    (RAM + page file): WinError 8 happens when the commit limit runs out."""
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return min(st.ullAvailPhys, st.ullAvailPageFile) // 2**20, st.ullTotalPhys // 2**20
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) // 1024
        return info.get("MemAvailable", info.get("MemFree", 0)), info.get("MemTotal", 0)
    except Exception:  # noqa: BLE001 - unknown OS: don't block anything
        return None, None


def wait_for_memory(min_mb, on_wait=None, max_wait=900):
    """Blocks until at least min_mb is free (or max_wait seconds passed)."""
    start = time.time()
    while True:
        free, _ = memory_mb()
        if free is None or free >= min_mb or time.time() - start > max_wait:
            return free
        gc.collect()
        if on_wait:
            on_wait(free)
        time.sleep(5)


def is_oom(exc) -> bool:
    if isinstance(exc, MemoryError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (8, 14, 1455):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("winerror 8", "not enough memory", "out of memory", "cannot allocate memory",
                                   "paging file is too small", "cuda_error_out_of_memory"))
