#!/usr/bin/env bash
# Prints the body of one version's section of CHANGELOG.md (used by .github/workflows/release.yml for the release notes).
#   scripts/changelog-section.sh 0.14.2
# Fails if the version has no section or the section is empty.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="${1:?usage: changelog-section.sh <version>}"
notes="$(awk -v v="$version" '
    /^## \[/ { if (on) exit; on = (index($0, "## [" v "]") == 1); next }
    on && /^\[[^]]+\]: / { next }
    on { print }
' "$ROOT/CHANGELOG.md" | sed '/./,$!d')"
[ -n "$notes" ] || { echo "CHANGELOG.md has no notes for version $version" >&2; exit 1; }
printf '%s\n' "$notes"
