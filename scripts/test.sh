#!/usr/bin/env bash
# Builds the test image (runtime + dev deps + tests) and runs the suite in a throwaway container. Extra args go to
# pytest. No volumes are mounted and the image contains no data/ or backups. The network stays on: the ICMP tests
# open real sockets.
#   scripts/test.sh                      whole suite
#   scripts/test.sh -k mtr -x            pytest arguments
#   scripts/test.sh --command <cmd...>   run this command in the container instead of pytest
# PENGUMON_JUNIT_DIR=<dir> (CI): pytest also writes <dir>/unit.xml (JUnit, with file and line per case), which
# scripts/junit-annotations.py turns into annotations. The directory must be writable by the container's user.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="localhost/pengumon-test:latest"

cmd=(python -m pytest -q -p no:cacheprovider)
mounts=()
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
elif [ -n "${PENGUMON_JUNIT_DIR:-}" ]; then
    mounts=(-v "$PENGUMON_JUNIT_DIR:/junit:rw")
    cmd+=(--junitxml=/junit/unit.xml -o junit_family=xunit1)
fi

# Runs the image this build produced, by its id: the tag is shared by every work tree, so two runs at once could
# otherwise test each other's code.
iid="$(mktemp)"
podman build -q --iidfile "$iid" -f "$ROOT/Dockerfile.test" -t "$IMAGE" "$ROOT" >/dev/null
built="$(cat "$iid")"; rm -f "$iid"
exec podman run --rm "${mounts[@]}" "$built" "${cmd[@]}" "$@"
