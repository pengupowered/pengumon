#!/usr/bin/env bash
# What the CI job "install" checks around the install scripts (.github/workflows/ci.yml); runs in bash on Linux,
# macOS and Windows (Git Bash). RUNNER_OS says which.
#   scripts/ci-install-check.sh installed <assets-dir>    after the first install: running, answering, who it runs as
#   scripts/ci-install-check.sh upgraded <assets-dir>     after running the installer again: backup made, answering
#   scripts/ci-install-check.sh removed                   after the uninstall: nothing answers, nothing is left
#   scripts/ci-install-check.sh diagnose                  on failure: service state and log tail as one error annotation
set -uo pipefail
phase="${1:?usage: ci-install-check.sh installed|upgraded|removed|diagnose [assets-dir]}"
assets="${2:-}"
port=8000
fails=0

case "${RUNNER_OS:-$(uname -s)}" in
    Linux)   pg=/opt/pengumon/venv/bin/pengumon; data=/var/lib/pengumon; sudo=sudo ;;
    macOS|Darwin) pg=/usr/local/pengumon/venv/bin/pengumon; data="/Library/Application Support/Pengumon"; sudo=sudo ;;
    *)       local_app="$(cygpath -u "$LOCALAPPDATA")"; pg="$local_app/Programs/Pengumon/venv/Scripts/pengumon.exe"; data="$local_app/Pengumon"; sudo= ;;
esac
os="${RUNNER_OS:-$(uname -s)}"

ok() { echo "ok: $*"; }
# A failed check is also an error annotation (the annotation is what can be read without signing in to GitHub).
# `bad WHAT [OUTPUT]`: the output's last line, which for a Python traceback is the exception, comes first.
bad() {
    echo "FAILED: $1" >&2; fails=$((fails + 1))
    local detail="${2:-}" last
    if [ -n "$detail" ]; then
        last="$(printf '%s\n' "$detail" | grep -v '^[[:space:]]*$' | tail -n 1)"
        echo "$detail" | tail -n 40 >&2
        annotate "install check (${RUNNER_OS:-$(uname -s)})" "$1: $last"$'\n'"$(printf '%s\n' "$detail" | tail -n 25)"
    else
        annotate "install check (${RUNNER_OS:-$(uname -s)})" "$1"
    fi
}
check() {
    local what="$1" out; shift
    if out="$("$@" 2>&1)"; then ok "$what"; else bad "$what" "$out"; fi
}

version_of_assets() { basename "$assets"/pengumon-*-py3-none-any.whl | sed -n 's/^pengumon-\([0-9][^-]*\)-.*/\1/p'; }
health() { curl -sf -m 5 "http://127.0.0.1:$port/api/health"; }
pid_of_service() { pgrep -f 'pengumon(w)?(\.exe)? run' | head -n 1; }

annotate() {  # annotate TITLE TEXT: one GitHub error annotation (newlines and percent signs escaped)
    local text="${2//'%'/'%25'}"; text="${text//$'\r'/}"; text="${text//$'\n'/'%0A'}"
    echo "::error title=$1::${text:0:3500}"
}

case "$phase" in
installed|upgraded)
    want="$(version_of_assets)"
    body="$(health)" && ok "/api/health answers: $body" || bad "/api/health does not answer"
    case "$body" in *"\"version\":\"$want\""*) ok "it is version $want" ;; *) bad "health does not report version $want: $body" ;; esac
    check "pengumon service status exits 0" "$pg" service status
    case "$os" in
    Linux)
        check "systemd says the unit is active" systemctl is-active --quiet pengumon.service
        user="$(ps -o user= -p "$(systemctl show -p MainPID --value pengumon.service)" | tr -d ' ')"
        [ -n "$user" ] && [ "$user" != root ] && ok "it runs as $user, not root" || bad "it runs as '$user'"
        score_line="$(systemd-analyze security pengumon.service --no-pager 2>&1 | grep 'Overall exposure level')"
        echo "$score_line"
        score="$(echo "$score_line" | sed -n 's/.*: \([0-9.]*\) .*/\1/p')"
        awk -v s="${score:-99}" 'BEGIN { exit !(s <= 2.0) }' && ok "exposure score $score is at most 2.0" || bad "exposure score '$score' is above 2.0"
        check "data directory has the database" $sudo test -f "$data/pengumon.db"
        ;;
    macOS|Darwin)
        user="$(ps -o user= -p "$(pid_of_service)" | tr -d ' ')"
        [ "$user" = _pengumon ] && ok "it runs as _pengumon" || bad "it runs as '$user'"
        check "data directory has the database" $sudo test -f "$data/pengumon.db"
        ;;
    *)
        check "the task exists" schtasks //Query //TN Pengumon
        check "data directory has the database" test -f "$data/pengumon.db"
        ;;
    esac
    if [ "$phase" = upgraded ]; then
        count="$($sudo ls "$data/backups" 2> /dev/null | wc -l | tr -d ' ')"
        [ "${count:-0}" -ge 1 ] && ok "the upgrade made a backup ($count)" || bad "no backup in $data/backups"
    fi
    ;;
removed)
    ! health > /dev/null 2>&1 && ok "nothing answers on port $port" || bad "something still answers on port $port"
    [ ! -e "$pg" ] && ok "the program is gone" || bad "$pg is still there"
    case "$os" in
    Linux) [ ! -e /etc/systemd/system/pengumon.service ] && ok "the unit is gone" || bad "the unit file is still there" ;;
    macOS|Darwin) [ ! -e /Library/LaunchDaemons/com.pengupowered.pengumon.plist ] && ok "the plist is gone" || bad "the plist is still there" ;;
    *) ! schtasks //Query //TN Pengumon > /dev/null 2>&1 && ok "the task is gone" || bad "the task is still there" ;;
    esac
    # /var/lib/pengumon is a link into /var/lib/private, which only root can open
    if $sudo test -d "$data"; then ok "the data is kept"; else bad "the data directory was removed"; fi
    ;;
diagnose)
    {
        echo "== service status"
        if [ -e "$pg" ]; then "$pg" service status 2>&1 | tail -n 40; else echo "$pg does not exist"; fi
        case "$os" in
        Linux) echo "== journalctl"; sudo journalctl -u pengumon.service --no-pager -n 30 2>&1 ;;
        macOS|Darwin) echo "== log"; $sudo tail -n 30 "$data/pengumon.log" 2>&1; $sudo launchctl print system/com.pengupowered.pengumon 2>&1 | head -n 25 ;;
        *) echo "== log"; tail -n 30 "$data/pengumon.log" 2>&1; schtasks //Query //TN Pengumon //V //FO LIST 2>&1 | head -n 25 ;;
        esac
    } > "${RUNNER_TEMP:-/tmp}/install-diagnose.txt" 2>&1
    cat "${RUNNER_TEMP:-/tmp}/install-diagnose.txt"
    # the last exception line of any traceback first, then the log
    headline="$(grep -E '^[A-Za-z_.]+(Error|Exception|Exit)\b' "${RUNNER_TEMP:-/tmp}/install-diagnose.txt" | tail -n 1)"
    annotate "install ($os)" "${headline:+$headline$'\n'}$(cat "${RUNNER_TEMP:-/tmp}/install-diagnose.txt")"
    exit 0
    ;;
*) echo "unknown phase $phase" >&2; exit 2 ;;
esac
exit $((fails > 0))
