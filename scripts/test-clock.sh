#!/usr/bin/env bash
# Runs the unit suite once per chosen wall-clock instant (libfaketime in the test image: scripts/test.sh with
# PENGUMON_FAKE_EPOCH and PENGUMON_TEST_TZ) and prints one line per instant: ok, or FAIL with the failing test ids.
# Why: a test that only fails in some seconds of the day (a window edge, midnight, a quarter hour, a DST change) passes on
# every ordinary run and then fails in CI. docs/testing.md ("Tests at other clock times").
#   scripts/test-clock.sh                    every instant below, one after another
#   scripts/test-clock.sh quarter midnight   only the instants whose name contains one of these words
#   scripts/test-clock.sh --ui [words]       the browser suite (scripts/test-ui.sh) instead: three variants, one at a time, at the instants marked UI_INSTANTS
#                                            below (or the named ones); about 8 minutes each; pytest arguments after -- (-k full)
#   scripts/test-clock.sh --list             print the instants and exit
#   scripts/test-clock.sh -- -k uptime       what follows -- goes to pytest (scripts/test.sh)
# PENGUMON_CLOCK_LOGS=<dir>  where the full log of each instant goes (default: a new directory under ${TMPDIR:-/tmp}).
# A speed below 1 would stretch the interesting seconds, but the suite's own timing tests then fail (they expect time to
# pass): every instant runs at speed 1. A window of a few seconds (a quarter hour) is therefore only hit by the tests that
# run in the first minute; to try one test file at such a window, name it after -- (docs/testing.md).
# Each instant is a whole suite run (about 1-2 minutes with 4 workers); run it when nothing else is loading the machine.
# The instants are fixed dates near today (autumn 2026 - spring 2027): the suite builds its stored data relative to "now",
# so any date works, and dates near the real one keep that data realistic. Time starts at the instant and runs on, so a
# run that starts 5 s before midnight crosses it while the suite runs.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# name | UTC time | TZ of the container | clock speed (1: real time) | why
INSTANTS=(
"quarter-hour|2026-11-16 09:14:55|UTC|1|5 s before a quarter hour: whole-quarter-hour range ends (7c88427)"
"quarter-hour-ny|2026-11-16 09:14:55|America/New_York|1|the same with a zone whose local clock is not UTC"
"midnight-utc-before|2026-11-15 23:59:55|UTC|1|5 s before UTC midnight (Sunday to Monday), also inside the 23:00-24:00 UTC hour"
"midnight-utc-after|2026-11-16 00:00:05|UTC|1|5 s after UTC midnight"
"midnight-ny-before|2026-11-16 04:59:55|America/New_York|1|5 s before local midnight in New York"
"midnight-ny-after|2026-11-16 05:00:05|America/New_York|1|5 s after local midnight in New York"
"hour23-month-end|2026-11-30 23:30:00|UTC|1|inside 23:00-24:00 UTC on the last day of a month"
"year-end|2026-12-31 23:59:55|UTC|1|5 s before the new year"
"fall-back-ny|2026-11-01 05:30:00|America/New_York|1|01:30 EDT on the US fall-back day"
"fall-back-ny-edge|2026-11-01 05:59:55|America/New_York|1|5 s before the clocks go back (02:00 EDT becomes 01:00 EST)"
"fall-back-eu|2026-10-25 01:30:00|Europe/Berlin|1|02:30 CET, the second 02:30 of the EU fall-back day"
"spring-forward-ny|2027-03-14 06:59:55|America/New_York|1|5 s before 02:00 EST jumps to 03:00 EDT"
)

# the browser suite is about 8 minutes per instant: by default the ones where a page, a server and a day edge meet
UI_INSTANTS=" midnight-ny-before midnight-utc-before fall-back-ny-edge "
runner="$ROOT/scripts/test.sh"
ui=0
if [ "${1:-}" = "--ui" ]; then ui=1; runner="$ROOT/scripts/test-ui.sh"; shift; fi

if [ "${1:-}" = "--list" ]; then
    for row in "${INSTANTS[@]}"; do IFS='|' read -r name utc tz speed why <<<"$row"; printf '%-22s %s UTC  TZ=%-17s x%-4s %s\n' "$name" "$utc" "$tz" "$speed" "$why"; done
    exit 0
fi
words=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do words+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
# a worker killed for memory (exit 137, "worker crashed") showed up twice in a dozen runs at the default 2g; the clock is not the cause
# The browser suite: one run at a time and three variants unless pytest arguments say otherwise. All 23 variants with 4 at once
# lost their page loads (30 s navigation timeouts) on a machine under load, where the same suite without the clock passed:
# libfaketime makes Chromium slower.
if [ $ui = 1 ]; then
    export PENGUMON_UI_WORKERS="${PENGUMON_UI_WORKERS:-1}"
    [ $# -gt 0 ] || set -- -k "bare or full-1of4 or longhistory-1of3"
fi
[ $ui = 1 ] || export PENGUMON_TEST_MEMORY="${PENGUMON_TEST_MEMORY:-3g}"
logs="${PENGUMON_CLOCK_LOGS:-$(mktemp -d "${TMPDIR:-/tmp}/pengumon-clock.XXXXXX")}"
mkdir -p "$logs"
echo "logs: $logs"

failed=0
ran=0
for row in "${INSTANTS[@]}"; do
    IFS='|' read -r name utc tz speed why <<<"$row"
    if [ $ui = 1 ] && [ ${#words[@]} = 0 ]; then
        case "$UI_INSTANTS" in *" $name "*) ;; *) continue;; esac
    fi
    if [ ${#words[@]} -gt 0 ]; then
        match=0; for w in "${words[@]}"; do case "$name" in *"$w"*) match=1;; esac; done
        [ $match = 1 ] || continue
    fi
    ran=$((ran + 1))
    epoch="$(date -u -d "$utc" +%s)"
    log="$logs/$name.log"
    started=$SECONDS
    # the time as a clock on the wall of that zone shows it (libfaketime reads it in the container's TZ)
    local_time="$(TZ="$tz" date -d "@$epoch" '+%Y-%m-%d %H:%M:%S')"
    PENGUMON_FAKE_EPOCH="$epoch" PENGUMON_FAKE_TIME="$local_time" PENGUMON_FAKE_SPEED="$speed" PENGUMON_TEST_TZ="$tz" "$runner" -p no:warnings -o addopts="" -rfE "$@" >"$log" 2>&1
    rc=$?
    took=$((SECONDS - started))
    label="$(printf '%-22s %s UTC TZ=%-17s' "$name" "$utc" "$tz")"
    if [ $rc = 0 ]; then
        echo "ok    $label ($took s) $(grep -E '^[0-9]+ passed' "$log" | tail -1)"
    else
        failed=$((failed + 1))
        echo "FAIL  $label ($took s, exit $rc) $(grep -E ' (passed|failed)' "$log" | tail -1)"
        grep -E '^(FAILED|ERROR) ' "$log" | sed -E 's/ - .*//; s/^/        /' | sort -u
        [ -n "$(grep -E '^(FAILED|ERROR) ' "$log")" ] || tail -5 "$log" | sed 's/^/        /'
    fi
done
echo "$((ran - failed)) of $ran instants passed; logs in $logs"
[ $failed = 0 ] && [ $ran -gt 0 ]
