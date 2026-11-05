#!/usr/bin/env bash
# Builds and (re)starts Pengumon on a Linux host with rootless Podman (docker-compose.yml).
#
#   scripts/deploy.sh              gate, backup, build, recreate, wait until healthy
#   scripts/deploy.sh --no-build   same, reusing localhost/pengumon:latest
#   scripts/deploy.sh --gate-only  run only the dependency gate (exit 0 if it passed or was skipped)
#   scripts/deploy.sh --dry-run    run the gate, then print the steps instead of running them
#
# The dependency gate runs first, in every mode, and has no skip switch: `depguard.sh check` (are the
# requirements*.txt locks current?) and `depguard.sh audit` (advisories, cooldown, yanked releases) from
# $DEPGUARD_HOME (default ~/src/depguard; depguard is an optional external tool, see docs/supply-chain.md).
# When depguard is installed, a failing check or audit refuses the deploy. When it is not installed the gate is
# skipped with a warning (the hash-locked requirements still apply); PENGUMON_REQUIRE_DEPGUARD=1 makes a missing
# depguard a failure instead. Exceptions are made only through .depguard-ignore.yaml (see the depguard README).
#
# Before replacing a running container it keeps one rollback point (older ones are removed):
#   * the current image, tagged localhost/pengumon:pre-deploy-<stamp>
#   * ~/backups/pengumon/<stamp>/ with a consistent copy of the database (VACUUM INTO) and secrets.json
# Rollback: podman tag localhost/pengumon:pre-deploy-<stamp> localhost/pengumon:latest, put the backup
# database back into the volume, then scripts/deploy.sh --no-build.
#
# Host setup this relies on (once): net.ipv4.ping_group_range covering the user (see docker-compose.yml),
# loginctl enable-linger, and systemctl --user enable podman-restart.service.
set -u

NAME="pengumon"
IMAGE="localhost/pengumon:latest"
PORT=8000
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPGUARD_HOME="${DEPGUARD_HOME:-$HOME/src/depguard}"
BACKUPS="${PENGUMON_BACKUPS:-$HOME/backups/pengumon}"
STAMP="$(date +%Y-%m-%d-%H%M)"

NO_BUILD=0; GATE_ONLY=0; DRY_RUN=0
for a in "$@"; do
    case "$a" in
        --no-build) NO_BUILD=1 ;;
        --gate-only) GATE_ONLY=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
run() {  # prints instead of running in a dry run
    if [ $DRY_RUN = 1 ]; then printf '\033[33mDRYRUN: %s\033[0m\n' "$*"; else "$@"; fi
}

cd "$ROOT" || exit 2

[ -f "$ROOT/config.yaml" ] || fail "config.yaml is missing: cp config.example.yaml config.yaml and edit it first (it is git-ignored)"

# ---- dependency gate ----
if [ ! -x "$DEPGUARD_HOME/depguard.sh" ]; then
    if [ "${PENGUMON_REQUIRE_DEPGUARD:-}" = 1 ]; then
        fail "Dependency gate unavailable: $DEPGUARD_HOME/depguard.sh not found (set DEPGUARD_HOME or clone depguard there); PENGUMON_REQUIRE_DEPGUARD=1 is set. Refusing to deploy."
    fi
    printf '\033[33mWARNING: dependency gate skipped: depguard not found at %s; the hash-locked requirements still apply.\033[0m\n' "$DEPGUARD_HOME" >&2
else
    for s in check audit; do
        step "Dependency gate: depguard $s"
        "$DEPGUARD_HOME/depguard.sh" "$s" "$ROOT"
        code=$?
        [ $code -eq 0 ] || fail "Dependency gate failed at 'depguard $s' (exit $code); deploy aborted. Fix the lock/advisory (re-lock with depguard.sh lock) or record a time-limited exception in $ROOT/.depguard-ignore.yaml."
    done
    printf '\033[32m==> Dependency gate passed\033[0m\n'
fi
[ $GATE_ONLY = 1 ] && exit 0

# ---- rollback point ----
running=0
podman container exists "$NAME" 2>/dev/null && [ "$(podman inspect -f '{{.State.Running}}' "$NAME")" = true ] && running=1
if podman image exists "$IMAGE" 2>/dev/null; then
    step "Tagging the current image as pre-deploy-$STAMP"
    run podman tag "$IMAGE" "localhost/pengumon:pre-deploy-$STAMP" || fail "tagging the rollback image failed"
