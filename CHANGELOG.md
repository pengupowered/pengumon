# Changelog

All notable changes to Pengumon are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.19.0] - 2026-11-02

### Added
- **Network discovery.** A fresh install finds its own path: traces to 1.1.1.1, 8.8.8.8 and 9.9.9.9 (and the IPv6
  path when the host has IPv6), with a best guess of who owns each hop (your network, your provider, the internet),
  its confidence and the reasons. Device checks without logging in (web page, UPnP, cable-modem page; SNMP only when
  ticked) suggest a driver. The optional operator lookup (who runs each public hop) is explained where you tick it.
- **Path editor.** "Edit" on Path health: rename, change a hop's role, remove (history kept, or deleted on request),
  add by address, group hops into one node, override "ignores ping", link a device, test a hop, then Apply. Export
  and import as YAML. "Rediscover" shows what changed since the path was applied.
- **First run.** Discovery starts by itself and a banner offers the result for review; nothing is probed from it until
  Apply.
- **Install without a container** on Windows, macOS and Linux (Raspberry Pi 64-bit included): `install.sh` and
  `install.ps1` install a pinned uv, Python 3.12 and the hash-locked dependencies, checked against the release's
  SHA256SUMS, then `pengumon service install` registers a background service (a hardened systemd unit, a macOS
  LaunchDaemon, or a Windows logon task). Upgrades back up the data first.
- Releases publish the wheel to PyPI (after an approval step), attach the wheel, lock, install scripts and checksums,
  and carry build provenance attestations.
- An "Internet (IPv6)" node on Path health and line on the latency chart when the path has IPv6 servers.

### Changed
- The probe path lives in the database. On the first start of this version the targets from config.yaml (and those
  added in the dashboard) are imported once, keeping their ids and history; config.yaml `targets:` is ignored after
  that. The example config has no active targets, so a new install goes through discovery.
- Trace labels no longer guess equipment ("Hop 3", "Hop 1 (private)" instead of "ISP Node / CMTS").
- The dependency lock covers every platform.

### Security
- Path import refuses YAML anchors, aliases and merge keys and deeply nested files; discovery refuses XML with a DTD in
  any encoding, only follows UPnP answers from routers on the traced path, keeps configured gateways to one request,
  and never sends this host's own public address to the operator lookup. Upgrade backups run as the service account
  and never follow symbolic links.
- Without an access token, any device on the LAN can now change the probe path and delete a removed hop's history:
  set a token if the network is shared (docs/security.md).

## [0.18.1] - 2026-11-02

### Fixed
- IPv6 traces broke off after a few hops: a provider network dropped bursts of ICMPv6 probes (only the first probe to
  each router was answered and the target never replied). IPv6 traces now send one probe every 100 ms (about 5 s for
  a full trace) and reach the target; IPv4 traces keep their speed.
- On Linux ping sockets, a router's ICMP error could make the next probe's send fail ("No route to host"); the
  socket's pending error is cleared before each send.

## [0.18.0] - 2026-11-02

### Added
- IPv6 on every platform: echo and traceroute over ICMPv6 (Linux ping or raw socket, macOS datagram socket, Windows
  Icmp6SendEcho2). Host-name targets take `family: 6` to probe over IPv6 (default 4). The add-target and trace
  dialogs have a family choice.
- "IPv6 unavailable on this host": when the host or its network has no IPv6 route, IPv6 targets are shown as not
  probed instead of down, and come back by themselves when IPv6 does. `pengumon doctor` reports IPv6 too.

### Fixed
- The image build could reuse a cached layer holding another source tree's wheel; the wheel is now copied in, and
  `scripts/deploy.sh` and the container test refuse an image whose package differs from the source tree.
- IPv6 targets, which stopped being probed in 0.17.0, are probed again.

## [0.17.0] - 2026-11-02

### Added
- Ping and traceroute are built in, without admin rights, on Linux (ping socket or raw socket), macOS (ICMP datagram
  socket) and Windows (IcmpSendEcho2). No `ping`, `traceroute` or `tracert` program is needed any more.
- `pengumon doctor`: which probe method is in use and its resolution, a loopback probe, the first router, the data
  directory and listen address, and the one command to run when ping isn't permitted (Debian-family Linux).
- The dashboard says "1 ms resolution" where the platform measures only whole milliseconds (Windows).
- CI tests the app natively on Linux x64 and arm64, macOS and Windows, traces through a simulated network of
  routers, and turns every test failure into a readable annotation.

### Changed
- Traceroute runs in the app: 3 probes per hop, all hops at once, one trace at a time app-wide.
- The container image no longer contains `iputils-ping` or `traceroute`.
- Echo replies are accepted only when they carry the exact data that was sent (forged or stray replies can't make
  a dead hop look alive), and probe ids are random.
- icmplib is no longer a dependency.

### Fixed
- Windows: the static file server could serve panel templates (backslash paths), and secret files were read and
  written in text mode.
- The alerts dialog could show an older answer over a newer one on a slow server.
- `/api/info` no longer blocks the server on a host-name lookup.

## [0.16.0] - 2026-11-02

### Added
- `pengumon` command: `pengumon` (or `pengumon run`) starts the server; `pengumon version`, `pengumon paths` (where
  the config, data and listen address come from) and `pengumon backup` (a consistent copy of the database and
  secrets, safe while running).
