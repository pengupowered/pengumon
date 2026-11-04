#!/bin/sh
# Installs (or upgrades) Pengumon on Linux and macOS and starts it in the background.
#
#   curl -fsSL https://github.com/pengupowered/pengumon/releases/latest/download/install.sh | sh
#   sh install.sh [--version X.Y.Z] [--user] [--port N] [--no-service] [--no-browser] [--yes]
#   sh install.sh --from DIR        install from files on disk (DIR has pengumon-X.Y.Z-py3-none-any.whl,
#                                   pengumon-X.Y.Z.lock and SHA256SUMS), for CI and machines without internet
#   sh install.sh --uninstall       remove the service and the program; your data stays
#
# What it does, in order:
#   1. gets the wheel, the lock (every dependency pinned with hashes) and SHA256SUMS from the GitHub release and
#      checks the wheel and the lock against SHA256SUMS;
#   2. downloads uv (version and sha256 pinned below, per platform) and checks it;
#   3. on an upgrade, runs `pengumon backup` and stops the old service (its --host and --port are kept: the new
#      definition reads them from the one it replaces);
#   4. installs Python 3.12 (managed by uv, kept inside the app folder) and a virtual environment, then the locked
#      dependencies with `uv pip install --require-hashes --only-binary :all:`, then the wheel;
#   5. runs `pengumon service install`, waits until it answers, and opens the browser.
#
# Where things go (system install / --user):
#   Linux   /opt/pengumon, data /var/lib/pengumon, systemd system unit  /  ~/.local/share/pengumon, ~/.local/state/pengumon, user unit
#   macOS   /usr/local/pengumon, data /Library/Application Support/Pengumon, LaunchDaemon  /  ~/Library/Application Support/Pengumon/app, LaunchAgent
# sudo is asked for only by the system install, and the script says why before it does. --user needs none (on Linux
# it may offer one sudo command, the ping setting, if your distribution does not allow unprivileged ping).
set -eu

REPO="pengupowered/pengumon"
UV_VERSION="0.13.4"
# sha256 of the uv release archives (https://github.com/astral-sh/uv/releases/tag/0.13.4)
UV_SHA256_X86_64_UNKNOWN_LINUX_MUSL="28d4385f9f9d1d1c7654d85b3855e0e2615b2f6cc13136a2edff858bbdc16ff1"
UV_SHA256_AARCH64_UNKNOWN_LINUX_MUSL="253a17cadcb659ae77a0fe3b9a2f1c14297528d480c421899e61e64d64f24e8f"
UV_SHA256_AARCH64_APPLE_DARWIN="e0b1d4743ce64c55d9f0f4a444afa6b81125680070ee04e853fcd0f6c1ebf8f1"
UV_SHA256_X86_64_APPLE_DARWIN="7be2dcd316122f4b3e5a0a7889f6248266c14425374e105ff7dc9779184603a6"
PYTHON_VERSION="3.12"
SYSCTL_FIX="echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/60-unprivileged-ping.conf && sudo sysctl --system"

VERSION="${PENGUMON_VERSION:-}"
BASE_URL="${PENGUMON_RELEASE_URL:-}"
FROM=""
SCOPE=""
PORT=""
SERVICE=1
BROWSER=1
YES=0
UNINSTALL=0

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -eu/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version) [ $# -ge 2 ] || die "--version needs a value"; VERSION="${2#v}"; shift ;;
        --from) [ $# -ge 2 ] || die "--from needs a folder"; FROM="$2"; shift ;;
        --port) [ $# -ge 2 ] || die "--port needs a number"; PORT="$2"; shift ;;
        --user) SCOPE=user ;;
        --system) SCOPE=system ;;
        --no-service) SERVICE=0 ;;
        --no-browser) BROWSER=0 ;;
        --yes|-y) YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done
case "$PORT" in ''|*[!0-9]*) [ -z "$PORT" ] || die "--port must be a number" ;; esac
nonl() {  # nonl NAME VALUE: a newline in a value that ends up in a file or a command is refused
    case "$2" in *'
'*) die "$1 must not contain a newline" ;; esac
}
nonl "--from" "$FROM"
nonl "HOME" "${HOME:-}"
nonl "XDG_DATA_HOME" "${XDG_DATA_HOME:-}"
nonl "XDG_STATE_HOME" "${XDG_STATE_HOME:-}"

