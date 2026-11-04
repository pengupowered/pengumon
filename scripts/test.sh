#!/usr/bin/env bash
# Builds the test image (runtime + dev deps + tests) and runs the suite in a throwaway container. Extra args go to
# pytest. No volumes are mounted and the image contains no data/ or backups. The network stays on: the ICMP tests
# open real sockets.
#   scripts/test.sh                      whole suite
#   scripts/test.sh -k mtr -x            pytest arguments
#   scripts/test.sh --command <cmd...>   run this command in the container instead of pytest
# PENGUMON_JUNIT_DIR=<dir> (CI): pytest also writes <dir>/unit.xml (JUnit, with file and line per case), which
# scripts/junit-annotations.py turns into annotations. The directory must be writable by the container's user.
# Parallel (pytest-xdist): the suite runs in PENGUMON_TEST_WORKERS processes (default 4: fits a 4-vCPU CI runner and the
# default memory cap; 0 or 1 runs in one process, as before). A pytest argument -n <N> or -n 0 given on the command
# line wins. The hang guard still works: a stuck test ends its worker and is reported as a failure, with its stack.
# Limits, so a test that hangs or leaks cannot take the machine with it (docs/testing.md, "Limits"):
#   PENGUMON_TEST_TIMEOUT=<s>   podman itself kills the container after this long (default 900), also when this
#                               script was killed: a killed `podman run` leaves its container running otherwise.
#   PENGUMON_TEST_MEMORY=<size> memory cap, no swap (default: 512m plus 384m per worker, so 2g with 4,
#                               where the suite peaks under 900m): a leak ends in an out-of-memory kill (exit 137).
#                               Set it too when you raise PENGUMON_TEST_WORKERS by much.
#   PENGUMON_TEST_HANG_S=<s>    one test running longer than this prints every thread's stack and ends the run
#                               (default 60: faulthandler_timeout in pyproject.toml).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

workers="${PENGUMON_TEST_WORKERS:-4}"
cmd=(python -m pytest -q -p no:cacheprovider)
if [ "$workers" -gt 1 ]; then
    cmd+=(-n "$workers")
fi
mounts=()
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
else
    if [ -n "${PENGUMON_JUNIT_DIR:-}" ]; then
        mounts=(-v "$PENGUMON_JUNIT_DIR:/junit:rw")
        cmd+=(--junitxml=/junit/unit.xml -o junit_family=xunit1)
    fi
    if [ -n "${PENGUMON_TEST_HANG_S:-}" ]; then
        cmd+=(-o "faulthandler_timeout=$PENGUMON_TEST_HANG_S")
    fi
fi

# Runs the image this build produced, by its id: two runs at once never test each other's code. The image is tagged per
# work tree and the work tree's previous image is removed after the build (scripts/lib-test-image.sh), so runs leave
# no dangling images behind.
. "$ROOT/scripts/lib-test-image.sh"
build_test_image pengumon-test "$ROOT" Dockerfile.test
memory="${PENGUMON_TEST_MEMORY:-$((512 + 384 * (workers > 1 ? workers : 1)))m}"
limits=(--timeout "${PENGUMON_TEST_TIMEOUT:-900}" --memory "$memory" --memory-swap "$memory" --pids-limit 1024)
exec podman run --rm "${limits[@]}" "${mounts[@]}" "$built" "${cmd[@]}" "$@"
