#!/usr/bin/env bash
# Builds the UI test image (Dockerfile.ui: pengumon's Python setup on top of the UI test runner image built by
# tests/ui/runner/build.sh, which has Chromium, Node, puppeteer-core, ESLint, all pinned) and runs the browser tests in a locked-down throwaway
# container. Nothing is installed on the host. Extra args go to pytest.
#   scripts/test-ui.sh                   both UI tests
#   scripts/test-ui.sh -k lint -x        pytest arguments
#   scripts/test-ui.sh --command <cmd...>  run this command in the container instead of pytest
# PENGUMON_JUNIT_DIR=<dir> (CI): pytest also writes <dir>/ui.xml (see scripts/test.sh); the one writable mount.
# The runs (the `full` variant in four slices, six other variants) go side by side with pytest-xdist:
#   PENGUMON_UI_WORKERS=<n>     parallel runs (default 4, which suits a 4-vCPU CI runner; 1 or 0: one after another).
#                               `-n <N>` on the command line wins.
#   PENGUMON_TEST_MEMORY=<size> memory cap, no swap (default: 1g plus 800m per worker, so 4200m with 4: each run is an
#                               app, Node and Chromium, which take 300-600 MB; /tmp is a tmpfs and counts too).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${UI_RUNNER_BASE:-localhost/ui-test-runner:1}"
RUNNER="${UI_RUNNER_DIR:-}"  # optional: a directory with a run.sh taking <image> <command...>

# One browser run may take 420 s (test_ui_smoke.py RUN_TIMEOUT_S), so the hang guard (pyproject.toml) allows 480 here.
workers="${PENGUMON_UI_WORKERS:-4}"
cmd=(python -m pytest -q -p no:cacheprovider -o faulthandler_timeout=480)
if [ "$workers" -gt 1 ]; then
    cmd+=(-n "$workers" --dist worksteal)  # worksteal: a worker that is done takes unstarted runs from the others
fi
cmd+=(tests/test_ui_smoke.py tests/test_ui_lint.py tests/test_ui_steps.py)
mounts=()
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
elif [ -n "${PENGUMON_JUNIT_DIR:-}" ]; then
    mounts=(-v "$PENGUMON_JUNIT_DIR:/junit:rw")
    cmd+=(--junitxml=/junit/ui.xml -o junit_family=xunit1)
fi

if ! podman image exists "$BASE"; then
    echo "base image $BASE is missing: run tests/ui/runner/build.sh first" >&2
    exit 1
fi

# The only step with network access (pip, for the hash-locked Python test requirements).
# Run by id, not by a tag, so two runs at once never test each other's code. The image is tagged per work tree and the
# work tree's previous image is removed after the build (scripts/lib-test-image.sh): no dangling images are left.
. "$ROOT/scripts/lib-test-image.sh"
build_test_image pengumon-ui-test "$ROOT" Dockerfile.ui --build-arg "BASE=$BASE"

# A shared runner script, when UI_RUNNER_DIR names one (several projects keeping the hardening in one place).
if [ -n "$RUNNER" ] && [ -x "$RUNNER/run.sh" ]; then
    exec "$RUNNER/run.sh" "$built" "${cmd[@]}" "$@"  # (no JUnit mount through a shared runner)
fi

memory="${PENGUMON_TEST_MEMORY:-$((1024 + 800 * (workers > 1 ? workers : 1)))m}"

# Otherwise the hardening flags inline:
# --rm                          throwaway container.
# --network none                the tests only use loopback inside the container; no route out.
# --cap-drop=ALL                no Linux capabilities (the image's user is non-root anyway).
# --security-opt no-new-privileges   no privilege gain through setuid binaries.
# --read-only                   root filesystem is immutable; only the tmpfs below is writable.
# --tmpfs /tmp:...              the one writable place (pytest tmp dirs, seeded DB, screenshots, Chromium profile);
#                               size-limited, no setuid/devices, not executable.
# --memory, --memory-swap       caps runaway browsers; swap equal to memory means no swap.
# --pids-limit                  caps processes/threads (Chromium forks many).
# --shm-size                    /dev/shm for Chromium's renderer; the 64m default is tight for Chromium.
# --timeout                     podman itself kills the container after this long, also when this script was killed
#                               (a killed `podman run` leaves its container running otherwise). PENGUMON_TEST_TIMEOUT.
exec podman run --rm \
    --timeout "${PENGUMON_TEST_TIMEOUT:-900}" \
    --network none \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,size=1g,nosuid,nodev,noexec \
    --memory "$memory" --memory-swap "$memory" \
    --pids-limit 1024 \
    --shm-size 512m \
    "${mounts[@]}" \
    "$built" "${cmd[@]}" "$@"