# ---- this machine ------------------------------------------------------------------------------------------------

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
    Linux/x86_64|Linux/amd64) UV_TARGET="x86_64-unknown-linux-musl" ;;
    Linux/aarch64|Linux/arm64) UV_TARGET="aarch64-unknown-linux-musl" ;;
    Darwin/arm64) UV_TARGET="aarch64-apple-darwin" ;;
    Darwin/x86_64) UV_TARGET="x86_64-apple-darwin" ;;
    Linux/armv7*|Linux/armv6*|Linux/i?86) die "$ARCH is a 32-bit system, which Pengumon does not support. On a Raspberry Pi use the 64-bit Raspberry Pi OS." ;;
    *) die "unsupported system: $OS $ARCH" ;;
esac
case "$UV_TARGET" in
    x86_64-unknown-linux-musl) UV_SHA256="$UV_SHA256_X86_64_UNKNOWN_LINUX_MUSL" ;;
    aarch64-unknown-linux-musl) UV_SHA256="$UV_SHA256_AARCH64_UNKNOWN_LINUX_MUSL" ;;
    aarch64-apple-darwin) UV_SHA256="$UV_SHA256_AARCH64_APPLE_DARWIN" ;;
    x86_64-apple-darwin) UV_SHA256="$UV_SHA256_X86_64_APPLE_DARWIN" ;;
esac

if [ "$(id -u)" = 0 ]; then
    SUDO=""
    [ -n "$SCOPE" ] || SCOPE=system
    [ "$SCOPE" = system ] || die "--user as root would install for root; run it as the user who should own the service"
else
    SUDO="sudo"
    [ -n "$SCOPE" ] || SCOPE=system
fi

if [ "$SCOPE" = system ]; then
    if [ "$OS" = Linux ]; then
        APP="/opt/pengumon"; DATA="/var/lib/pengumon"
    else
        APP="/usr/local/pengumon"; DATA="/Library/Application Support/Pengumon"
    fi
    RUN_ROOT="$SUDO"
else
    if [ "$OS" = Linux ]; then
        APP="${XDG_DATA_HOME:-$HOME/.local/share}/pengumon"; DATA="${XDG_STATE_HOME:-$HOME/.local/state}/pengumon"
    else
        APP="$HOME/Library/Application Support/Pengumon/app"; DATA="$HOME/Library/Application Support/Pengumon"
    fi
    RUN_ROOT=""
fi
SCOPE_FLAG="--$SCOPE"
VENV="$APP/venv"
PENGUMON="$VENV/bin/pengumon"

root() { if [ -n "$RUN_ROOT" ]; then "$RUN_ROOT" "$@"; else "$@"; fi; }

# The backup of a system install is taken as the service's own account, never as root: the data directory belongs
# to that account, and root writing into it would follow any symlink that account (or whatever took it over) planted.
#   Linux  a transient systemd unit with the service's own sandbox identity (DynamicUser, User=pengumon: the same uid
#          as the service, because the dynamic user is named after the unit) and the same StateDirectory, so
#          /var/lib/pengumon is reached exactly as the service reaches it;
#   macOS  sudo -u _pengumon, the account that owns /Library/Application Support/Pengumon.
# A --user install runs everything as you already.
backup_data() {
    if [ "$SCOPE" != system ]; then "$PENGUMON" backup; return; fi
    if [ "$OS" = Linux ]; then
        command -v systemd-run > /dev/null 2>&1 || { echo "systemd-run is not installed, so the backup cannot be taken safely"; return 1; }
        # A named unit, so that when it fails (systemd-run --quiet says nothing about why) its journal can be shown.
        unit="pengumon-backup-$$"
        if $SUDO systemd-run --quiet --wait --pipe --collect --unit="$unit" \
            -p DynamicUser=yes -p User=pengumon -p StateDirectory=pengumon -p StateDirectoryMode=0700 \
            -p UMask=0077 -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p NoNewPrivileges=yes \
            --setenv=PENGUMON_DATA="$DATA" --setenv=PYTHONDONTWRITEBYTECODE=1 "$PENGUMON" backup; then
            return 0
        fi
        echo "the backup unit $unit.service failed. What systemd logged for it:"
        $SUDO journalctl -u "$unit.service" -n 15 --no-pager -o cat 2> /dev/null || echo "(no journal available)"
        echo "To see more: sudo journalctl -u $unit.service"
        return 1
    else
        if ! dscl . -read /Users/_pengumon UniqueID > /dev/null 2>&1; then
            echo "no database: the service account does not exist yet, so there is no data to back up"
            return 1
        fi
        ( cd / && sudo -u _pengumon env "PENGUMON_DATA=$DATA" PYTHONDONTWRITEBYTECODE=1 "$PENGUMON" backup )
    fi
}

