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


_freers = []


def register_freer(fn):
    """Functions that release memory (unload Whisper, unload the AI model...)."""
    _freers.append(fn)


def relieve():
    """Frees everything we can before retrying."""
    for fn in _freers:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.debug("freer failed: %s", e)
    gc.collect()


def commit_info():
    """Windows: (commit limit MB, physical RAM MB). The commit limit is RAM + page file.
    'Could not start: WinError 8' means the commit limit was reached."""
    if os.name != "nt":
        return None, None
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullTotalPageFile // 2**20, st.ullTotalPhys // 2**20
    except Exception:  # noqa: BLE001
        return None, None


def _page_file_setting():
    """Reads Windows' page file setting. Returns "auto", a custom max size in MB, 0 (none)
    or None if unknown."""
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management")
        value, _ = winreg.QueryValueEx(key, "PagingFiles")
        entries = value if isinstance(value, list) else [value]
        total = 0
        for e in entries:
            parts = e.strip().split()
            if not parts:
                continue
            if parts[0].startswith("?:") or len(parts) == 1 or parts[1:3] == ["0", "0"]:
                return "auto"  # "Automatically manage" or "System managed size"
            total += int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        return total
    except Exception:  # noqa: BLE001
        return None


def page_file_small():
    """True when Windows can't give enough memory: no page file or a small fixed one.
    A Windows-managed page file grows when needed, so that's fine even while it's small."""
    setting = _page_file_setting()
    if setting == "auto":
        return False
    if isinstance(setting, int):
        return setting < 16 * 1024
    limit, phys = commit_info()
    return bool(limit and phys and limit - phys < 8 * 1024)


def wait_until_runnable(name, on_wait=None, max_wait=1800):
    """After an out-of-memory error: free memory, then wait until there is room again."""
    relieve()
    start = time.time()
    while time.time() - start < max_wait:
        free, _ = memory_mb()
        if free is None or free >= 2500:
            return True
        if on_wait:
            on_wait(free)
        log.info("waiting for memory to run %s (%s MB free)", name, free)
        time.sleep(10)
        relieve()
    return False
