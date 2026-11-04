#!/usr/bin/env bash
# Tests the Pengumon image the way it is run: read-only root, unprivileged user, a data volume, a config mounted in.
# Podman by default (CONTAINER=docker works too); the same script runs locally and in CI (.github/workflows/ci.yml).
#
#   scripts/test-container.sh                   build the image from source (what scripts/deploy.sh does), test it
#   scripts/test-container.sh --prebuilt        build it from the wheel in dist/ (WHEEL_STAGE=wheel-prebuilt, as CI does)
#   scripts/test-container.sh --image NAME      test an image that already exists
#   scripts/test-container.sh --no-e2e          skip the browser check (needs the localhost/ui-test-runner:1 image)
#
# Checks: uid 10001; root filesystem read-only (/app not writable, /app/data and /tmp are); /api/health reports the
# version in src/pengumon/__init__.py; `pengumon version` and `pengumon paths` show the image's paths and address;
# an ICMP probe to 127.0.0.1 and an ICMPv6 one to ::1 work; `pengumon doctor` exits 0 on the raw socket; no ping/traceroute is installed;
# then the browser end to end check (tests/ui/container-e2e.mjs). Everything it creates (image if it built one,
# container, network, volume, temp files) is removed at the end. It never touches localhost/pengumon:latest or a
# container named pengumon.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CT="${CONTAINER:-podman}"
RUNNER="${UI_RUNNER_BASE:-localhost/ui-test-runner:1}"
ID="pgctest-$$"
IMAGE=""; BUILD_ARGS=(); E2E=1; BUILT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift ;;
        --prebuilt) BUILD_ARGS=(--build-arg WHEEL_STAGE=wheel-prebuilt) ;;
        --no-e2e) E2E=0 ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

TMP="$(mktemp -d)"
FAILED=0
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
pass() { printf '  ok    %s\n' "$*"; }
# annotate TITLE TEXT: in GitHub Actions, a failure annotation (the job log is not readable without a login, annotations
# are). The message is the last lines of TEXT, newlines as %0A, at most 1000 characters; its first line is the error.
annotate() {
    [ "${GITHUB_ACTIONS:-}" = true ] || return 0
    local title="$1" text="$2" msg
    msg="$(printf "%s\n" "$text" | sed "s/\x1b\[[0-9;]*m//g" | grep -v "^[[:space:]]*\$" | tail -n 8)"
    [ -n "$msg" ] || msg="(no output)"
    while [ "${#msg}" -gt 700 ] && [[ "$msg" == *$'\n'* ]]; do msg="${msg#*$'\n'}"; done   # drop the oldest lines first
    [ "${#msg}" -le 700 ] || msg="${msg: -700}"
    # an "ERROR: ..." line (the e2e script prints one) goes first, so the first line is the actual error
    local err; err="$(printf "%s\n" "$text" | sed -n "s/^ERROR: //p" | head -n 1)"
    err="${err:0:290}"
    [ -z "$err" ] || msg="$err"$'\n'"$msg"
    msg="${msg//'%'/%25}"; msg="${msg//$'\r'/%0D}"; msg="${msg//$'\n'/%0A}"; msg="${msg:0:1000}"
    title="${title//[:,]/ }"
    printf '::error title=%s::%s\n' "$title" "$msg"
}
# bad WHAT [DETAIL]: DETAIL (default WHAT) is what the annotation says; put the actual error first.
bad() { printf '\033[31m  FAIL  %s\033[0m\n' "$1"; FAILED=$((FAILED + 1)); annotate "$1" "${2:-$1}"; }
check() {
    local what="$1" out rc; shift
    out="$("$@" 2>&1)"; rc=$?
    if [ $rc = 0 ]; then pass "$what"; else bad "$what" "${out:-command exited $rc: $*}"; fi
}
# wait_for SECONDS COMMAND...: run COMMAND every half second until it succeeds or the time is up.
wait_for() {
    local end=$((SECONDS + $1)); shift
    until "$@" >/dev/null 2>&1; do [ $SECONDS -lt $end ] || return 1; sleep 0.5; done
}

cleanup() {
    # One at a time: podman 4.9's `rm -f A B` removes nothing when B is already gone (and still exits 0), which left
    # the container, so its network stayed too. -v: the fresh container's anonymous /app/data volume goes with it.
    "$CT" rm -f -v "$ID" >/dev/null 2>&1
    "$CT" rm -f -v "$ID-fresh" >/dev/null 2>&1
    "$CT" network rm "$ID" >/dev/null 2>&1
    "$CT" volume rm -f "$ID" >/dev/null 2>&1
    [ $BUILT = 1 ] && "$CT" rmi -f "$IMAGE" >/dev/null 2>&1
    rm -rf "$TMP"
}
trap cleanup EXIT
# A run that was killed outright (SIGKILL: the trap above never runs) leaves its network and volume. Their names
# carry the run's process id, so remove those of runs whose process is gone; a run still going is never touched.
sweep_stale() {
    local name pid
    for name in $( { "$CT" ps -a --format '{{.Names}}'; "$CT" network ls --format '{{.Name}}'; \
                     "$CT" volume ls --format '{{.Name}}'; } 2>/dev/null | grep -E '^pgctest-[0-9]+(-fresh)?$' | sort -u); do
        pid="${name#pgctest-}"; pid="${pid%-fresh}"
        kill -0 "$pid" 2>/dev/null && continue
        "$CT" rm -f -v "$name" >/dev/null 2>&1
        "$CT" network rm "$name" >/dev/null 2>&1
        "$CT" volume rm -f "$name" >/dev/null 2>&1
    done
}
sweep_stale
# A backstop for a killed run: the trap above never runs on SIGKILL, and a killed `podman run` leaves its container
# running. podman then stops each container itself after this long (PENGUMON_TEST_TIMEOUT, default 900 s).
backstop=()
[ "$(basename "$CT")" = podman ] && backstop=(--timeout "${PENGUMON_TEST_TIMEOUT:-900}")

VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/src/pengumon/__init__.py")"
[ -n "$VERSION" ] || { echo "no __version__ in src/pengumon/__init__.py" >&2; exit 2; }

if [ -z "$IMAGE" ]; then
    IMAGE="localhost/pengumon-ctest:$$"
    step "Building $IMAGE ${BUILD_ARGS[*]:-(from source)}"
    "$CT" build "${BUILD_ARGS[@]}" -t "$IMAGE" "$ROOT" >"$TMP/build.log" 2>&1 || { tail -30 "$TMP/build.log"; echo "build failed" >&2; exit 1; }
    BUILT=1
fi

# A config with targets that answer (loopback) and nothing else. Readable by uid 10001 through the mount.
cat >"$TMP/config.yaml" <<'YAML'
prober: {interval_seconds: 1.0}
targets:
  - {id: t_local, name: "Local", category: local, address: "127.0.0.1", enabled: true}
  - {id: t_gateway, name: "Gateway", category: gateway, address: "127.0.0.1", enabled: true}
  - {id: t_public, name: "Public", category: public, address: "127.0.0.1", enabled: true}
YAML
chmod 644 "$TMP/config.yaml"

step "Starting $IMAGE (read-only root, tmpfs /tmp, fresh data volume)"
"$CT" network create "$ID" >/dev/null || exit 1
"$CT" volume create "$ID" >/dev/null || exit 1
# Same flags as the compose files, except the network (a private one, so the browser container can reach it by name).
"$CT" run -d "${backstop[@]}" --name "$ID" --network "$ID" --network-alias pengumon-under-test --cap-add NET_RAW \
    --read-only --tmpfs /tmp -v "$ID:/app/data" -v "$TMP/config.yaml:/app/config.yaml:ro" "$IMAGE" >/dev/null \
    || { echo "could not start the container" >&2; exit 1; }
ex() { "$CT" exec "$ID" "$@"; }

step "Waiting for /api/health"
health=""
healthy() { health="$(ex curl -s -m 2 http://127.0.0.1:8000/api/health || true)"; case "$health" in *'"ok"'*) return 0 ;; esac; return 1; }
end=$((SECONDS + 60))
until healthy; do
    [ "$("$CT" inspect -f '{{.State.Running}}' "$ID" 2>/dev/null)" = true ] || break   # it died: stop waiting
    [ $SECONDS -lt $end ] || break
    sleep 0.5
