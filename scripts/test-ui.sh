#!/usr/bin/env bash
# Builds the UI test image (Dockerfile.ui: pengumon's Python setup on top of the UI test runner image built by
# tests/ui/runner/build.sh, which has Chromium, Node, puppeteer-core, ESLint, all pinned) and runs the browser tests in a locked-down throwaway
# container. Nothing is installed on the host. Extra args go to pytest.
#   scripts/test-ui.sh                   both UI tests
#   scripts/test-ui.sh -k lint -x        pytest arguments
#   scripts/test-ui.sh --command <cmd...>  run this command in the container instead of pytest
# PENGUMON_JUNIT_DIR=<dir> (CI): pytest also writes <dir>/ui.xml (see scripts/test.sh); the one writable mount.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="localhost/pengumon-ui-test:latest"
BASE="${UI_RUNNER_BASE:-localhost/ui-test-runner:1}"
RUNNER="${UI_RUNNER_DIR:-}"  # optional: a directory with a run.sh taking <image> <command...>

cmd=(python -m pytest -q -p no:cacheprovider tests/test_ui_smoke.py tests/test_ui_lint.py tests/test_ui_steps.py)
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
# Run by id, not by the tag every work tree shares, so two runs at once never test each other's code.
iid="$(mktemp)"
podman build -q --iidfile "$iid" --build-arg "BASE=$BASE" -f "$ROOT/Dockerfile.ui" -t "$IMAGE" "$ROOT" >/dev/null
built="$(cat "$iid")"; rm -f "$iid"

# A shared runner script, when UI_RUNNER_DIR names one (several projects keeping the hardening in one place).
if [ -n "$RUNNER" ] && [ -x "$RUNNER/run.sh" ]; then
    exec "$RUNNER/run.sh" "$built" "${cmd[@]}" "$@"  # (no JUnit mount through a shared runner)
fi

# Otherwise the hardening flags inline:
# --rm                          throwaway container.
# --network none                the tests only use loopback inside the container; no route out.
# --cap-drop=ALL                no Linux capabilities (the image's user is non-root anyway).
# --security-opt no-new-privileges   no privilege gain through setuid binaries.
# --read-only                   root filesystem is immutable; only the tmpfs below is writable.
# --tmpfs /tmp:...              the one writable place (pytest tmp dirs, seeded DB, screenshots, Chromium profile);
#                               size-limited, no setuid/devices, not executable.
# --memory, --memory-swap       caps a runaway browser; swap equal to memory means no swap.
# --pids-limit                  caps processes/threads (Chromium forks many).
# --shm-size                    /dev/shm for Chromium's renderer; the 64m default is tight for Chromium.
exec podman run --rm \
    --network none \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,size=512m,nosuid,nodev,noexec \
    --memory 2g --memory-swap 2g \
    --pids-limit 512 \
    --shm-size 256m \
    "${mounts[@]}" \
    "$built" "${cmd[@]}" "$@"
