# Changelog

All notable changes to Pengumon are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.22.0] - 2026-11-04
### Added
- **Ask AI** (header, and the incident inspector): one Markdown text to paste into ChatGPT, Gemini, Claude or any
  other assistant, which cannot see your network itself. Pengumon writes what it measured and works out, not raw data:
  each hop's normal and how far it strayed, where along the path problems start (loss that carries on to every later
  hop, or stays at one router that only ignores pings), patterns (time of day, how often, how long), what coincided
  (line errors, a modem or router restart, a busy line, Wi-Fi clients dropping, route or settings changes), its own
  verdict and a rule-based confidence per incident, minute-by-minute close-ups of the most significant ones, and what
  it cannot see. With instructions and plain questions for the assistant. About 3 to 5 thousand tokens, even for a week.
  - Past hour, 24 hours or 7 days, or one incident; "Short" or "With more examples".
  - **Full**, **Vague** (your own routers, devices and addresses become roles such as "Router") or **Anonymized**
    (roles and numbers only, no names, brands or dates). Never included at any level: Wi-Fi client lists, incident
    descriptions, error messages, passwords. A "Who is who" key in the dialog (never in the text) maps the roles back.
  - A preview of exactly what Copy and Download give. Pengumon sends nothing anywhere. Also `GET /api/ai-pack`.
  - Tested by giving real packs to three AI models and grading their answers: times are in your own time zone (with
    the clock changes marked), the assistant is told what the measurements already rule out (so it does not ask about
    them) and which check only you can do (the gateway's own event log), and it asks only for what would change its
    answer.
- **Per-device modules:** each kind of device brings its own part of the pack (how to read its readings, its findings,
  the events that can explain an incident) and only when you have it: the cable line (DOCSIS signal and codewords),
  line load, the router, Wi-Fi and mesh, with notes for the Xfinity gateway and AsusWRT. A new driver gets this by
  implementing a small interface.
- **Device telemetry** over OpenTelemetry: the cable line's power and SNR, its error and restart counters, WAN rates,
  router CPU and memory and restarts, clients by connection and band, Wi-Fi signal, mesh nodes. Summaries, not one
  series per channel or client. Four Grafana rows in `docs/grafana/pengumon.json`, one per kind of device, that stay
  empty when you don't have it.

### Changed
- **Where a fault is, named by where loss starts.** The hop that was called "Line" on the path is the **Gateway**
  (your modem or gateway, pinged on its home-side address: those pings do not use the provider's line). Loss that
  starts there is now "Gateway or its link not answering" (the gateway itself, or the cable from the router to it),
  no longer a line fault; loss that starts at the provider's first router while the gateway answers is "Line or the
  provider's first router"; loss beyond it is the provider's network. The dashboard, alerts, the ISP dossier (which
  now tells the provider which side stopped answering and asks for a gateway check where that is the evidence) and the
  AI pack all use the same words. Stored incidents keep their codes and are shown in the right place; webhooks gain
  the cause `gateway_unreachable` (docs/webhooks.md lists every cause). Going back to an older version after a
  gateway incident was recorded needs the backup taken before the upgrade.

### Fixed
- Settings: an invalid value in the preview's days box was put back into the page as markup; it is shown as text.

## [0.21.0] - 2026-11-03

### Added
- **Settings page** (header > Settings, or `#settings`). One page for what used to live in config.yaml: outage rules,
  alerts, your line, devices and host app checks, probing, retention, privacy, discovery and display, with an
  Advanced area for the hop colours and the log levels. Every row shows its value, its default, what it changes and
  "Reset to default"; nothing changes until **Save**, and Cancel throws the draft away. A value set by an environment
  variable says "From environment" and can't be edited there. Every number has a range, and a value out of range is
  refused with a plain message.
- **Changes apply at once**, without a restart: the outage rules, alerts, the line levels, the router levels, the
  probe timeouts, the DNS lookups, retention, log levels, the Grafana link, host app checks, webhooks and devices. If
  a part can't take a change, the page says it applies at the next start.
- **Your config.yaml is imported once**, value for value, at the first start of this version. After that the file is
  only read for the listen address, the port, the database path and the probe interval, and the startup log says
  when the file and the dashboard disagree. `pengumon settings check-import` is a dry run of that import on copies
  of the database and secrets.json (it prints names and counts, never values).
- **Preview the outage rules before saving**: "With these rules the last 7 days would have had 4 incidents instead
  of 9 (and 2 alerts instead of 6)". Pengumon replays the stored 1-second samples through the real classifier in a
  separate low-priority process (a week takes about 15 seconds; it stops after a minute), and lists the incidents that
  would open or no longer open. Old incidents are never relabelled: new rules apply from the moment you save.
- **New alert rules**: send only critical incidents (a warning that becomes critical is sent then), wait N minutes
  before alerting (an outage shorter than that sends nothing), and quiet hours, with "still send critical". What
  quiet hours held back and is still going on is sent when they end, as "Ongoing since 03:12". The repeat window
  (5 minutes) is a setting too. Time zone under Display.
- **Lists on the page**: devices, host app checks, webhooks and DNS lookups each have an editor. A saved device's id
  is fixed, because its history and sign-in are kept under it; removing a device unlinks its path hops.
- **Secrets are write-only**: the ntfy address and token, webhook URLs and header values can be replaced or removed
  but never read back, and they are never exported.
- **Versions**: every Save is kept (the last 50) with a one-line summary; load an old one and save it to go back.
  **Export / Import** moves settings between installs as YAML, without secrets. Export needs the access token; it
  shows the YAML with **Copy** and a download link (Chrome blocks or warns about a download over plain HTTP).
- **New settings**: recovery confirmed after N seconds (3 by default, as before), a latency level for the ISP hops,
  the loss levels for degraded and down (15 % and 40 %, as before), the DNS loss level and cooldown, the line and
  router levels, the uptime and report levels, the daily route-check time, and the default range of the latency chart.
- **Help texts quote the values in use**, so a changed level shows in the panel's "i" text.
- **Path editor: "Warn above N ms"** per hop (under More), for a far-away server that is always slow. It changes
  that hop's colour only; the incident rules are unchanged.
- **View only**: without the token the Settings page opens read-only.
- **Telemetry for Pengumon's own health**, as metrics, logs and traces over OpenTelemetry (docs/telemetry.md):
  memory, CPU, threads, open files, asyncio tasks and event-loop lag; the work of each probe round by step; the
  unsaved-sample buffer, samples dropped, failed writes, database and WAL size, retention; dashboard clients, message
  size, slow clients dropped, history answer sizes, answers cut by a cap, rate-limit refusals; device polls the gate
  held back, backoff, rebuilds; background tasks that stopped; discovery runs, route checks and route changes, with a
  trace per run; alerts by channel and outcome (sent, failed, held for quiet hours, dropped, suppressed), the held
  queue and delivery time; settings saves and live-apply failures; preview runs. Labels are ids from your settings,
  never addresses or names. The cost is a few microseconds per probe round.
- **A Grafana dashboard in the repo**, `docs/grafana/pengumon.json`, with a row for each of those and a Service
  selector, so a second instance never mixes in.


### Fixed
Found by testing every feature since 0.15 as a user would (plain HTTP from another machine, a phone, a week of real
data), before this release:
- **Messages in view.** Notes in Settings and the path editor (a failed Import or Apply, a loaded version, a conflict)
  were shown at the top of a scrolled dialog, out of sight; Versions and Export too. They come into view now.
- **Over plain HTTP** (`http://<host>:8000` from another machine): Copy works everywhere (the browser's clipboard API
  exists only on HTTPS or the same machine), Export shows the file with Copy (Chrome blocks such downloads as
  "insecure"), and the Alerts dialog says that browser notifications need HTTPS and points to ntfy or webhooks.
- **Drafts are not lost:** Cancel, Escape, Back and reload ask before throwing away changes in Settings and the path
  editor.
- A new webhook could not be saved from the page; the incident list skipped two incidents between pages; the count
  showed "38+" instead of the real total; four dialogs ignored Escape and the keyboard; "Show in latency chart" did
  nothing on a phone; availability left out an incident still going on.
- **Pings not allowed** (a fresh install without the permission): Pengumon says "Not probing" and how to fix it, in
  the page, `/api/health` and `pengumon service status`, instead of reporting a false router outage.
- **Install:** re-running the install script to upgrade keeps the service's listen address and port; `pengumon paths`
  and `backup` take `--system` for a system install; removing a uv install first no longer leaves a service that keeps
  failing; uninstall removes the access-token file; the install prints the address other devices use.
- **Wording:** settings live on the Settings page, not in config.yaml, in the docs and the page; alerts and test
  results use plain words instead of codes ("could not connect", the incident's title). Webhook JSON is unchanged.
- **Grafana:** the dashboard in `docs/grafana/` works on a stock Prometheus (no feature flag needed).
- **Windows:** time zones (quiet hours, the daily route check) work there too: Pengumon installs Python's `tzdata`
  package on Windows, which has no time-zone database of its own.
- **For developers:** the browser tests reach the app by a non-local name, as people do (so a secure-context-only API
  fails there too), and a short Firefox pass runs beside Chromium.
### Changed
- The notifier, the host app checker and the background route check always run, so a channel, a check or the
  route check switched on from the page starts without a restart.
- A device that can't start (a bad address, a missing CA file) is reported on the page instead of stopping the start.
- **Lighter dashboard.** The flowing path wires and the pulsing status dot are animated in a way the browser runs
  apart from the page (transform and opacity), which cut the open dashboard's main-thread time by about 85 %.
  Colours and time labels are no longer recomputed for every message. A hidden tab keeps only the newest message
  and catches up when shown, so a dashboard left in a background tab costs almost nothing. Dialogs load when first
  opened, and the incident list loads 50 at a time (older ones when a view needs them): the first load is about
  20 % smaller.
- **Steadier server.**
  - If the database can't be written (a full disk), unsaved samples are capped at about 14 minutes instead of
    growing without end, and the dashboard keeps updating.
  - A device that fails for weeks keeps retrying at its longest backoff; before, after about 43 days of failures its
    polling stopped until a restart.
  - Saving a device's settings during one of its polls no longer causes a second poll right after: the minimum gap
    between polls carries over.
  - Discovery and the daily route check send a configured device nothing: its driver alone talks to it.
  - A time in the hour skipped in spring (daily route check, quiet hours) now comes when the clock passes it, not an
    hour early.
  - History answers are capped at 50,000 rows and 60 requests a minute; a device's raw history at its newest 48 h.
  - Shutting down with the telemetry collector unreachable takes at most 3 s more, within podman's stop grace.
- The header's "Copy the LAN address" works over plain HTTP too (the browser's clipboard API exists only on HTTPS
  or localhost; it failed silently from other machines).
- **For developers.**
  - The unit tests run in parallel (pytest-xdist) in about 35 s instead of 96, and the browser tests in about 90 s
    instead of 320.
  - A test that hangs ends the run after 60 s with every thread's stack.
  - The test containers have a memory cap and a time limit that holds even when the script is killed, and they no
    longer leave an old image, volume or network behind on every run (docs/testing.md, "Limits").

## [0.20.2] - 2026-11-03

### Fixed
- The README is the project description on PyPI, where relative images and links were broken and the GitHub alert
  showed as text: every image and link is now absolute, and a test keeps it that way.
- docs/install.md: the `uv tool install --exclude-newer "7 days ago"` example hid Pengumon's own new release for a week;
  it now keeps the 7-day cooldown for dependencies only.
- Code scanning findings: the UPnP search during discovery listens only on the LAN address it sends from (not on
  every interface); the loss-by-hour panel takes its plain text from the page instead of a tag-stripping regex; two
  tests made stricter.

### Changed
- The README says `uv tool install pengumon` works, and shows a PyPI version badge and the Release workflow status
  (the CI workflow no longer runs on `main`).
- Releases: the Release workflow publishes the files that the CI run already tested, without running the tests again.

## [0.20.1] - 2026-11-03

### Fixed
- The release could not publish: the job that signs the image's provenance into the registry had no registry login.
  0.20.0 has no published release; 0.20.1 is the first one with the image, the install files and the PyPI package.
- The container test reports every failing check readably on GitHub, waits for the container network to settle
  before starting the browser, and waits on conditions instead of fixed times.

## [0.20.0] - 2026-11-02

### Added
- **Route-change checks.** Discovery reruns quietly once a day and about ten minutes after an incident ends; a change
  of your network's or provider's hops, confirmed by a second run, shows as "Your path changed · Review" and is sent
  as one alert (no addresses in the text). `discovery: {background: true, asn_lookup: false}` in the config.
- **Path versions**: every Apply is kept (the last 50) with a summary of what changed; an address change is marked in
  the hop's history chart.
- **Preview** before Apply: new or changed hops are probed for 30 s and show real figures.
- Discovery finds **a modem or ISP gateway that isn't on the traced path** (a modem in bridge mode): it checks a few
  common gateway addresses once and offers one that answers as "Line / gateway (not on the traced path)".
- **View only without the token** (`PENGUMON_VIEW_WITHOUT_TOKEN=1`, off by default): with a token set, people without
  it can look at the dashboard, and every change asks them to sign in.

### Fixed
- A long verdict title could push the header wider than the window; it now ends in "…" with the full text on hover.
- README and docs read well on GitHub and on phones (no tables wider than the page).

### Changed
- **Path editor, clearer.** Every hop shows what Pengumon measures there now ("11.2 ms · 0% loss (last minute)",
  "Not answering", "No reply · ignores ping (not counted as an outage)", "IPv6 unavailable on this host"), updated
  while the editor is open; a new hop says "Not probed yet" with Test, and **Preview** probes the new hops for 30 s
  before you apply. The path is drawn at the top like Path health: click a node to edit it, **Merge** / **Split** on
  the links replace the "one node with hop N" box. Each role says what it changes. A calmer card: name and measurement,
  then address and role, then the controls.
- **Devices only where they mean something.** The device line is on hops of your network and the line only, as a
  sentence with the device's own status ("Readings from: ASUS router · RT-AX88U · Healthy · CPU 12% · 23 clients ·
  Change", or "No device connected · Connect") and a link to its Router or Line panel; Path health's Router and Line
  nodes name it in their tooltip. Human labels (`/api/devices` gains `label`). A hop is linked by itself when exactly
  one configured device is at its address: on the first import, once at the start for a path stored by 0.19, for a
  new hop on Apply, and when the editor opens. "No device" is remembered (`device_unlinked`) and never undone.
- **Edit a hop's address in place** ("Change address"): the hop keeps its id and history, for a renumbered router.
- **Drop "Hop N:" prefixes** from names in one click (Path health numbers the hops itself).
- **Earlier paths**: the editor lists the applied versions; **Restore the previous path** loads one as a draft.
- Path health shows **"Your path changed · Review"** when the route check finds a change.

### Removed
- The "Ignores ping" setting. Whether a hop ignores ping is observed (a later hop answers), not set: the prober decides
  alone, an `ignores_ping` key in an older export or a stored hop is ignored, and stored values are dropped at the
  first start.

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

[Unreleased]: https://github.com/pengupowered/pengumon/compare/v0.22.0...HEAD
[0.22.0]: https://github.com/pengupowered/pengumon/compare/v0.21.0...v0.22.0
[0.21.0]: https://github.com/pengupowered/pengumon/compare/v0.20.2...v0.21.0
[0.20.2]: https://github.com/pengupowered/pengumon/compare/v0.20.1...v0.20.2
[0.20.1]: https://github.com/pengupowered/pengumon/compare/v0.20.0...v0.20.1
[0.20.0]: https://github.com/pengupowered/pengumon/compare/v0.19.0...v0.20.0
[0.19.0]: https://github.com/pengupowered/pengumon/compare/v0.18.1...v0.19.0
[0.18.1]: https://github.com/pengupowered/pengumon/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/pengupowered/pengumon/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/pengupowered/pengumon/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/pengupowered/pengumon/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/pengupowered/pengumon/compare/v0.14.2...v0.15.0
[0.14.2]: https://github.com/pengupowered/pengumon/releases/tag/v0.14.2
