"""The process figures that macOS and Windows give through system calls, read with ctypes (the standard library only).

Linux is read from /proc in runtime.py. Here: macOS (`task_info` for the resident size, `proc_pidinfo` for the threads)
and Windows (`GetProcessMemoryInfo` for the working set, `GetProcessHandleCount`, a Toolhelp snapshot for the threads).
CPU time is `os.times()` on every platform (it is `GetProcessTimes` on Windows and `times()` on macOS), and the open
descriptors on macOS come from /dev/fd; both are in runtime.py.

Every call is guarded: a failure gives None for that figure and one debug line (once per figure, not once per scrape),
never an exception: these run in the gauge callbacks on the SDK's export thread. Each function takes the library to
call, so the tests can fake it (they run on Linux).
"""

import ctypes
import os
import sys
from typing import Any, Callable, Dict, Optional, Set

from pengumon.observability.logs import get_logger

_log = get_logger("pengumon.platform_stats")
_PLATFORM = sys.platform  # (a module attribute so that the tests can pick the platform)
_failed: Set[str] = set()


def _guarded(name: str, read: Callable[[], Optional[int]]) -> Optional[int]:
    try:
        value = read()
    except Exception as exc:  # ctypes raises OSError, AttributeError, ArgumentError, ...: all mean "not available"
        if name not in _failed:
            _failed.add(name)
            _log.debug("Process figure %s is not available here: %s", name, exc)
        return None
    if value is None and name not in _failed:
        _failed.add(name)
        _log.debug("Process figure %s is not available here", name)
    return value


# --- macOS ------------------------------------------------------------------------------------------------------------

MACH_TASK_BASIC_INFO = 20
PROC_PIDTASKINFO = 4


class _MachTaskBasicInfo(ctypes.Structure):  # <mach/task_info.h>, 48 bytes (MACH_TASK_BASIC_INFO_COUNT = 12 words)
    _fields_ = [("virtual_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64), ("user_seconds", ctypes.c_int32),
                ("user_microseconds", ctypes.c_int32), ("system_seconds", ctypes.c_int32),
                ("system_microseconds", ctypes.c_int32), ("policy", ctypes.c_int32)]


class _ProcTaskInfo(ctypes.Structure):  # <sys/proc_info.h> struct proc_taskinfo, 96 bytes
    _fields_ = [("pti_virtual_size", ctypes.c_uint64), ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64), ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64), ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32), ("pti_faults", ctypes.c_int32), ("pti_pageins", ctypes.c_int32),
                ("pti_cow_faults", ctypes.c_int32), ("pti_messages_sent", ctypes.c_int32),
                ("pti_messages_received", ctypes.c_int32), ("pti_syscalls_mach", ctypes.c_int32),
                ("pti_syscalls_unix", ctypes.c_int32), ("pti_csw", ctypes.c_int32), ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32), ("pti_priority", ctypes.c_int32)]


def _darwin_lib() -> Any:
    return ctypes.CDLL(None, use_errno=True)  # libSystem: task_info and proc_pidinfo (libproc) are both in it


def _mach_task(lib: Any) -> int:
    return ctypes.c_uint32.in_dll(lib, "mach_task_self_").value  # the exported variable `mach_task_self()` reads


def mac_rss(lib: Any) -> Optional[int]:
    """Resident bytes now (`resident_size`; `ru_maxrss` would be the peak)."""
    info = _MachTaskBasicInfo()
    count = ctypes.c_uint32(ctypes.sizeof(info) // 4)
    task = _mach_task(lib)
    lib.task_info.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.task_info.restype = ctypes.c_int
    if lib.task_info(task, MACH_TASK_BASIC_INFO, ctypes.byref(info), ctypes.byref(count)) != 0:  # KERN_SUCCESS
        return None
    return int(info.resident_size)


def mac_threads(lib: Any) -> Optional[int]:
    info = _ProcTaskInfo()
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    if lib.proc_pidinfo(os.getpid(), PROC_PIDTASKINFO, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    return int(info.pti_threadnum)


# --- Windows ----------------------------------------------------------------------------------------------------------

TH32CS_SNAPTHREAD = 0x4
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ProcessMemoryCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class _ThreadEntry32(ctypes.Structure):  # THREADENTRY32
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32), ("th32ThreadID", ctypes.c_uint32),
                ("th32OwnerProcessID", ctypes.c_uint32), ("tpBasePri", ctypes.c_int32),
                ("tpDeltaPri", ctypes.c_int32), ("dwFlags", ctypes.c_uint32)]


def _windows_lib() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # (only called on Windows)


def win_rss(lib: Any) -> Optional[int]:
    """The working set: Windows' name for the resident memory."""
    lib.GetCurrentProcess.restype = ctypes.c_void_p
    lib.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    lib.K32GetProcessMemoryInfo.restype = ctypes.c_int32
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not lib.K32GetProcessMemoryInfo(lib.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize)


def win_handles(lib: Any) -> Optional[int]:
    lib.GetCurrentProcess.restype = ctypes.c_void_p
    lib.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.GetProcessHandleCount.restype = ctypes.c_int32
    count = ctypes.c_uint32(0)
    if not lib.GetProcessHandleCount(lib.GetCurrentProcess(), ctypes.byref(count)):
        return None
    return int(count.value)


def win_threads(lib: Any) -> Optional[int]:
    """The threads of this process in a Toolhelp snapshot of all the threads in the system (a few thousand entries)."""
    lib.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    lib.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    lib.Thread32First.argtypes = lib.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.Thread32First.restype = lib.Thread32Next.restype = ctypes.c_int32
    lib.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = lib.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot is None or snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        if not lib.Thread32First(snapshot, ctypes.byref(entry)):
            return None
        pid, mine = os.getpid(), 0
        while True:
            if entry.th32OwnerProcessID == pid:
                mine += 1
            entry.dwSize = ctypes.sizeof(entry)
            if not lib.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        return mine or None
    finally:
        lib.CloseHandle(snapshot)


# --- the entry point --------------------------------------------------------------------------------------------------

def platform_stats() -> Dict[str, Optional[int]]:
    """{"rss": bytes, "threads": n, "fds": n} as far as this platform's system calls give them; {} on Linux and others
    (runtime.py reads those). A figure that cannot be had is None."""
    if _PLATFORM == "darwin":
        def lib_call(read: Callable[[Any], Optional[int]]) -> Callable[[], Optional[int]]:
            return lambda: read(_darwin_lib())
        return {"rss": _guarded("rss", lib_call(mac_rss)), "threads": _guarded("threads", lib_call(mac_threads))}
    if _PLATFORM == "win32":
        def win_call(read: Callable[[Any], Optional[int]]) -> Callable[[], Optional[int]]:
            return lambda: read(_windows_lib())
        return {"rss": _guarded("rss", win_call(win_rss)), "threads": _guarded("threads", win_call(win_threads)),
                "fds": _guarded("fds", win_call(win_handles))}
    return {}