# Stops the running service without removing its definition: the new version's setup reads the listen address
# (--host, --port) out of it and puts it into the new definition.
stop_service() {
    if [ "$OS" = Linux ]; then
        if [ "$SCOPE" = system ]; then root systemctl stop pengumon.service > /dev/null 2>&1 || true
        else systemctl --user stop pengumon.service > /dev/null 2>&1 || true; fi
    elif [ "$SCOPE" = system ]; then
        root launchctl bootout system/com.pengupowered.pengumon > /dev/null 2>&1 || true
    else
        launchctl bootout "gui/$(id -u)/com.pengupowered.pengumon" > /dev/null 2>&1 || true
    fi
}

# What a failed start leaves to look at: where the service writes its log.
log_hint() {
    if [ "$OS" != Linux ]; then
        if [ "$SCOPE" = system ]; then echo "The log is $DATA/pengumon.log"; else echo "The log is $HOME/Library/Logs/Pengumon/pengumon.log"; fi
    elif [ "$SCOPE" = system ]; then echo "To see why: journalctl -u pengumon.service -e"
    else echo "To see why: journalctl --user -u pengumon.service -e"; fi
}

confirm() {  # confirm "question": yes with --yes, else ask on the terminal
    [ "$YES" = 1 ] && return 0
    if [ -r /dev/tty ] && : < /dev/tty 2> /dev/null; then
        printf '%s [Y/n] ' "$1" > /dev/tty
        read -r answer < /dev/tty || answer=n
        case "$answer" in ''|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
    fi
    die "$1 -- there is no terminal to ask on; run again with --yes to agree"
}

explain_sudo() {
    [ -n "$RUN_ROOT" ] || return 0
    command -v sudo > /dev/null 2>&1 || die "this needs administrator rights and sudo is not installed; run it as root, or add --user"
    if [ "$OS" = Linux ]; then
        say "Pengumon is about to ask for your password (sudo), once, to:"
        say "  - put the program in $APP, where no one can change it but root;"
        say "  - add a systemd service that runs it as an unprivileged throw-away user whose only privilege is"
        say "    sending ICMP (ping) packets, so it works on every distribution without changing a kernel setting."
    else
        say "Pengumon is about to ask for your password (sudo), once, to:"
        say "  - put the program in $APP, where no one can change it but an administrator;"
        say "  - add a hidden account _pengumon and a LaunchDaemon that runs it as that account at boot."
        say "    (A service run by the system is not subject to macOS's local-network permission prompt.)"
    fi
    say "If you would rather not, press Ctrl-C and run again with --user: it needs no sudo."
    confirm "Continue?" || die "cancelled"
}

# ---- uninstall -----------------------------------------------------------------------------------------------------

if [ "$UNINSTALL" = 1 ]; then
    if [ -x "$PENGUMON" ]; then
        if [ -n "$RUN_ROOT" ]; then
            say "Removing the system service needs administrator rights, so sudo will ask for your password."
            confirm "Continue?" || die "cancelled"
        fi
        root "$PENGUMON" service uninstall "$SCOPE_FLAG" || true
        root rm -rf "$APP"
        # The folder of systemd drop-ins ("Opening the dashboard", step 3) holds the access token: it goes with the service.
        if [ "$OS" = Linux ]; then
            if [ "$SCOPE" = system ]; then DROPIN="/etc/systemd/system/pengumon.service.d"
            else DROPIN="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/pengumon.service.d"; fi
            if [ -d "$DROPIN" ]; then
                root rm -rf "$DROPIN"
                if [ "$SCOPE" = system ]; then root systemctl daemon-reload || true; else systemctl --user daemon-reload || true; fi
                say "Removed $DROPIN (it held the access token)."
            fi
        fi
        say "Pengumon is removed. Your data is still in $DATA (delete it yourself if you do not want it)."
    else
        say "Nothing to remove: $PENGUMON does not exist."
    fi
    exit 0
