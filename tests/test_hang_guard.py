"""The limits that keep a stuck or leaking test from taking the machine with it (docs/testing.md, "Limits"): pytest's
hang guard (faulthandler_timeout in pyproject.toml) and the container limits in the test scripts. A run of the alerts
work once looped for hours and grew to 21 GB because neither existed."""

import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUCK = '''
import time


def test_waits_forever():
    while True:
        time.sleep(0.05)


async def test_never_yields_to_the_loop():
    while True:
        pass


def test_quick():
    time.sleep(0.5)
'''


def _run(tmp_path, select, hang_s, *extra):
    """Runs one test of STUCK under the project's own pytest settings, the hang guard set to `hang_s` seconds."""
    (tmp_path / "test_stuck.py").write_text(STUCK)
    started = time.monotonic()
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(tmp_path), "-o", f"faulthandler_timeout={hang_s}", *extra, f"test_stuck.py::{select}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60)
    return run, time.monotonic() - started


def test_the_project_ends_a_run_whose_test_hangs_for_a_minute():
    options = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    assert float(options["faulthandler_timeout"]) == 60
    assert options["faulthandler_exit_on_timeout"] is True


def test_a_stuck_test_ends_the_run_with_its_stack(tmp_path):
    run, took = _run(tmp_path, "test_waits_forever", 1)
    assert run.returncode != 0
    assert "Timeout" in run.stderr and "test_waits_forever" in run.stderr
    assert took < 30


def test_a_test_that_never_yields_to_the_event_loop_is_caught_too(tmp_path):
    run, took = _run(tmp_path, "test_never_yields_to_the_loop", 1)
    assert run.returncode != 0
    assert "test_never_yields_to_the_loop" in run.stderr
    assert took < 30


def test_under_parallel_workers_a_stuck_test_fails_the_run_loudly_and_is_not_retried(tmp_path):
    """scripts/test.sh runs pytest-xdist: the guard ends the worker, and the run reports that test as failed with its
    stack. The test is not run a second time on a replacement worker (the stack is printed once)."""
    (tmp_path / "test_stuck.py").write_text(STUCK)
    started = time.monotonic()
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ROOT / "pyproject.toml"),
         "--rootdir", str(tmp_path), "-o", "faulthandler_timeout=2", "-n", "2", "test_stuck.py::test_quick",
         "test_stuck.py::test_waits_forever", "test_stuck.py::test_never_yields_to_the_loop"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60)
    took = time.monotonic() - started
    out = run.stdout + run.stderr
    assert run.returncode != 0
    assert "worker" in out and "crashed while running" in out
    for name in ("test_waits_forever", "test_never_yields_to_the_loop"):
        assert f"FAILED test_stuck.py::{name}" in out, out
    assert "1 passed" in out and "2 failed" in out
    assert out.count("Timeout (0:00:02)") == 2  # once per stuck test: neither was started again
    assert took < 30


def test_a_test_within_the_limit_passes(tmp_path):
    run, _ = _run(tmp_path, "test_quick", 5)
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_test_containers_have_a_timeout_a_memory_cap_and_a_process_cap():
    unit = (ROOT / "scripts" / "test.sh").read_text()
    for flag in ("--timeout", "--memory", "--memory-swap", "--pids-limit", '-n "$workers"'):
        assert flag in unit, flag
    ui = (ROOT / "scripts" / "test-ui.sh").read_text()
    for flag in ("--timeout", "--memory \"$memory\"", "--pids-limit", "faulthandler_timeout=480"):
        assert flag in ui, flag
    container = (ROOT / "scripts" / "test-container.sh").read_text()
    assert container.count('"${backstop[@]}"') == 3
    # nothing left behind: the fresh container's anonymous volume goes with it, and a killed run's network and
    # volume are swept by the next run
    assert container.count('rm -f -v') >= 3 and "sweep_stale\n" in container
    assert not re.search(r'rm -f( -v)? "\$ID" "\$ID-fresh"', container)  # podman 4.9 removes neither of the two
