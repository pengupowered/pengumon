#!/usr/bin/env bash
# Exits 0 when the pengumon package inside IMAGE is byte-identical to src/pengumon of this tree (every file except
# bytecode), 1 otherwise. scripts/deploy.sh and scripts/test-container.sh run it after a build, so a stale build cache
# can never ship or test another tree's code.
#   scripts/image-matches-tree.sh IMAGE
set -euo pipefail
IMAGE="$1"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CT="${CONTAINER_CLI:-podman}"
list='find pengumon -type f ! -name "*.pyc" ! -path "*__pycache__*" | LC_ALL=C sort | xargs sha256sum'
tree="$(cd "$ROOT/src" && sh -c "$list")"
image="$("$CT" run --rm --entrypoint sh "$IMAGE" -c \
    "cd /usr/local/lib/python3.*/site-packages && $list")"
if [ "$tree" = "$image" ]; then
    echo "the image's package matches src/pengumon ($(printf '%s\n' "$tree" | wc -l) files)"
    exit 0
fi
echo "the image's package differs from src/pengumon of this tree:" >&2
diff <(printf '%s\n' "$tree") <(printf '%s\n' "$image") | head -20 >&2 || true
exit 1