fi

# ---- tools ---------------------------------------------------------------------------------------------------------

if command -v sha256sum > /dev/null 2>&1; then
    sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum > /dev/null 2>&1; then
    sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    die "neither sha256sum nor shasum is installed"
fi

fetch() {  # fetch URL FILE
    if command -v curl > /dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1" || die "cannot download $1"
    elif command -v wget > /dev/null 2>&1; then
        wget -q -O "$2" "$1" || die "cannot download $1"
    else
        die "neither curl nor wget is installed"
    fi
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/pengumon-install.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# ---- the release ---------------------------------------------------------------------------------------------------

if [ -n "$FROM" ]; then
    [ -d "$FROM" ] || die "--from: $FROM is not a folder"
    if [ -z "$VERSION" ]; then
        set -- "$FROM"/pengumon-*-py3-none-any.whl
        if [ "$#" != 1 ] || [ ! -f "$1" ]; then die "--from: expected exactly one pengumon-*-py3-none-any.whl in $FROM"; fi
        VERSION="$(basename "$1" | sed -n 's/^pengumon-\([0-9][^-]*\)-.*/\1/p')"
    fi
else
    if [ -z "$VERSION" ]; then
        say "Looking up the latest release..."
        fetch "https://api.github.com/repos/$REPO/releases/latest" "$WORK/latest.json"
        VERSION="$(sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' "$WORK/latest.json" | head -n 1)"
        [ -n "$VERSION" ] || die "cannot read the latest version from GitHub; pass --version X.Y.Z"
    fi
    [ -n "$BASE_URL" ] || BASE_URL="https://github.com/$REPO/releases/download/v$VERSION"
fi
# anchored like install.ps1's ^[0-9]+\.[0-9]+\.[0-9]+$: digits and exactly two dots, nothing else (a newline fails too)
case "$VERSION" in
    ''|*[!0-9.]*|.*|*.|*..*|*.*.*.*) die "not a version number: $VERSION" ;;
    *.*.*) ;;
    *) die "not a version number: $VERSION" ;;
esac
WHEEL="pengumon-$VERSION-py3-none-any.whl"
LOCK="pengumon-$VERSION.lock"

say "Pengumon $VERSION"
for f in "$WHEEL" "$LOCK" SHA256SUMS; do
    if [ -n "$FROM" ]; then
        [ -f "$FROM/$f" ] || die "--from: $FROM/$f is missing"
        cp "$FROM/$f" "$WORK/$f"
    else
        fetch "$BASE_URL/$f" "$WORK/$f"
    fi
done

verify() {  # verify FILE: its sha256 is the one SHA256SUMS lists
    want="$(awk -v f="$1" '$2 == f || $2 == "*" f { print $1; exit }' "$WORK/SHA256SUMS")"
    [ -n "$want" ] || die "SHA256SUMS does not list $1"
    have="$(sha256_of "$WORK/$1")"
    [ "$have" = "$want" ] || die "$1 does not match SHA256SUMS (expected $want, got $have); nothing was installed"
}
verify "$WHEEL"
verify "$LOCK"
say "Checked $WHEEL and $LOCK against SHA256SUMS."

# ---- uv ------------------------------------------------------------------------------------------------------------

UV_ARCHIVE="uv-$UV_TARGET.tar.gz"
fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE" "$WORK/$UV_ARCHIVE"
[ "$(sha256_of "$WORK/$UV_ARCHIVE")" = "$UV_SHA256" ] || die "$UV_ARCHIVE does not match the sha256 pinned in this script; nothing was installed"
tar -xzf "$WORK/$UV_ARCHIVE" -C "$WORK"
UV="$WORK/uv-$UV_TARGET/uv"
[ -x "$UV" ] || die "uv is not in $UV_ARCHIVE"
say "uv $UV_VERSION checked."

# ---- upgrade: back up, stop ------------------------------------------------------------------------------------------

