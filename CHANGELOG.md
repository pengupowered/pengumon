# Changelog

All notable changes to Pengumon are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/pengupowered/pengumon/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/pengupowered/pengumon/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/pengupowered/pengumon/compare/v0.14.2...v0.15.0
[0.14.2]: https://github.com/pengupowered/pengumon/releases/tag/v0.14.2
