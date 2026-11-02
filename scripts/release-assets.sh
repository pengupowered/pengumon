#!/usr/bin/env bash
# Assembles what a release attaches, from the wheel and sdist that CI built:
#   pengumon-X.Y.Z-py3-none-any.whl, pengumon-X.Y.Z.tar.gz   the package
#   pengumon-X.Y.Z.lock        requirements.txt of that commit: the universal, hash-locked dependencies
#   install.sh, install.ps1    the install scripts of that commit
#   SHA256SUMS                 sha256 of every file above
# CI builds this once (job "release-assets"), tests the install scripts with it, and the release job attaches the
# same files. The install scripts verify the wheel and the lock against SHA256SUMS before they install anything.
#
#   scripts/release-assets.sh <dist-dir> <out-dir>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dist="${1:?usage: release-assets.sh <dist-dir> <out-dir>}"
out="${2:?usage: release-assets.sh <dist-dir> <out-dir>}"

shopt -s nullglob
wheels=("$dist"/pengumon-*.whl)
[ "${#wheels[@]}" -eq 1 ] || { echo "release-assets: expected exactly one pengumon-*.whl in $dist, found ${#wheels[@]}" >&2; exit 1; }
version="$(basename "${wheels[0]}" | sed -n 's/^pengumon-\([0-9][^-]*\)-.*\.whl$/\1/p')"
[ -n "$version" ] || { echo "release-assets: cannot read the version from ${wheels[0]}" >&2; exit 1; }
[ -f "$dist/pengumon-$version.tar.gz" ] || { echo "release-assets: $dist/pengumon-$version.tar.gz is missing" >&2; exit 1; }

mkdir -p "$out"
rm -f "$out"/pengumon-* "$out"/install.sh "$out"/install.ps1 "$out"/SHA256SUMS
cp "${wheels[0]}" "$dist/pengumon-$version.tar.gz" "$out/"
cp "$ROOT/requirements.txt" "$out/pengumon-$version.lock"
cp "$ROOT/scripts/install.sh" "$ROOT/scripts/install.ps1" "$out/"
chmod 755 "$out/install.sh"
(cd "$out" && sha256sum pengumon-"$version"-*.whl pengumon-"$version".tar.gz pengumon-"$version".lock install.sh install.ps1 > SHA256SUMS)
echo "release-assets: $version"
cat "$out/SHA256SUMS"
