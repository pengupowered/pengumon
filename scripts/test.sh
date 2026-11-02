#!/usr/bin/env bash
# Builds the test image (runtime + dev deps + tests) and runs the suite in a throwaway container. Extra args go to
# pytest. No volumes are mounted and the image contains no data/ or backups. The network stays on: the ICMP tests
# open real sockets.
#   scripts/test.sh                      whole suite
#   scripts/test.sh -k mtr -x            pytest arguments
#   scripts/test.sh --command <cmd...>   run this command in the container instead of pytest
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="localhost/pengumon-test:latest"

cmd=(python -m pytest -q -p no:cacheprovider)
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
fi

# Runs the image this build produced, by its id: the tag is shared by every work tree, so two runs at once could
# otherwise test each other's code.
iid="$(mktemp)"
podman build -q --iidfile "$iid" -f "$ROOT/Dockerfile.test" -t "$IMAGE" "$ROOT" >/dev/null
built="$(cat "$iid")"; rm -f "$iid"
exec podman run --rm "$built" "${cmd[@]}" "$@"
