#!/usr/bin/env bash
# Prints the id of the successful push run of ci.yml for exactly one commit, or fails with an error annotation.
# release.yml uses it (job "tested") so a release publishes the files a CI run already built and tested instead of
# testing the commit again. GH_TOKEN is optional (a public repository answers without one, rate-limited).
#
#   scripts/find-tested-run.sh <owner/repo> <commit sha>
set -euo pipefail
repo="${1:?usage: find-tested-run.sh <owner/repo> <sha>}"
sha="${2:?usage: find-tested-run.sh <owner/repo> <sha>}"
case "$sha" in *[!0-9a-f]* | "") echo "::error::not a commit sha: $sha" >&2; exit 2 ;; esac

auth=()
[ -z "${GH_TOKEN:-}" ] || auth=(-H "Authorization: Bearer $GH_TOKEN")
url="${GITHUB_API_URL:-https://api.github.com}/repos/$repo/actions/runs?head_sha=$sha&status=success&event=push&per_page=100"
response="$(curl -fsS --retry 3 -H "Accept: application/vnd.github+json" "${auth[@]}" "$url")"

# Several successful runs (a re-run keeps its id; two pushes of the same commit are two runs): the latest one.
# `path` is ".github/workflows/ci.yml", with an "@ref" suffix on some events, hence the prefix match.
run_id="$(jq -r '[.workflow_runs[]
                  | select(.head_sha == "'"$sha"'" and .event == "push" and .conclusion == "success"
                           and (.path | startswith(".github/workflows/ci.yml")))]
                 | sort_by(.created_at) | last | .id // empty' <<<"$response")"

if [ -z "$run_id" ]; then
  echo "::error title=Tested commit::no successful CI run for $sha: push it to a ci/ branch first" >&2
  echo "no successful CI run for $sha: push it to a ci/ branch first" >&2
  exit 1
fi
echo "CI run $run_id passed for $sha" >&2
echo "$run_id"
