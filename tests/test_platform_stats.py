"""macOS and Windows process figures (observability/platform_stats.py). The system calls are faked here (the tests run
on Linux) and run for real on their own OS in the native CI legs."""

import ctypes
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from pengumon.observability import platform_stats as ps
from pengumon.observability import runtime


@pytest.fixture(autouse=True)
def _fresh_log_state():
    ps._failed.clear()
    yield
    ps._failed.clear()


def _mac_lib(resident=123_456_789, threads=7, task_status=0, pidinfo_size=None):
    lib = SimpleNamespace()

    def task_info(task, flavor, ref, count):
        assert task == 99 and flavor == ps.MACH_TASK_BASIC_INFO
        assert count._obj.value == 12  # words of the 48-byte struct
        ref._obj.resident_size = resident
        ref._obj.resident_size_max = resident * 2  # the peak must not be what is read
        return task_status

    def proc_pidinfo(pid, flavor, arg, ref, size):
        assert pid == os.getpid() and flavor == ps.PROC_PIDTASKINFO and size == 96
        ref._obj.pti_threadnum = threads
        return size if pidinfo_size is None else pidinfo_size

    lib.task_info, lib.proc_pidinfo = task_info, proc_pidinfo
    return lib


def test_struct_sizes_match_the_system_headers():
    assert ctypes.sizeof(ps._MachTaskBasicInfo) == 48
    assert ctypes.sizeof(ps._ProcTaskInfo) == 96


def test_mac_rss_is_the_resident_size_not_the_peak(monkeypatch):
    lib = _mac_lib()
    monkeypatch.setattr(ps, "_mach_task", lambda lib: 99)
    assert ps.mac_rss(lib) == 123_456_789


def test_mac_failures_give_none(monkeypatch):
    lib = _mac_lib(task_status=5, pidinfo_size=0)
    monkeypatch.setattr(ps, "_mach_task", lambda lib: 99)
    assert ps.mac_rss(lib) is None
    assert ps.mac_threads(lib) is None


def test_mac_threads():
    assert ps.mac_threads(_mac_lib(threads=11)) == 11


def _win_lib(working_set=250_000_000, handles=321, thread_owners=None):
    owners = [os.getpid(), os.getpid() + 1, os.getpid(), os.getpid() + 2, os.getpid()] if thread_owners is None else thread_owners
    state = {"i": 0, "closed": []}

    def memory_info(process, ref, size):
        assert process == 4242 and size == ctypes.sizeof(ps._ProcessMemoryCounters)
        ref._obj.WorkingSetSize = working_set
        ref._obj.PeakWorkingSetSize = working_set * 2
        return 1

    def handle_count(process, ref):
        ref._obj.value = handles
        return 1

    def first(snapshot, ref):
        state["i"] = 0
        ref._obj.th32OwnerProcessID = owners[0] if owners else 0
        return 1 if owners else 0

    def nxt(snapshot, ref):
        assert ref._obj.dwSize == ctypes.sizeof(ps._ThreadEntry32)  # reset before every call, as the API requires
        state["i"] += 1
        if state["i"] >= len(owners):
            return 0
        ref._obj.th32OwnerProcessID = owners[state["i"]]
        return 1

    return SimpleNamespace(GetCurrentProcess=lambda: 4242, K32GetProcessMemoryInfo=memory_info,
                           GetProcessHandleCount=handle_count, CreateToolhelp32Snapshot=lambda flags, pid: 77,
                           Thread32First=first, Thread32Next=nxt, CloseHandle=lambda h: state["closed"].append(h),
                           state=state)


def test_windows_working_set_handles_and_threads():
    lib = _win_lib()
    assert ps.win_rss(lib) == 250_000_000
    assert ps.win_handles(lib) == 321
    assert ps.win_threads(lib) == 3  # only this process's entries of the system-wide snapshot
    assert lib.state["closed"] == [77]  # the snapshot handle is closed


def test_windows_failures_give_none():
    lib = _win_lib()
    lib.K32GetProcessMemoryInfo = lambda *a: 0
    lib.GetProcessHandleCount = lambda *a: 0
    lib.CreateToolhelp32Snapshot = lambda *a: ps.INVALID_HANDLE_VALUE
    assert ps.win_rss(lib) is None and ps.win_handles(lib) is None and ps.win_threads(lib) is None


def test_platform_stats_dispatches_on_the_platform(monkeypatch):
    monkeypatch.setattr(ps, "_PLATFORM", "win32")
    monkeypatch.setattr(ps, "_windows_lib", lambda: _win_lib())
    assert ps.platform_stats() == {"rss": 250_000_000, "threads": 3, "fds": 321}
    monkeypatch.setattr(ps, "_PLATFORM", "linux")
    assert ps.platform_stats() == {}


def test_a_failing_call_is_none_and_logged_once_at_debug_never_raised(monkeypatch, caplog):
    monkeypatch.setattr(ps, "_PLATFORM", "darwin")

    def boom():
        raise OSError("no libSystem")
    monkeypatch.setattr(ps, "_darwin_lib", boom)
    with caplog.at_level(logging.DEBUG, logger="pengumon.platform_stats"):
        assert ps.platform_stats() == {"rss": None, "threads": None}
        assert ps.platform_stats() == {"rss": None, "threads": None}
    lines = [r for r in caplog.records if r.levelno == logging.DEBUG and "not available" in r.getMessage()]
    assert len(lines) == 2 and all(r.levelno == logging.DEBUG for r in lines)  # one per figure, not per scrape


def test_process_stats_uses_the_native_figures_and_falls_back_for_what_is_missing(monkeypatch):
    monkeypatch.setattr(ps, "_PLATFORM", "win32")
    monkeypatch.setattr(runtime.platform_stats, "platform_stats",
                        lambda: {"rss": 300_000_000, "threads": None, "fds": 55})
    stats = runtime.process_stats()
    assert stats["rss"] == 300_000_000 and stats["fds"] == 55
    assert stats["threads"] >= 1  # None from the system: Python's own count


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="the system calls exist on macOS and Windows only")
def test_the_real_system_calls_give_plausible_figures():
    stats = runtime.process_stats()
    assert stats["rss"] > 10_000_000
    assert stats["threads"] >= 1
    assert stats["fds"] is not None and stats["fds"] >= 3
    assert stats["user"] + stats["system"] > 0
    native = ps.platform_stats()
    assert native["rss"] > 10_000_000 and native["threads"] >= 1  # from the calls themselves, not the fallback
