#!/usr/bin/env bash
# Builds the browser test base image (localhost/ui-test-runner:1 unless UI_RUNNER_IMAGE says otherwise). The only step
# with network access: apt and the npm registry, through the hash-pinned lockfile. Podman by default; CONTAINER=docker
# works too.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${UI_RUNNER_IMAGE:-localhost/ui-test-runner:1}"
"${CONTAINER:-podman}" build -f "$DIR/Dockerfile" -t "$IMAGE" "$DIR"
echo "built $IMAGE"