done
echo "  health: $health"
healthy || bad "the app answers /api/health within 60 s" \
    "no \"ok\" from /api/health (last: ${health:-nothing}); container log: $("$CT" logs --tail 8 "$ID" 2>&1 | grep -v uvicorn.access)"

step "Checks"
[ "$(ex id -u)" = 10001 ] && pass "runs as uid 10001" || bad "runs as uid 10001 (got $(ex id -u 2>&1))"
check "root filesystem is read-only: a write to /app/x fails" bash -c "! $CT exec $ID touch /app/x"
check "root filesystem is read-only: a write to /usr/local/x fails" bash -c "! $CT exec $ID touch /usr/local/x"
check "a write to /app/data works" ex touch /app/data/x
check "a write to /tmp works" ex touch /tmp/x
case "$health" in *"\"version\":\"$VERSION\""*) pass "/api/health reports version $VERSION" ;; *) bad "/api/health reports version $VERSION" ;; esac
v="$(ex pengumon version 2>&1)"
case "$v" in *"$VERSION"*) pass "pengumon version prints $VERSION" ;; *) bad "pengumon version prints $VERSION (got: $v)" ;; esac
p="$(ex pengumon paths 2>&1)"
for want in /app/config.yaml /app/data /app/data/pengumon.db 0.0.0.0; do
    case "$p" in *"$want"*) pass "pengumon paths shows $want" ;; *) bad "pengumon paths shows $want" ;; esac
done
check "the data volume holds the database the app created" ex test -s /app/data/pengumon.db
check "an ICMP probe to 127.0.0.1 works (the app's backend, as the prober uses it)" \
    ex python -c "import asyncio, sys; from pengumon.engine.icmp_select import open_backend; b = open_backend(); r = asyncio.run(b.probe('127.0.0.1', timeout=2)); print(b.name, r); sys.exit(0 if r.kind == 'reply' else 1)"
