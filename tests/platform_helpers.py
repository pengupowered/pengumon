"""What tests check differently per OS, each with the reason."""

import os
import stat
import sys

# Windows has no POSIX permission bits: os.chmod only sets or clears the read-only flag, and st_mode reports 0o666 or
# 0o444. Files there are protected by the user profile's ACL (%LOCALAPPDATA%), not by a mode, so a mode is checked
# only where modes exist.
POSIX_MODES = sys.platform != "win32"


def assert_mode(path, mode: int) -> None:
    """`path` has exactly these permission bits (on an OS that has them)."""
    if POSIX_MODES:
        assert stat.S_IMODE(os.stat(path).st_mode) == mode, f"{path}: {oct(stat.S_IMODE(os.stat(path).st_mode))}"


def skip_without_tzset() -> None:
    """Skips the calling test or fixture where the process time zone cannot be switched (time.tzset is Unix-only):
    output pinned to UTC can't be produced on Windows that way."""
    import time

    import pytest

    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() is Unix-only: the process time zone can't be set to UTC here (Windows)")


def _skipif(condition: bool, reason: str):
    import pytest

    return pytest.mark.skipif(condition, reason=reason)


# The maintainer's shell scripts (scripts/deploy.sh, scripts/changelog-section.sh) are bash run on Linux; on a Windows
# runner `bash` is WSL's launcher with no distribution installed, and a .sh file is not an executable.
posix_shell = _skipif(sys.platform == "win32", "the maintainer's bash scripts run on Linux and macOS, not Windows")

# Windows has no SIGTERM to deliver: Popen.terminate() there is TerminateProcess, which nothing can handle.
posix_signals = _skipif(sys.platform == "win32", "SIGTERM handling is POSIX-only (Windows terminates outright)")

# Windows has no FIFOs (os.mkfifo) and no permission bits to make a file unreadable or readable by others.
posix_files = _skipif(sys.platform == "win32", "needs POSIX file types or permission bits (Windows has neither)")
