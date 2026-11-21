"""Diagnosis companion to test_run_recovery.py (sprint 3.1). The original final-tree regression had ONE failure in test_run_recovery.py whose name was not captured. The most plausible mechanism is the one
demonstrated here: ``test_a_real_process_killed_mid_run_is_recovered_after_its_lease_expires`` gives the child process a 2.0 s lease with a 0.5 s heartbeat and asserts that, during a 2.6 s wait, a second process
recovers NOTHING because only the child's heartbeat keeps the run owned. A child that is starved or paged out for more than the lease term (a machine under heavy memory/CPU pressure) legitimately loses the
lease. This test proves the product behaves correctly in that situation: a live owner that is STALLED longer than its lease term is recovered by another process (never two owners), and the stalled owner,
when it wakes, can no longer change the run. It does not prove which test failed in the original run."""

import queue
import subprocess
import sys
import threading
import time
import uuid

import pytest

from agentplatform.plan_runs import PlanRunStore
from agentplatform.run_leases import LeaseConfig
from migration_support import BACKEND
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, make_plan, statuses, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import _read_lines, _wait_line


def _tree(pid: int) -> list:
    """The process and ALL its descendants: on Windows a venv ``python.exe`` is a launcher that spawns the real interpreter as a child, so suspending only the launcher would freeze nothing."""
    import ctypes
    from ctypes import wintypes

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

    snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x2, 0)
    pairs, entry = [], ENTRY()
    entry.dwSize = ctypes.sizeof(ENTRY)
    ok = ctypes.windll.kernel32.Process32First(snap, ctypes.byref(entry))
    while ok:
        pairs.append((entry.th32ProcessID, entry.th32ParentProcessID))
        ok = ctypes.windll.kernel32.Process32Next(snap, ctypes.byref(entry))
    ctypes.windll.kernel32.CloseHandle(snap)
    found = [pid]
    for p_, parent in pairs * 3:  # a few passes cover grandchildren regardless of listing order
        if parent in found and p_ not in found:
            found.append(p_)
    return found


def _each(pid: int, fn_name: str) -> None:
    import ctypes

    for target in _tree(pid):
        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, target)  # PROCESS_SUSPEND_RESUME
        if handle:
            getattr(ctypes.windll.ntdll, fn_name)(handle)
            ctypes.windll.kernel32.CloseHandle(handle)


def suspend(pid: int) -> None:
    if sys.platform == "win32":
        _each(pid, "NtSuspendProcess")
    else:
        import os
        import signal

        os.kill(pid, signal.SIGSTOP)


def resume(pid: int) -> None:
    if sys.platform == "win32":
        _each(pid, "NtResumeProcess")
    else:
        import os
        import signal

        os.kill(pid, signal.SIGCONT)


def test_a_live_owner_stalled_longer_than_its_lease_term_is_recovered_and_cannot_change_the_run_when_it_wakes(world):
    plan_id = make_plan(world, CHAIN)
    child = subprocess.Popen([sys.executable, str(BACKEND / "tests" / "crash_child.py"), world["db"].db.url, str(world["sa"].user_id), str(plan_id), "2.0", "0.5", "child-stalled"],
                             cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()
    threading.Thread(target=_read_lines, args=(child.stdout, lines), daemon=True).start()
    try:
        run_id = _wait_line(lines, "RUN ").split()[1]
        _wait_line(lines, "CALL research")  # the provider call is in flight in the child
        other = PlanRunStore(world["sf"], config=LeaseConfig(ttl_seconds=2.0, heartbeat_seconds=0.5))
        assert other.recover_expired() == [] and other.leases.snapshot(uuid.UUID(run_id))["owner_id"] == "child-stalled"  # healthy: the heartbeat keeps it
        suspend(child.pid)  # the process is ALIVE but frozen: no heartbeat (what a starved or paged-out process looks like)
        deadline, closed = time.monotonic() + 15, []
        while not closed and time.monotonic() < deadline:
            closed = other.recover_expired()
            time.sleep(0.2)
        assert [str(r) for r in closed] == [run_id]  # exactly one recovery, once the term lapsed
        resume(child.pid)  # it wakes up: its next write is fenced out
        time.sleep(1.5)
    finally:
        resume(child.pid)
        if sys.platform == "win32":  # kill the WHOLE tree: killing only the venv launcher would orphan the real interpreter (and its heartbeat)
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
        elif child.poll() is None:
            child.kill()
        child.wait(timeout=20)
    run = world["svc"].store.get(world["sa"], uuid.UUID(run_id))
    assert (run.status, run.error_code, statuses(run)) == ("failed", "interrupted", {"a": "failed", "b": "skipped", "c": "skipped"})  # recovery's verdict stands
    assert world["db"].rows("SELECT status, error_code FROM agent_executions") == [("failed", "interrupted")]
    assert other.recover_expired() == []