- Pengumon installs as an ordinary Python package (preview): the wheel now carries the web files and declares every
  dependency. See "Running without a container" in docs/configuration.md.
- CI builds the wheel once, tests it natively on Linux, builds the image from it on amd64 and arm64, and tests the
  image (non-root, read-only root, health, ping, a browser check). Releases publish that same wheel and image.

### Changed
- Files live in one data directory: `PENGUMON_DATA` (the image: `/app/data`; natively the OS's per-user data
  folder), config at `PENGUMON_CONFIG` (default: `config.yaml` in the data directory). A relative `database.db_path`
  keeps its old meaning.
- Outside a container Pengumon listens on 127.0.0.1 unless `PENGUMON_HOST` or `server.host` says otherwise. The
  image sets `PENGUMON_HOST=0.0.0.0`, so containers are unchanged.
- The image is built from the wheel (no source tree inside) and the compose files run it with a read-only root
  filesystem.
- `uvicorn` without its `[standard]` extras: three native dependencies fewer (uvloop, httptools, watchfiles).
- `scripts/deploy.sh` backs up with `pengumon backup`, so it follows the configured database location.

## [0.15.0] - 2026-11-02

### Added
- The Internet node in Path health (and a folded ISP node on narrow screens) opens a history of the whole group:
  every member's latency in its own colour, the median the node shows, per-member loss and uptime, and a switcher to
  one member. A path node's history opens on Live and shows the node's own figure, and every figure names its window.
- Incidents show how long probes were actually lost ("Loss 7s · incident 12s"), not only the detection span.
- New device drivers: `openwrt` (ubus JSON-RPC) and `snmp` (SNMPv2c, any router, switch or NAS with IF-MIB and
  HOST-RESOURCES-MIB).
- Webhook alerts next to ntfy, in `json`, `slack` or `discord` format (Home Assistant works with `json`), with a
  "Send test" button in the alerts dialog.
- Mesh nodes show whether they are online and how they connect to the main router; an offline node is a router
  warning. Clients show a device type where the router reports one.
- Sign out and "Sign out everywhere" when an access token is set. Sessions now expire on the server
  (`PENGUMON_SESSION_MAX_AGE_DAYS`, default 30), and a page whose sign-in ends goes to the sign-in page.
- Published container image (`ghcr.io/pengupowered/pengumon`, amd64 and arm64) and `compose.release.yaml` to install
  without building; CI runs the unit and browser tests on every push.

### Changed
- Hops that ignore ping are decided once, on the server, and every view treats them alike: Uptime, the target history,
  the report, When it breaks, the ISP dossier and incidents no longer count them as outages.
- The incident inspector and the report use the same short names as the rest of the page (full names on hover).
- Default target colours: members of one group always get distinct colours; the example config no longer pins them.
- `scripts/deploy.sh` warns and continues when depguard is not installed (`PENGUMON_REQUIRE_DEPGUARD=1` keeps it
  required).

### Fixed
- The modem's codeword-error ratio is computed even when one channel does not report error-free codewords (before,
  it was never computed on such modems, and grading fell back to error rate alone).
- The image no longer copies a local `config.yaml`; it carries the example and compose mounts yours.

### Upgrade notes
- With an access token set, everyone signs in once more after upgrading (the session cookie format changed). Rolling
  back to an older image makes old copied cookies valid again: change the token after such a rollback.

## [0.14.2] - 2026-11-02

First public release.

Pengumon is a self-hosted network monitor that shows where your internet breaks: your Wi-Fi, your modem, your provider
or the internet beyond. It runs as one container with one SQLite file.

- Probes every stage of the path (this host, router, modem, each provider hop, public resolvers) about once a second
  and shows the median latency, loss and jitter of each, live on a dashboard that also works on a phone.
- Classifies every outage as a problem in your home network, the line, the provider or beyond, and records an incident
  with per-hop evidence, the modem's signal and the line load at the time. Slowness while the line is busy is marked as
  expected, not reported as a fault.
- Patterns over time: loss by hour of the week, per-target uptime, a reliability report and an ISP dossier to send to
  support.
- Optional device drivers: DOCSIS levels and codeword errors from Xfinity XB6/XB7/XB8 gateways; WAN traffic, CPU,
  memory, clients and mesh nodes from AsusWRT routers.
- HTTP health checks for your own self-hosted services, with incidents and ntfy notifications.
- Optional access token, OpenTelemetry export and Grafana links.
- Install from the published container image (`ghcr.io/pengupowered/pengumon`, amd64 and arm64) with
  `compose.release.yaml`, or build from source.

[Unreleased]: https://github.com/pengupowered/pengumon/compare/v0.19.0...HEAD
[0.19.0]: https://github.com/pengupowered/pengumon/compare/v0.18.1...v0.19.0
[0.18.1]: https://github.com/pengupowered/pengumon/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/pengupowered/pengumon/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/pengupowered/pengumon/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/pengupowered/pengumon/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/pengupowered/pengumon/compare/v0.14.2...v0.15.0
[0.14.2]: https://github.com/pengupowered/pengumon/releases/tag/v0.14.2
