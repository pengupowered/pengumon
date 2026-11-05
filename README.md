<p align="center">
  <img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/logo.png" width="112" height="112" alt="">
</p>

<h1 align="center">Pengumon</h1>

<p align="center">
  <a href="https://pypi.org/project/pengumon/"><img src="https://img.shields.io/pypi/v/pengumon" alt="PyPI version"></a>
  <a href="https://github.com/pengupowered/pengumon/actions/workflows/release.yml"><img src="https://github.com/pengupowered/pengumon/actions/workflows/release.yml/badge.svg" alt="Release workflow status"></a>
  <a href="https://github.com/pengupowered/pengumon/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

<p align="center">
  <b>Find out where your internet actually breaks: your Wi-Fi, your modem, your provider, or the internet beyond.</b><br>
  A self-hosted network monitor that pings every hop once a second, explains each outage, and keeps the evidence.
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/overview-dark.png" alt="The Pengumon dashboard: path health from this host through the router, modem and three provider hops to the internet, a 24-hour latency chart and the cable modem's signal" width="100%">
</p>

## Why

"The internet is slow again" is hard to act on. Was it the Wi-Fi, the cable to the modem, a congested node at the
provider, or one far-away server? Pengumon watches each stage of the path all the time, so when something goes wrong
you can see where it started, how long it lasted and what the modem and router were doing at that moment, and hand your
provider a report instead of a hunch.

## Features

- **The whole path, live.** This host → router → modem → each provider hop → the internet, with the median latency,
  loss and jitter of every stage and how much each link adds.
- **Every outage explained.** A classifier places each problem in your home network, the line, the provider or beyond,
  and records an incident with per-hop evidence, the modem's signal and the line load at the time.
- **Busy is not broken.** Router WAN traffic is taken into account, so lag while someone is downloading is marked as
  expected rather than reported as a fault.
- **Patterns over time.** Loss by hour of the week, per-target uptime, a reliability report, and an ISP dossier to send
  to support. Click any hour or uptime block to see the incidents in it.