fi
if [ $running = 1 ]; then
    step "Backing up the database and secrets to $BACKUPS/$STAMP"
    run mkdir -p "$BACKUPS/$STAMP"
    if podman exec "$NAME" pengumon version >/dev/null 2>&1; then
        # `pengumon backup` follows the configured database location and writes pengumon.db (VACUUM INTO),
        # secrets.json and sessions.json into a timestamped folder; copy its contents to the host folder.
        TMPBK="/tmp/pre-deploy-$STAMP"
        if [ $DRY_RUN = 1 ]; then
            printf '\033[33mDRYRUN: podman exec %s pengumon backup --to %s   (then podman cp the folder it prints to %s)\033[0m\n' "$NAME" "$TMPBK" "$BACKUPS/$STAMP"
        else
            folder="$(podman exec "$NAME" pengumon backup --to "$TMPBK" | tail -n 1)" && [ -n "$folder" ] \
                || fail "database backup failed; nothing was changed"
            podman cp "$NAME:$folder/." "$BACKUPS/$STAMP/" || fail "copying the backup out failed"
            podman exec "$NAME" rm -rf "$TMPBK"
        fi
    else
        # a container from before the pengumon command (images up to 0.15.0): the database sits at data/pengumon.db
        run podman exec "$NAME" python -c "import sqlite3; c = sqlite3.connect('data/pengumon.db'); c.execute(\"VACUUM INTO 'data/.pre-deploy.db'\"); c.close()" \
            || fail "database backup failed; nothing was changed"
        run podman cp "$NAME:/app/data/.pre-deploy.db" "$BACKUPS/$STAMP/pengumon.db" || fail "copying the backup out failed"
        run podman exec "$NAME" rm -f data/.pre-deploy.db
        if podman exec "$NAME" test -f data/secrets.json 2>/dev/null; then
            run podman cp "$NAME:/app/data/secrets.json" "$BACKUPS/$STAMP/secrets.json"
        fi
    fi
    run chmod -R go-rwx "$BACKUPS/$STAMP"
fi

# ---- the host's time zone, for the container (docker-compose.yml passes TZ) ----
# The image runs in UTC; without this, quiet hours and the daily route check would follow UTC, not the household's
# clock. A TZ already set to a zone name wins; else the zone /etc/localtime links to; else timedatectl's.
host_zone() {
    local z="${TZ:-}"
    z="${z#:}"
    case "$z" in ""|/*|*..*) z="" ;; esac
    if [ -z "$z" ] && [ -L /etc/localtime ]; then
        z="$(readlink /etc/localtime)"
        case "$z" in *zoneinfo/*) z="${z#*zoneinfo/}"; z="${z#posix/}" ;; *) z="" ;; esac
    fi
    if [ -z "$z" ] && command -v timedatectl >/dev/null 2>&1; then
        z="$(timedatectl show -p Timezone --value 2>/dev/null)"
    fi
    printf '%s' "$z"
}
TZ="$(host_zone)"
export TZ
step "Time zone for the container: ${TZ:-UTC (not found on the host)}"

# ---- build and recreate ----
if [ $NO_BUILD = 0 ]; then
    step "Building $IMAGE"
    run podman-compose build || fail "build failed"
    # A stale build cache must never ship another tree's code: the image's package has to equal src/pengumon here.
    step "Checking that the image holds this tree's code"
    run "$ROOT/scripts/image-matches-tree.sh" "$IMAGE" \
        || fail "the built image does not match this tree (stale build cache?): rebuild with podman build --no-cache"
fi
step "Recreating the container"
run podman-compose down || fail "removing the old container failed"
run podman-compose up -d || fail "starting the container failed"

if [ $DRY_RUN = 1 ]; then step "Dry run finished: nothing was changed"; exit 0; fi

# /api/health answers 200 once the probe loop has finished a round; the first start after an upgrade may
# migrate the database first (minutes at most).
step "Waiting for the API (up to 5 minutes)"
for _ in $(seq 1 100); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        printf '\033[32mPengumon is up: http://%s:%s\033[0m\n' "$(hostname -I | awk '{print $1}')" "$PORT"
        # one rollback point per project: drop the older ones, but only once this run made a new one
        if podman image exists "localhost/pengumon:pre-deploy-$STAMP"; then
            podman images --format '{{.Repository}}:{{.Tag}}' | grep '^localhost/pengumon:pre-deploy-' \
                | grep -v ":pre-deploy-$STAMP\$" | xargs -r podman rmi >/dev/null 2>&1
        fi
        if [ -f "$BACKUPS/$STAMP/pengumon.db" ]; then
            find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d ! -name "$STAMP" -exec rm -rf {} + 2>/dev/null
        fi
        exit 0
    fi
    if [ "$(podman inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]; then
        podman logs --tail 40 "$NAME" 2>&1
        fail "Pengumon isn't running; the rollback image and backup were kept"
    fi
    sleep 3
done
podman logs --tail 40 "$NAME" 2>&1
fail "Pengumon didn't become healthy within 5 minutes; the rollback image and backup were kept"