check "an ICMPv6 probe to ::1 works on the raw socket (the IPv6 half of the app's backend)" \
    ex python -c "import asyncio, sys; from pengumon.engine.icmp_select import open_backend; b = open_backend(); r = asyncio.run(b.probe('::1', timeout=2)); print(b.ipv6_name, r); sys.exit(0 if r.kind == 'reply' and b.ipv6_name == 'linux-raw' else 1)"
doctor="$(ex pengumon doctor 2>&1)"; doctor_rc=$?
if [ $doctor_rc = 0 ]; then pass "pengumon doctor exits 0"; else bad "pengumon doctor exits 0 (got $doctor_rc): $doctor"; fi
case "$doctor" in *"linux-raw"*) pass "the image uses the raw socket (CAP_NET_RAW on the interpreter)" ;;
    *) bad "the image uses the raw socket (doctor: $doctor)" ;; esac
if [ "$BUILT" = 1 ]; then
    check "the image holds this tree's code (no stale build cache)" env CONTAINER_CLI="$CT" "$ROOT/scripts/image-matches-tree.sh" "$IMAGE"
fi
check "no ping or traceroute program in the image" bash -c \
    "! $CT exec $ID sh -c 'command -v ping || command -v traceroute || command -v tracepath'"
check "the app's own prober sees its targets alive" wait_for 30 bash -c \
    "$CT exec $ID curl -s -m 3 http://127.0.0.1:8000/api/health | grep -q '\"status\":\"ok\"'"
check "a versioned static asset is served (/static/v/<hash>/)" bash -c \
    "h=\$($CT exec $ID curl -s -m 3 http://127.0.0.1:8000/ | grep -o '/static/v/[0-9a-f]*/' | head -1); [ -n \"\$h\" ] && $CT exec $ID curl -sf -m 3 -o /dev/null \"http://127.0.0.1:8000\${h}js/util.js\""
stray="$("$CT" diff "$ID" | grep -Ev '^. /(etc|tmp|app/data)(/|$)|^. /app$')"
if [ -z "$stray" ]; then pass "nothing was written outside /app/data and /tmp"; else bad "files written outside /app/data and /tmp: $stray"; fi

# A fresh install with the config the image ships (config.example.yaml) or an empty one has no path of its own:
# revision 0, so the dashboard's first run discovers it.
step "Fresh container, no config mounted (the image's own config.yaml): first run"
ID2="$ID-fresh"
"$CT" run -d "${backstop[@]}" --name "$ID2" --cap-add NET_RAW --read-only --tmpfs /tmp "$IMAGE" >/dev/null || bad "could not start the fresh container"
rev=""
for _ in $(seq 1 60); do
    rev="$("$CT" exec "$ID2" curl -s -m 2 http://127.0.0.1:8000/api/path 2>/dev/null | sed -n 's/.*"revision": *\([0-9]*\).*/\1/p')"
    [ -n "$rev" ] && break
    sleep 1
done
if [ "$rev" = 0 ]; then pass "a fresh container is at path revision 0 (first run)"; else bad "a fresh container is at path revision 0 (got: '${rev}')"; fi
"$CT" rm -f -v "$ID2" >/dev/null 2>&1

if [ $E2E = 1 ]; then
    step "Browser end to end ($RUNNER -> http://pengumon-under-test:8000)"
    if ! "$CT" image exists "$RUNNER"; then
        bad "browser check: image $RUNNER is missing (tests/ui/runner/build.sh builds it; --no-e2e skips the check)"
    else
        # The runner starts only now: the app is healthy and every other container is attached. The script itself then waits
        # for the runner's own network to settle before it starts Chromium (see container-e2e.mjs).
        e2e_out="$("$CT" run --rm "${backstop[@]}" --network "$ID" --cap-drop=ALL --security-opt no-new-privileges --read-only \
            --tmpfs /tmp:rw,size=512m,nosuid,nodev --memory 2g --pids-limit 512 --shm-size 256m \
            -v "$ROOT/tests/ui:/e2e:ro" -e PENGUMON_BROWSER_ARGS="--no-sandbox" -e GITHUB_ACTIONS="${GITHUB_ACTIONS:-}" "$RUNNER" \
            node /e2e/container-e2e.mjs http://pengumon-under-test:8000 2>&1)"; e2e_rc=$?
        printf '%s\n' "$e2e_out"
        if [ $e2e_rc = 0 ]; then pass "browser end to end"; else bad "browser end to end" "${e2e_out:-exit code $e2e_rc, no output}"; fi
    fi
fi
if [ $FAILED = 0 ]; then printf '\033[32mAll container checks passed.\033[0m\n'; else
    printf '\033[31m%d check(s) failed.\033[0m\n--- container log (tail) ---\n' "$FAILED"
    logtail="$("$CT" logs --tail 30 "$ID" 2>&1 | grep -v "uvicorn.access")"
    printf '%s\n' "$logtail"
    # one warning annotation with the app's log, next to the error annotations of the failed checks
    annotate "container log (tail)" "$logtail" | sed 's/^::error /::warning /'
fi
exit $((FAILED > 0))