- **Ask an AI.** One Markdown document of Pengumon's own findings (where problems start, what is normal, what coincided,
  what it cannot see) with instructions, to paste into any chat model; nothing is sent anywhere by Pengumon. Full,
  Vague or Anonymized, from the dashboard (or `GET /api/ai-pack`): [the guide](https://github.com/pengupowered/pengumon/blob/main/docs/ai-pack.md).
- **Your modem and router.** DOCSIS channel levels and codeword errors; router CPU, memory, WAN traffic, connected
  clients and mesh nodes. Drivers are pluggable.
- **Your own apps too.** HTTP health checks for self-hosted services, with incidents and alerts by [ntfy](https://ntfy.sh) or webhook.
- **Small and private.** One process (installed natively or in a container) and one SQLite file. Nothing leaves your network unless you turn on
  notifications or the optional OpenTelemetry export. Works on a phone.

## Install

Pengumon runs natively on Windows, macOS and Linux, or in a container. One command per system; each link goes to that
system's section in [docs/install.md](https://github.com/pengupowered/pengumon/blob/main/docs/install.md), with what it needs, what it sets up and how to remove it.

**Windows** 10/11 (no administrator rights), in PowerShell ([details](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#windows)):

```powershell
irm https://github.com/pengupowered/pengumon/releases/latest/download/install.ps1 | iex
```

**macOS** (Apple silicon or Intel) ([details](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#macos)), **Linux** (systemd, x86-64 or arm64)
([details](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#linux)) and **Raspberry Pi** (64-bit OS only) ([details](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#raspberry-pi)) use the
same line:

```bash
curl -fsSL https://github.com/pengupowered/pengumon/releases/latest/download/install.sh | sh
```

**Container** (Docker or Podman on Linux): `docker compose up -d` with the published image, as below
([details](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#container)).

The scripts download the release, check every file against its hash, set up Pengumon's own Python, start it in the
background so it survives reboots, and open `http://127.0.0.1:8000` (this computer only until you allow more, with an
access token). On Linux and macOS the script asks for sudo once, says why, and has a `--user` option to avoid it. Run
the same command again to upgrade (your data is backed up first). Not tested yet on real Windows, macOS or Raspberry Pi
hardware: see [what has been tested where](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#what-has-been-tested-where).

Already use uv? `uv tool install pengumon`: no dependency hash check and no background service; see [the uv section](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#quick-way-uv-tool-install).

> **Important:** Anyone who can reach port 8000 can read the data and change settings until you set `PENGUMON_ACCESS_TOKEN`. The
> default listens on this computer only. See [docs/security.md](https://github.com/pengupowered/pengumon/blob/main/docs/security.md).

**Container**, the short version (Linux host with Docker or Podman, x86-64 or arm64; host networking so the probes
measure the host's own path):

```bash
mkdir pengumon && cd pengumon
curl -fsSL https://raw.githubusercontent.com/pengupowered/pengumon/main/compose.release.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/pengupowered/pengumon/main/config.example.yaml -o config.yaml
                                      # no targets in it: Pengumon finds your network path itself
docker compose up -d                  # or: podman compose up -d
```

Open `http://<host>:8000`. Rootless Podman needs unprivileged ping allowed once, and Docker Desktop (macOS, Windows) is
not supported for this: both are explained in [docs/install.md](https://github.com/pengupowered/pengumon/blob/main/docs/install.md#container), as is building from source.
`pengumon doctor` says whether probing works here and, if not, what fixes it.

## First run

Nothing to configure. When you open the dashboard for the first time, Pengumon traces the path from this computer to the
internet by itself (it takes a few seconds; nothing about your path leaves your computer at this point) and **Path health**
shows a banner, "Pengumon found your network path". Press **Review** to see each hop it found, with a role (your
network, the line, your provider, the internet), how sure it is (Sure, Likely or Unsure) and why, then **Apply**. Until
you apply, the default targets keep being probed.

Later, **Path health > Edit** opens the same editor: rename, re-role, reorder, add, remove, or press **Rediscover** to
trace again and see what changed. Details: [docs/path-editor.md](https://github.com/pengupowered/pengumon/blob/main/docs/path-editor.md) and
[docs/discovery.md](https://github.com/pengupowered/pengumon/blob/main/docs/discovery.md), which lists exactly what discovery sends where.

## Telling it about your network

The probe path is edited in the dashboard, not in `config.yaml`: **Path health > Edit** shows the hops as cards in path
order (see [First run](#first-run) and [docs/path-editor.md](https://github.com/pengupowered/pengumon/blob/main/docs/path-editor.md)). **Export** and **Import** move a
path between installs as YAML.

Everything else is set on the **Settings** page of the dashboard: devices, service checks, alerts, thresholds, your
line's capacity. `config.yaml` is only for the listen address, the port, the database path, the probe interval and the
one-time import described next. It is read once, on the first start after an install or an upgrade to 0.21, and copied
into the Settings page; after that, edits to `config.yaml` are ignored (the startup log says so). Change settings on the
page instead.

Upgrading from 0.17 or older: the first start also imports config.yaml's `targets:` and the targets added from the
dashboard into the database once, with their ids, so all history stays attached. From then on `targets:` in the file is
ignored; a fresh install leaves it out and discovers its path.

Each hop has a role (**category**) that places it on the path. Any number of each works, and none is required:

- `local`: shown as **Router**; your router or mesh nodes.
- `gateway`: shown as **Gateway**; the cable/fiber modem or the provider's box, probed on its home-side address (those
  pings never use the line, so loss there is the gateway's, not the line's).
- `isp`: shown as **ISP hop 1, 2…**; your provider's routers, in path order.
- `public`: shown as **Internet**; public resolvers, the figure is their median.
- `custom`: shown under its own name; anything else worth watching: a NAS, a VPN, a server.

The dashboard adapts to the shape of your network. A long path folds its provider hops into one node when space runs
out. A provider router that ignores ping while traffic passes through it is shown as such, not as down. Custom targets
are listed under the path. Hops can be IPv4 or IPv6 addresses, or host names probed over either; the IPv6 servers
discovery finds are their own node, "Internet (IPv6)", and on a host without IPv6 they say "IPv6 unavailable on this
host" instead of counting as down. Everything is in [docs/configuration.md](https://github.com/pengupowered/pengumon/blob/main/docs/configuration.md).

### Supported devices

- `xfinity_xb`: Xfinity / Comcast XB6, XB7, XB8 gateways. Reads DOCSIS downstream and upstream levels, SNR and
  codeword errors.
- `asuswrt`: AsusWRT routers (RT-AX, ZenWiFi, …). Reads WAN traffic, CPU, memory, clients by band and mesh nodes.
- `openwrt`: OpenWrt routers (with LuCI, or rpcd and uhttpd-mod-ubus). Reads WAN traffic, CPU, memory, model and
  clients (DHCP leases, Wi-Fi stations by band).
- `snmp`: anything with an SNMPv2c agent: routers, switches, NAS boxes. Reads WAN (or uplink) traffic, CPU, memory
  and model.

Probing works with any equipment; devices are an optional extra. Setting up each one, and adding a driver, is
described in [docs/devices.md](https://github.com/pengupowered/pengumon/blob/main/docs/devices.md).

## Alerts

Pengumon can tell you when an incident starts and ends, and when one of your own apps stops answering. Expected
conditions (busy line) never alert, a cause is not repeated within a set time ([when alerts go out](https://github.com/pengupowered/pengumon/blob/main/docs/webhooks.md#when-alerts-go-out)), and a slow receiver cannot delay the
probes. Two channels, either or both:

- **ntfy.** Enter the topic address (and the token for a protected server) on the **Settings** page, Alerts. The
  variables `PENGUMON_NOTIFY_URL` and `PENGUMON_NOTIFY_TOKEN` still win when set (for a container, put them in `.env`).
- **Webhooks.** Add them on the **Settings** page: Discord, Slack, or JSON for Home Assistant and your own scripts.
  Tokens go in headers, which may name a secret instead of holding it. The URL is kept out of the settings, in
  `secrets.json`.

The **Alerts** dialog lists the configured channels and has a **Send test** button. The payload, the three formats and
the Home Assistant recipe are in [docs/webhooks.md](https://github.com/pengupowered/pengumon/blob/main/docs/webhooks.md).

## A closer look

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/inspector.png" alt="The incident inspector: when it started, where loss first appeared, per-hop lanes before, during and after, and the modem's reading" width="640">

**Incident inspector.** Where loss first appeared, every hop before, during and after, the modem's signal and the line load.

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/drilldown.png" alt="Loss by hour of the week; one evening hour selected, and the incident list filtered to that hour" width="640">

**When it breaks.** Loss by hour of the week; click an hour to see its incidents.

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/router.png" alt="The router card: WAN traffic against the line capacity, CPU and memory, clients by band and the mesh nodes" width="640">

**Router.** WAN traffic against the line's capacity, CPU and memory, clients by band and per mesh node.

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/modem.png" alt="The cable modem card: downstream power, SNR and upstream power against their good ranges, codeword errors and channel lock" width="640">

**Cable modem.** Signal levels against their good ranges, codeword errors and channel lock, with history.

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/first-run.png" alt="Path health on a first run: the banner 'Pengumon found your network path' with a Review button" width="640">

**First run.** Discovery runs by itself and offers the path it found; the defaults are probed until you apply it.

<img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/path-editor.png" alt="The path editor with a draft from discovery: each hop with a role, a Sure or Likely badge and the reasons" width="640">

**Path editor.** Discovery's draft: every hop with a role, how sure it is and why. Edit, then Apply.

<p align="center">
  <img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/phone-live.png" width="180" alt="The dashboard on a phone: path health as a vertical chain">
  &nbsp;&nbsp;
  <img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/phone-incidents.png" width="180" alt="The incidents tab on a phone">
  &nbsp;&nbsp;
  <img src="https://raw.githubusercontent.com/pengupowered/pengumon/main/docs/images/overview-light.png" width="300" alt="The dashboard in the light theme">
</p>

## Documentation

- [Install](https://github.com/pengupowered/pengumon/blob/main/docs/install.md): Windows, macOS, Linux, Raspberry Pi and container: the install scripts, the background service, upgrading, removing, verifying a download
- [Configuration](https://github.com/pengupowered/pengumon/blob/main/docs/configuration.md): Where the files live, the listen address, the first-start import, environment variables; the settings themselves are on the Settings page
- [Probing](https://github.com/pengupowered/pengumon/blob/main/docs/probing.md): Ping and traceroute over IPv4 and IPv6 on Linux, macOS and Windows, `pengumon doctor`, the Linux sysctl step
- [Path editor](https://github.com/pengupowered/pengumon/blob/main/docs/path-editor.md): Edit the probe path in the dashboard: hops, roles, groups, Test, Apply, Rediscover, Export and Import, upgrading from 0.17
- [Discovery](https://github.com/pengupowered/pengumon/blob/main/docs/discovery.md): Finding the path and who operates each hop: what is sent where, how roles are judged
- [Devices](https://github.com/pengupowered/pengumon/blob/main/docs/devices.md): Modems and routers on the Settings page: drivers, capabilities, TLS, credentials, writing a driver
- [Diagnosis](https://github.com/pengupowered/pengumon/blob/main/docs/diagnosis.md): How outages are classified: load-aware rules, incident kinds and causes
- [Dashboard](https://github.com/pengupowered/pengumon/blob/main/docs/dashboard.md): The panels, the path chain, layout, themes, the phone view
- [Ask AI](https://github.com/pengupowered/pengumon/blob/main/docs/ai-pack.md): The troubleshooting pack for a chat assistant: what it holds, the three detail levels, device modules
- [Clients](https://github.com/pengupowered/pengumon/blob/main/docs/clients.md): Connected clients and mesh nodes, privacy settings, what is stored
- [Services](https://github.com/pengupowered/pengumon/blob/main/docs/services.md): Health checks for your own apps and their notifications
- [Webhooks](https://github.com/pengupowered/pengumon/blob/main/docs/webhooks.md): Alerts to Discord, Slack, Home Assistant or any URL: config, payload, secrets
- [Telemetry](https://github.com/pengupowered/pengumon/blob/main/docs/telemetry.md): What is stored and sent, logging, OpenTelemetry, ntfy
- [Security](https://github.com/pengupowered/pengumon/blob/main/docs/security.md): Access token, request guard, environment variables
- [Storage](https://github.com/pengupowered/pengumon/blob/main/docs/storage.md): Database layout, migrations, upgrade notes
- [API](https://github.com/pengupowered/pengumon/blob/main/docs/api.md): Data and operations endpoints
- [Architecture](https://github.com/pengupowered/pengumon/blob/main/docs/architecture.md): Components, start-up order, the live message
- [Testing](https://github.com/pengupowered/pengumon/blob/main/docs/testing.md): Unit tests, browser tests and lint
- [Supply chain](https://github.com/pengupowered/pengumon/blob/main/docs/supply-chain.md): Dependency policy, hash-locked requirements, vendored code

## Development

```bash
pip install -e ".[dev]"
PENGUMON_CONFIG=config.yaml PENGUMON_DATA=data python -m pengumon.main   # :8000 on 127.0.0.1, ./config.yaml
pytest                             # unit tests
scripts/test.sh                    # the same, in a container
tests/ui/runner/build.sh           # once: the browser test image
scripts/test-ui.sh                 # browser tests in headless Chromium, no network
```

Dependencies are hash-locked (`requirements*.txt`, generated from the `.in` files) and installed with
`--require-hashes`. `scripts/deploy.sh` is the maintainer's deploy: it adds a dependency audit (the separate `depguard`
tool, run when it is installed), a rollback image and a database backup before rebuilding. `docker compose` is all you need to run Pengumon. Releases are cut as described in [docs/releasing.md](https://github.com/pengupowered/pengumon/blob/main/docs/releasing.md).

## How accurate is it?

Pengumon sends its own ICMP (no `ping` or `traceroute` program). On Linux and macOS the probes use kernel receive
timestamps (`SO_TIMESTAMPNS` / `SO_TIMESTAMP`, as `ping` does) over one shared socket, started 25 ms apart; against a
continuous `ping` they agree within about 0.2 ms. Windows reports round trips in whole milliseconds, and the dashboard
says so. DNS timings are measured in Python and can read a few tenths of a millisecond high. Details per OS:
[docs/probing.md](https://github.com/pengupowered/pengumon/blob/main/docs/probing.md).

## License

[MIT](https://github.com/pengupowered/pengumon/blob/main/LICENSE). Chart.js, bundled in `src/pengumon/web/static/vendor/`, is also MIT-licensed.