explain_sudo
umask 022
if [ -x "$PENGUMON" ]; then
    say "Pengumon is already installed here ($("$PENGUMON" version 2> /dev/null || echo unknown)): upgrading."
    if out="$(backup_data 2>&1)"; then
        say "Backup of your data: $out"
    else
        case "$out" in
            *"no database"*) say "No database yet, so nothing to back up." ;;
            *) die "the backup failed, so nothing was changed: ${out:-no reason was given.}
  To try the backup yourself: $(if [ "$SCOPE" = system ]; then echo "sudo $PENGUMON backup --system --to ~/pengumon-backup"; else echo "$PENGUMON backup"; fi)" ;;
        esac
    fi
    stop_service
fi

# ---- install ---------------------------------------------------------------------------------------------------------

root mkdir -p "$APP"
# Python is kept inside the app folder (a service that cannot see /home must still find it); no cache is left behind
uvr() { root env "UV_PYTHON_INSTALL_DIR=$APP/python" "UV_PYTHON_PREFERENCE=only-managed" "$UV" --no-config --no-cache "$@"; }

say "Installing Python $PYTHON_VERSION and the locked dependencies into $APP ..."
uvr python install "$PYTHON_VERSION"
uvr venv --clear --python "$PYTHON_VERSION" "$VENV"
uvr pip install --python "$VENV/bin/python" --require-hashes --only-binary :all: --no-deps --compile-bytecode -r "$WORK/$LOCK"
verify "$WHEEL"   # again, right before it is used
uvr pip install --python "$VENV/bin/python" --no-deps --compile-bytecode "$WORK/$WHEEL"
"$PENGUMON" version > /dev/null || die "the installed pengumon does not start"
say "Installed $("$PENGUMON" version)."

# ---- service ------------------------------------------------------------------------------------------------------------

answers() {
    if command -v curl > /dev/null 2>&1; then curl -fsS -m 3 -o /dev/null "$1" 2> /dev/null
    else wget -q -T 3 -O /dev/null "$1" 2> /dev/null; fi
}
LISTEN_PORT="${PORT:-8000}"
if [ "$SERVICE" = 0 ]; then
    say "No service installed. Start it with: $PENGUMON run"
    exit 0
fi

# The service keeps the listen address of the one it replaces (--host, --port) unless --port says otherwise; the port it
# ended up on is in its last line ("... open http://127.0.0.1:PORT/").
if [ -n "$PORT" ]; then
    root "$PENGUMON" service install "$SCOPE_FLAG" --port "$PORT" > "$WORK/service.txt" 2>&1 || { cat "$WORK/service.txt"; exit 1; }
else
    root "$PENGUMON" service install "$SCOPE_FLAG" > "$WORK/service.txt" 2>&1 || { cat "$WORK/service.txt"; exit 1; }
fi
cat "$WORK/service.txt"
kept_port="$(sed -n 's|.*open http://[^/]*:\([0-9][0-9]*\)/.*|\1|p' "$WORK/service.txt" | tail -n 1)"
[ -z "$kept_port" ] || LISTEN_PORT="$kept_port"

if [ "$OS" = Linux ] && [ "$SCOPE" = user ] && ! "$PENGUMON" doctor > "$WORK/doctor.txt" 2>&1; then
    cat "$WORK/doctor.txt"
    say ""
    say "This computer does not let ordinary users send ICMP (ping) packets, so Pengumon cannot probe yet."
    say "One setting fixes it, and it needs sudo because it changes the kernel for all users:"
    say "  $SYSCTL_FIX"
    if confirm "Run it now?"; then
        sh -c "$SYSCTL_FIX" || say "That did not work; run the line above yourself."
        systemctl --user restart pengumon.service || true
    fi
fi

say "Waiting for Pengumon to answer on port $LISTEN_PORT ..."
URL="http://127.0.0.1:$LISTEN_PORT/"
n=0
until answers "${URL}api/health"; do
    n=$((n + 1))
    if [ "$n" -ge 60 ]; then
        say "It has not answered after 2 minutes. Check it with: $PENGUMON service status"
        say "$(log_hint)"
        exit 1
    fi
    sleep 2
done
say "Pengumon is running: $URL"

if [ "$BROWSER" = 1 ]; then
    if [ "$OS" = Darwin ]; then
        open "$URL" > /dev/null 2>&1 || true
    elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open > /dev/null 2>&1; then
        xdg-open "$URL" > /dev/null 2>&1 || true
    else
        say "Open $URL in a browser (on this computer, or through an SSH tunnel)."
    fi
fi
say "Check it any time with: $PENGUMON service status"
