# Install

Pengumon runs natively on Windows, macOS and Linux (including a 64-bit Raspberry Pi), or in a container. Pick the
section for your computer: [Windows](#windows), [macOS](#macos), [Linux](#linux), [Raspberry Pi](#raspberry-pi) or
[Container](#container). Every native install ends the same way: Pengumon runs in the background, starts by itself,
listens on this computer only (`http://127.0.0.1:8000`), and its page opens in your browser. What you do next is in
[Your first minutes](#your-first-minutes).

## What has been tested where

| | Tested how |
|---|---|
| Linux, x86-64 | A real computer (Linux Mint): the service, the dashboard and the container. GitHub's Linux runner installs with `install.sh` (the system service) on every change |
| Linux, per-user service (`--user`) | Not in CI; it is the same unit, run as you |
| Raspberry Pi, 64-bit | Not on a Pi. It is the Linux arm64 install: GitHub's arm64 runner runs the unit tests natively, and the arm64 image is built on a native arm64 runner |
| macOS | GitHub's macOS 14 runner installs the LaunchDaemon with `install.sh`, checks that it answers, upgrades and uninstalls. The `--user` LaunchAgent and the Local Network permission are not tested. Not on a real Mac yet |
| Windows | GitHub's Windows Server runner installs with `install.ps1`, checks that the logon task answers, upgrades and uninstalls. Not on a real Windows 10 or 11 PC yet |
| Container | The image is tested read-only, as uid 10001, on the Linux runner and on a real Linux host ([testing.md](testing.md)). Docker Desktop is not supported |

If something differs on your macOS or Windows computer, that is a bug worth reporting.

The install scripts are small and readable: [`install.sh`](../scripts/install.sh) (Linux, macOS) and
[`install.ps1`](../scripts/install.ps1) (Windows). They:

1. download the wheel, the dependency lock and `SHA256SUMS` from the [GitHub release](https://github.com/pengupowered/pengumon/releases)
   and check the wheel and the lock against `SHA256SUMS`;
2. download [uv](https://docs.astral.sh/uv/) at a version and sha256 pinned in the script, and check it;
3. let uv install Python 3.12 (kept inside Pengumon's own folder, nothing is changed in your system Python) and create a
   virtual environment;
4. install every dependency from the lock with `uv pip install --require-hashes --only-binary :all:`, so each file must
   match its recorded hash and nothing is compiled, then install the wheel;
5. run `pengumon service install`, wait until `/api/health` answers, and open the browser.

The lock is the same file the container image is built from (`requirements.txt`, one lock for every operating
system, with markers for the packages only some systems need).

Options (the same names in both scripts, `-Version` style on Windows): `--version X.Y.Z` (default: the latest
release), `--port N`, `--no-service` (install only, start it yourself with `pengumon run`), `--no-browser`,
`--from DIR` (install from a folder with the release files, for a computer without internet access), `--uninstall`.

## Windows

**Needs:** 64-bit Windows 10 or 11 with Windows PowerShell 5.1 or newer (already there), and internet access to GitHub for the download. No administrator rights. In PowerShell:

```powershell
irm https://github.com/pengupowered/pengumon/releases/latest/download/install.ps1 | iex
```

Or download `install.ps1` first, read it, and run `powershell -ExecutionPolicy Bypass -File install.ps1`.

What you get:

- The program in `%LOCALAPPDATA%\Programs\Pengumon`, your data (database, `config.yaml`, `secrets.json`, `pengumon.log`)
  in `%LOCALAPPDATA%\Pengumon`.
- A Task Scheduler task named `Pengumon` that starts Pengumon every time you sign in, without a console window
  (`pengumonw.exe`), and restarts it if it stops. It is not a Windows service: it runs while you are signed in, as you.
  Windows may show a firewall prompt only if you change the listen address from `127.0.0.1`.
- Probing uses Windows' own ICMP API, which needs no special rights. Time resolution is about 1 ms there.

Check it with `pengumon service status` (the scripts print the full path of `pengumon.exe`; add the folder
`%LOCALAPPDATA%\Programs\Pengumon\venv\Scripts` to your PATH if you want the short command). Windows keeps logs of the
windowless run in `%LOCALAPPDATA%\Pengumon\pengumon.log`.

## macOS

**Needs:** macOS on Apple silicon or Intel, the Terminal's `curl` and `sh` (both ship with macOS), internet access to GitHub, and for the default install an administrator account (for sudo). In Terminal:

```sh
curl -fsSL https://github.com/pengupowered/pengumon/releases/latest/download/install.sh | sh
```

By default this installs a **system service** and asks for your password once (sudo), after saying why: it puts the
program in `/usr/local/pengumon`, creates a hidden account `_pengumon` with no login, and adds a LaunchDaemon that runs
Pengumon as that account from boot, before anyone signs in. A service run by the system is not subject to macOS's
"find devices on your local network" permission, which a program run from your own session is. Data is in
`/Library/Application Support/Pengumon`, the log in `pengumon.log` next to it.

Without sudo: add `--user`. That installs a LaunchAgent under your own account (program in
`~/Library/Application Support/Pengumon/app`, data in `~/Library/Application Support/Pengumon`) that starts when you sign
in. macOS may ask once whether Pengumon may find devices on your local network; allow it.

The pings and trace probes use an unprivileged ICMP socket, so no root is needed to probe.

## Linux

**Needs:** a systemd-based distribution on x86-64 or arm64, `curl` and `sh`, internet access to GitHub, and for the default install `sudo`. In a terminal:

```sh
curl -fsSL https://github.com/pengupowered/pengumon/releases/latest/download/install.sh | sh
```

By default this installs a **system service** and asks for your password once (sudo), after saying why: it puts the
program in `/opt/pengumon` (owned by root, so the service cannot change its own code) and adds a systemd unit
`pengumon.service`. The unit runs Pengumon as a throw-away user (`DynamicUser`) with `CAP_NET_RAW` as its only
privilege, so it can send ICMP without changing the system's `ping_group_range` setting, and with the rest of the system
read-only or hidden (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`, a system call filter, and more).
`systemd-analyze security pengumon.service` rates it about 1.5 ("OK"); CI fails if it rises above 2.0. Data is in
`/var/lib/pengumon`.

Without sudo: add `--user`. That installs a systemd *user* unit that runs as you (program in `~/.local/share/pengumon`,
data in `~/.local/state/pengumon`). Two things differ:

- Many distributions (Debian, Ubuntu, Raspberry Pi OS) do not let ordinary users send ICMP. The script runs
  `pengumon doctor` and, if probing does not work, shows the one fix and offers to run it with sudo:
  `echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/60-unprivileged-ping.conf && sudo sysctl --system`.
- A user service stops when you log out unless lingering is on: `sudo loginctl enable-linger $USER`.

## Raspberry Pi

**Needs:** a **64-bit** Raspberry Pi OS (Pi 3, 4, 5 and Zero 2 W). The 32-bit images are not supported: several dependencies
publish no 32-bit ARM wheels, and the scripts refuse to run there (`uname -m` says `aarch64` on a 64-bit system). Then
follow [Linux](#linux): the system service is the right choice on a Pi that stays on, and a Pi that you only reach over
SSH needs [the dashboard opened to other devices](#opening-the-dashboard-from-other-devices). Raspberry Pi OS needs the
ping setting only for `--user`; the system service does not.

## Container

Docker or Podman on a Linux host, x86-64 or arm64. The published image is built from the same wheel as the native
install, runs as an unprivileged user with a read-only root filesystem and writes only to its data volume and a `/tmp`
tmpfs. It uses host networking so the probes measure the host's own path.

```bash
mkdir pengumon && cd pengumon
curl -fsSL https://raw.githubusercontent.com/pengupowered/pengumon/main/compose.release.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/pengupowered/pengumon/main/config.example.yaml -o config.yaml
                                      # no targets in it: Pengumon finds your network path itself (see First run)
docker compose up -d                  # or: podman compose up -d
```

Open `http://<host>:8000` and press **Review** on the banner (see [Your first minutes](#your-first-minutes)).
`config.yaml` holds the settings that are not edited in the dashboard: device drivers, service checks, thresholds, line
capacity. The example has no active `targets:` (the path is discovered); a `targets:` list there is an optional fixed
list, used on the first start only.

Optional settings (access token, device passwords, ntfy, OpenTelemetry) go in a `.env` file next to `compose.yaml`;
[`.env.example`](../.env.example) lists them. The container listens on all interfaces, so **set
`PENGUMON_ACCESS_TOKEN`** unless the host is only reachable by people you trust ([Security](security.md)).

- **Rootless Podman** needs unprivileged ping allowed for your user, once:
  `echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/60-unprivileged-ping.conf && sudo sysctl --system`.
  `docker exec pengumon pengumon doctor` (or `podman exec`) says whether probing works.
- **Docker Desktop on macOS and Windows** is not supported: its containers run inside a virtual machine, so host
  networking attaches to that VM and the probes would measure the VM's path. Use the native install there.
- **Upgrade**: change the version in the `image:` line of `compose.yaml` to the newest
  [release](https://github.com/pengupowered/pengumon/releases) and run `docker compose up -d` again. The data lives in
  the `pengumon_data` volume; `docker exec pengumon pengumon backup` copies it first.
- **Remove**: `docker compose down` (the volume stays; `docker compose down -v` deletes it too).

### Building from source instead

```bash
git clone https://github.com/pengupowered/pengumon.git
cd pengumon
cp config.example.yaml config.yaml    # no targets in it: the path is discovered on the first visit
cp .env.example .env                  # optional: access token, device passwords, ntfy
docker compose up -d --build          # or: podman-compose up -d --build
```

`scripts/deploy.sh` is the maintainer's deploy: it adds a dependency audit, a rollback image and a database backup
before rebuilding.

## Your first minutes

1. Open `http://127.0.0.1:8000` (on the same computer; the install script opens it for you).
2. **Path health** shows "Finding your network path" and then "Pengumon found your network path". Press **Review**,
   check the hops (each has a role, a badge of how sure it is, and the reasons), rename any you like, and press
   **Apply**. Until then the default targets are probed. What discovery sends where is in
   [discovery.md](discovery.md); the editor is in [path-editor.md](path-editor.md).
3. A week of history makes the charts and reports useful. Nothing leaves your computer unless you turn on alerts or the
   OpenTelemetry export ([telemetry.md](telemetry.md)).

## Opening the dashboard from other devices

By default Pengumon listens on `127.0.0.1`, this computer only, which is also why no firewall prompt appears. To open it
from a phone or another computer (the usual case on a Raspberry Pi or a server):

1. Choose a long random access token and give it to the service as `PENGUMON_ACCESS_TOKEN`. Without a token, anyone who
   can reach the port can read the data and change settings.
2. Listen on the network: `pengumon service install --host 0.0.0.0` (add `--system` or `--user` as the same install
   used), or `server.host: "0.0.0.0"` in `config.yaml` and a restart. Containers already do.
3. Open `http://<computer's address>:8000` and sign in with the token. If you use a name that is not an IP address, a
   single word or `.lan`, `.local`, `.home.arpa` or `.internal`, add it to `PENGUMON_ALLOWED_HOSTS` ([Security](security.md)).
4. Your computer's firewall must allow the port; the install scripts do not open it.

Setting the token for a native service: on Linux, `sudo systemctl edit pengumon` (system service) or
`systemctl --user edit pengumon`, add

```ini
[Service]
Environment=PENGUMON_ACCESS_TOKEN=put-your-token-here
```

and `sudo systemctl restart pengumon` (`systemctl --user restart pengumon`). On macOS the token goes in the
`EnvironmentVariables` of the LaunchDaemon or LaunchAgent plist, and on Windows in a per-user environment variable
(`setx PENGUMON_ACCESS_TOKEN ...`) followed by a sign-out and sign-in; these two have not been tried on a real computer.

## Upgrade

Run the same install script again (it fetches the newest release, or pass `--version X.Y.Z`). It first runs
`pengumon backup` (a consistent copy of the database, `secrets.json` and `sessions.json` in `backups/` inside the data
directory; it runs as the service's own account, so it can read the data: on Linux through `systemd-run` with the service's DynamicUser identity, on macOS with `sudo -u _pengumon`) and stops if the backup fails, removes the old service, reinstalls the program from the new release, and
installs the service again. Your configuration and data are not touched. Containers: see above.

## Uninstall

```sh
curl -fsSL https://github.com/pengupowered/pengumon/releases/latest/download/install.sh | sh -s -- --uninstall
```

On Windows: `install.ps1 -Uninstall`. This removes the service and the program; **your data stays** in the data
directory (each system's section above names it; `pengumon paths` prints it). Delete that folder yourself if you want
it gone. On macOS the hidden
`_pengumon` account also stays, because it owns the data. Without the script: `pengumon service uninstall` removes only
the service.

## The `pengumon service` command

The scripts use it, and you can use it directly from the installed program:

| Command | What it does |
|---|---|
| `pengumon service install [--system \| --user] [--host H] [--port N] [--dry-run]` | Writes the unit, plist or task for this system and starts it. As root the default is the system service, otherwise the per-user one; Windows has only the per-user task. `--dry-run` prints what it would do and the files, changing nothing. |
| `pengumon service uninstall [--system \| --user]` | Stops and removes the service, keeping the data. |
| `pengumon service status` | Whether the service is installed, running and answering `/api/health`. Exit 0 yes, 1 installed but not answering, 3 not installed. |

The other commands: `pengumon run` starts the server in this terminal (the default when you give none), `pengumon version`
prints the version, `pengumon paths` shows where the config, data and database are and the listen address,
`pengumon backup [--to DIR]` copies the database, `secrets.json` and `sessions.json`, and `pengumon doctor` says whether
this computer can probe and what fixes it (exit 0 when it can).

## Troubleshooting

First run `pengumon doctor` and `pengumon service status`. The script prints where `pengumon` is; if the short command is
not found, give its full path.

**Linux**

- Log: `journalctl -u pengumon -e` (system service) or `journalctl --user -u pengumon -e` (per-user).
- "ICMP not permitted" in doctor with `--user`: run the sysctl command above (Debian, Ubuntu, Mint, Raspberry Pi OS).
  The system service does not need it.
- A per-user service that stops when you log out: `sudo loginctl enable-linger $USER`.
- `systemd-analyze security pengumon.service` shows how locked down the system service is.
- Port in use: install with `--port N` or stop the other program.

**macOS**

- Log: `pengumon.log` in the data directory (`/Library/Application Support/Pengumon` for the daemon,
  `~/Library/Application Support/Pengumon` for `--user`). `sudo launchctl print system/com.pengupowered.pengumon` (or
  `launchctl print gui/$(id -u)/com.pengupowered.pengumon` for the agent) shows its state.
- Probes to your router fail in a per-user install: check System Settings > Privacy & Security > Local Network and allow
  Pengumon (or Terminal, if it was started from there), or reinstall as the system service, which is not subject to it.

**Windows**

- Log: `%LOCALAPPDATA%\Pengumon\pengumon.log`. The task is `Pengumon` in Task Scheduler (`schtasks /query /tn Pengumon`).
- It runs only while you are signed in. If `irm ... | iex` is blocked by your execution policy, download `install.ps1`
  and run `powershell -ExecutionPolicy Bypass -File install.ps1`.
- Security software that blocks the script: it only downloads from github.com and the uv release host, and the file
  hashes are checked ([Checking a download](#checking-a-download)).

**Everywhere**

- The page says "Not connected" or is empty right after start: the first probe round takes a few seconds; reload.
- `http://127.0.0.1:8000/api/health` answers 200 once the first round is done.
- Discovery finds fewer hops than you expect: some routers do not answer the trace at all; add them by address in the
  editor ([path-editor.md](path-editor.md)).

## Checking a download

Each release lists `SHA256SUMS` and the scripts check it themselves. The files also carry signed build provenance, which
proves they were built from this repository by the release workflow (needs the [GitHub CLI](https://cli.github.com/)):

```sh
gh attestation verify pengumon-X.Y.Z-py3-none-any.whl --repo pengupowered/pengumon
```

The container image has one too: `gh attestation verify oci://ghcr.io/pengupowered/pengumon:X.Y.Z --repo pengupowered/pengumon`.

What the `SHA256SUMS` check protects against: a corrupt or partial download, a file from another release mixed in, and
files swapped in a `--from` folder. What it does not: a compromised release, which replaces the files and `SHA256SUMS`
together, because both come from the same place. The download is protected by TLS, so never point `PENGUMON_RELEASE_URL`
at an `http://` address. The attestation above (optional, and it needs the GitHub CLI) is what proves who built the files.

## Quick way: uv tool install

If you already have [uv](https://docs.astral.sh/uv/), `uv tool install pengumon` installs the same wheel from PyPI
into its own environment and puts `pengumon` on your PATH. It does **not** check dependency hashes (use the install
script for that), and it does not set up the service: run `pengumon service install --user`, or start it with
`pengumon run`. `uv tool install --exclude-newer "7 days ago" pengumon` keeps the 7-day cooldown on new releases that
this project applies to its own dependencies.
