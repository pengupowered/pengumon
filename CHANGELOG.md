# Changelog

All notable changes to Pengumon are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.27.0] - 2026-11-11
### Added
- **Device names in Pengumon.** A device whose router name is unhelpful (a game console the router calls "EXAMPLE
  NETWORK TECHNOLOGY CO") can be given a name with the pencil in its traffic dialog. It is shown instead of the
  router's name on the Internet use card, in the dialog, in Local network and in the incident inspector, wherever names
  are shown (never when Settings > Privacy hides them, and never in the ISP dossier, the Ask AI pack or MCP). Schema v20
  (`client_names`); `PUT /api/devices/{id}/clients/{key}/name`.
- **Internet use: which devices used the internet, and for what.** On an ASUS router with its Traffic Analyzer on,
  Pengumon reads the router's own counts of download and upload per device and hour, and per app over 24-hour windows
  (the router keeps app figures no finer). A new **Internet use** card shows the last 24 hours or 7 days per hour, or
  the last 30 whole days per day, the top devices with their main app ("mostly Sony PlayStation") and the top apps of
  the same range (over 7 and 30 days, the sum of the router's count for each whole day, midnight to midnight, that
  Pengumon read); a device opens to its own traffic over the same 24 hours, 7 days or 30 days (buttons for the others),
  with its apps over that range and its apps day by day. Hours the router has not published yet (it publishes each hour shortly after it ends) are
  marked as such, not drawn as zero. An incident's details say which devices were using the internet in its hours, so a
  slow evening caused by a game download is told from a provider problem. The router is asked once an hour by default
  (device option `traffic_interval_seconds`, from 60 s; a read that finds nothing new costs one request); Settings >
  Retention > Keep router traffic for (400 days). `GET /api/devices/{id}/traffic`, `.../traffic/clients/{key}`, and an
  MCP `traffic` tool. The ISP dossier, the Ask AI pack and MCP describe a device by its kind and maker only.
- **Router uptime, CPU temperature and WAN link speed** on the router card and in its history (ASUS), with **router
  events**: a restart (from the uptime) and a WAN link that changed speed or went down. Events are listed on the router
  card and in the details of incidents around them (`GET /api/devices/{id}/events`). The client list says which app a
  device mostly uses.
- **SNMPv3** for the `snmp` driver, beside SNMPv2c: noAuthNoPriv, authNoPriv and authPriv with HMAC-SHA-1 or SHA-224 to
  SHA-512 and AES-128 (no MD5 or DES). The privacy password is a new device secret (dashboard, environment or secret
  file), never shown again. Tested against net-snmp 5.9. Adds the `cryptography` package, pinned to an exact version.
- **Probe a hop by TCP or HTTP(S), not only ping.** For a server or router that drops ping, a hop's **Probe by** in the
  path editor can be **TCP connect** (a handshake to a port, no data sent; judged like a ping hop) or an **HTTP(S)
  request** (one `HEAD` or `GET`, only the status line is read; the hop is always **Other**, charted and never blamed
  for an outage). Test and Preview use the hop's own method, Path health and Hop details say how each hop is probed,
  changing a method is kept as an event on the hop's history, and the reports, the Ask AI pack, MCP and the metrics
  (a new `probe` label) name the method. Limits: at most 8 such hops; no loopback, link-local or other special
  destinations (checked again at every probe, so a name pointed at this computer is refused); ports on the browsers'
  bad-port list are refused; TLS is always verified; no redirect is followed. docs/probing.md,
  docs/security.md.
- **Download a backup of the database** (Settings > About this install, `GET /api/backup`): a consistent copy of the
  history and settings, without any password, token or webhook address. Only for someone who may change settings; two
  in 10 minutes, one at a time.
- **A read token for AI assistants and scripts** (Settings > Sign-in > Read token). It reads what a viewer may read
  and may call the read-only MCP endpoint, and can change nothing. Make, replace and remove it with the current
  password; it is shown once. Use it instead of the password in an assistant's configuration
  (docs/mcp.md).
- `database.incident_retention_days` (Settings > Retention > Keep resolved incidents for): resolved incidents can be
  deleted after that many days; 0, the default, keeps them forever, and an open incident is never deleted.
- `database.slot_retention_days` (Keep 10-second history for): the latency chart's 10-second history now has its own
  setting, 7 days by default, instead of following the raw samples.
- Every list in Settings has a longest length (50 devices, 100 host app checks, 20 webhooks, 50 DNS lookups, 30 custom
  themes); more is refused with "too many".
- `PENGUMON_TRUSTED_PROXIES` for a reverse proxy whose `X-Forwarded-For` should be believed (see Upgrade notes).
### Changed
- **Faster dashboard.** Opening the page fired a dozen reads that waited for each other on one database connection
  (300-500 ms each behind the slowest); reads now run on up to three connections. Long ranges are read from new
  10-second and quarter-hour summaries instead of minutes or raw samples: a 6-hour latency history answers in about a
  fifth of the time, the 30-day report and loss grid in about a twentieth, and a year-long report stays fast as history
  grows. The report, loss grid and uptime strips are also kept for the rest of the minute.
- **The live stream sends only what changed**: each second's message to the dashboard carries the new probes, not the
  last 60 of every target, so each message is much smaller, and the latency chart redraws less.
- **History is kept at two levels**: per-minute history for 90 days by default (was 400) and quarter-hour history for
  400 days (Settings > Retention > Keep quarter-hour history for); charts and reports older than the per-minute history
  use 15-minute detail. A per-minute setting stored at the old default of 400 days goes to 90 at the upgrade (any other
  stored value stays); nothing is deleted then. Speed tests follow the quarter-hour setting. A database ends up much smaller than it
  would have after a year at the old default.
- The upgrade builds the new summaries from what is stored, once: about half a minute for a week of samples, during
  which the dashboard does not answer.
- **Sign-in is stricter.** The password is stored as a scrypt hash, never as text, and the sign-in cookies are signed
  with a random server secret. Changing or removing the password, making, replacing or removing the read token, and
  turning on "Others may look, but not change anything" now ask for the current password (also when the password comes
  from `PENGUMON_ACCESS_TOKEN`), so a browser left signed in cannot leave access behind. A new password or removing it
  removes the read token. Failed sign-ins are counted per address (an IPv6 address by its /64) and for all addresses
  together: 5 a minute from one address, 20 a minute from all, and an address that has signed in recently is exempt from
  the 20 (docs/security.md). With HTTPS, every answer carries `Strict-Transport-Security`.
- **Viewers and the read token see less.** The settings a viewer opens come without a device's user name and options, a
  check's path and query, a webhook's headers and secret hosts; kept versions of the settings and of the path, and the
  backup, need the password; alert channels come without their hosts, a device's address without its path, and a failed
  device poll in fixed words instead of the device's own message.
- **MCP tools show less.** Hops are named by role, not by name or address, and every text in a tool's answer is scanned
  for home addresses, MAC addresses, home host names and passwords in addresses. The Ask AI pack's final scan finds a
  MAC address in every written form, any host name at Anonymized, and secrets in address queries.
- The incident dossier is limited to 20 a minute and one at a time, the incident inspector to 60 a minute, and the
  backup to 2 in 10 minutes. The live connection takes at most 64 dashboards, and a page can send at most 4096 bytes on it.
- The compose files drop every capability but `NET_RAW` and limit memory (512 MB) and processes (256). The image no
  longer contains `libcap2-bin` (the tool that sets the raw-socket capability is installed and removed again in the build).
- Everything a device, a discovery target, a host app check, ntfy, a webhook or a speed test server answers is read
  with a size limit and a total time limit, with no compression asked for; a webhook redirected from `https` to `http`
  is refused, and the cable modem driver follows redirects only on its own host.
- A device's address and login name no longer appear in logs and traces that are exported (OpenTelemetry); the page
  and the API still show the full error text.
- **Faster dashboard start.** The page lists its start's modules for the browser to fetch together, the panels' and
  dialogs' stylesheets come as one file, and text files are compressed once and kept: a cold load on localhost made 117
  requests instead of 146. A large history answer is shaped off the main loop, a history read writes the sample buffer
  only when its window reaches it, the retention cleanup and the deletion of a removed hop's history run in bounded
  batches so the probes' writes get in between, and the DNS probe no longer reads the resolver configuration every time.
- Dialogs, the Back button and Settings: every dialog (Settings and the path editor too) pushes one history entry that
  is marked spent when the dialog closes, so Back never lands on a dead entry and a close followed by a reload or a
  link is not undone.
- Several built-in themes have more readable text; a custom theme with unreadable colors gets a warning in Settings
  (never a refusal), and two custom themes cannot share an id.
- A probe round in Settings must be above 0 and at most 60 seconds; Infinity and NaN are refused in settings, the
  line capacity and the history time parameters; a URL with a port that is not a port is refused.
- The install scripts keep the working program aside (as `venv.old`) while the new one is installed and put it back if
  anything fails or you interrupt them; on Linux and macOS they refuse a data folder inside the program folder and, for a
  system install, a program folder that is not owned by root or can be written by others. `scripts/deploy.sh`
  names its rollback point to the second and refuses to reuse one. The release workflow puts the version and `latest`
  tags on the image only after its provenance attestation exists.
### Fixed
- The latency chart no longer shows short gaps in the Router and Gateway lines after the page loads. The stored probes
  it starts from were grouped by whole second, and a probe round (its probes 25 ms apart, drifting slowly against the
  clock) put a target in the next second whenever its place crossed a half second; they are now grouped by probe
  round, and a round only partly stored at either end of the window is left out.
- Upstream and downstream DOCSIS channels with the same number no longer share an error baseline, which counted
  the whole error history of a downstream channel as new errors.
- ntfy alerts with a title that is not plain ASCII are delivered.
- The uptime strips count a drop that began before the window and reaches into it.
- An incident is stored before it is active, so a busy database no longer leaves an incident that was never recorded;
  it starts at its first faulty round and never ends before it started.
- A dashboard whose live connection died silently now notices and reconnects; a sign-in change no longer leaves a page
  stuck after the server closes its connection.
- Settings: a setting that applies live is no longer shown as failed; a failed save puts back the secrets it replaced;
  the secrets of removed items are deleted only after the save succeeded; a value is stored as it is read, not as it was
  sent; a list item of the wrong shape is refused with a message instead of an error; the focused field survives the
  redraw after a sign-in change; and many smaller dialog problems (the generated password and typed passwords no longer
  stay in a closed dialog, Escape clears the Add target form, an older, slower answer no longer replaces the dossier
  window you picked, and others).
- The MCP report's worst hours of the day were shown as 1970 dates; the incident inspector looked at only the first 10
  seconds of an incident that was still open; the Ask AI pack and the MCP incident tool leave out a reverse host name
  that is not shaped like a host name.
- Discovery: an address that answers at two hop counts is one hop; a hop probed by TCP or HTTP is never reported as gone;
  a route-change notice can be dismissed during a check; a clock set back no longer runs the daily check early.
- A raw ICMP socket that keeps failing to read pauses its reader instead of spinning the event loop.
- Speed test: a direction cut off at its time limit counts what it moved.
- The ASUS driver keeps at most 2048 clients and 32 mesh nodes from one answer; the SNMPv2c driver drops a datagram that
  does not decode instead of failing the request; UPnP control addresses longer than 100 characters are read whole.
### Security
- An SNMPv3 device whose settings were being changed could, for one poll, fall back to SNMPv2c and send its password in
  clear text; a v3 device now never speaks v2c, and settings an agent would refuse (a password under 8 characters, a user
  name over 32 bytes) are refused when saved.
- A dashboard connection could make the server resend the full state to every open dashboard as fast as it asked; a
  resync now goes to that connection only, at most once every 2 s.
- Malformed datagrams from someone spoofing an SNMPv3 agent no longer fail a request; the report routes build at most
  two answers at a time and are rate limited, so they cannot hold every database connection.
- A broken or hostile router (or something answering in its place) could freeze Pengumon for seconds to minutes per
  poll with a large, malformed answer: some of the ASUS driver's text patterns took time that grew with the square of
  the answer's size. The router's answers are now read up to a size limit (4 MiB for the status
  answer, 1 MiB for the other pages) and parsed in linear time; absurdly long numbers, deep nesting and impossible
  uptimes are treated as no value instead of failing the poll or the event list.
- Names a device reports can no longer put fake time markers into the Ask AI pack (braces in names become
  parentheses).
- Failed sign-ins are throttled, the password is stored only as a hash, cookies are signed with a server secret, a page
  on another site can no longer submit sign-in guesses from your browser (`Origin` is checked on `POST /login`), and
  `X-Forwarded-For` is ignored unless `PENGUMON_TRUSTED_PROXIES` names the proxy.
- A device or other server that answered with a huge or endlessly slow reply, or a compressed answer that expands
  enormously, could hold or exhaust Pengumon; every such answer is now bounded in size and in total time, and the parsers of the
  Xfinity status page, the page titles discovery reads and the redaction rules take time in proportion to the text.
- Path probes cannot be aimed at this computer's loopback or link-local addresses (a cloud metadata service, for
  example), also through a name that resolves there, nor at NAT64 and IPv4-compatible forms of them.
- The backup's temporary folder is opened without following a link, refused unless it is ours, and set to mode 0700.
- Theme files: ASCII digits only in value patterns, a stricter lint with its limits stated.
### Upgrade notes
Everyone signs in once more after the upgrade (the cookies have a new format). At the first start the stored password
is converted to a hash in `secrets.json`; to roll back to an earlier version afterwards, restore `secrets.json` from the
backup taken before the upgrade, along with the database (the converted file holds a placeholder an older version
cannot use, so it would keep the dashboard locked). Hops saved from now on carry the new probe keys (`probe`, `port`
and, for HTTP, `http_scheme`, `http_path`, `http_verb`); an older version ignores them and probes the hop by ping.
`X-Forwarded-For` is no longer trusted by default: behind a reverse proxy, set `PENGUMON_TRUSTED_PROXIES` to the
proxy's address (in the compose files, in the `environment:` list of an override file) or every client counts as the
proxy.

## [0.26.0] - 2026-11-08
### Added
- **Outages: drops that belong together are one event.** A line that drops, comes back for a few minutes and drops again
  used to make one incident per drop (one 25-minute night at the provider made 7). Drops at the same or neighboring
  places of the path that start within 10 minutes of the last one ending are now one **outage**: the Incidents card shows one row ("7 drops ·
  down 14 min") that opens to the drops, and the report card, the ISP dossier, the Ask AI pack and the MCP `incidents`
  tool count outages and their total down time. Incidents stay the record underneath, unchanged; the recovery rule is
  not changed. Settings > Outage rules > Outages sets the gap (0 turns grouping off); a change regroups the history.
- **"Likely maintenance"**: an outage at the line or the provider's hops (nothing else) that starts from 00:00 up to
  06:00 (a setting) is labeled as likely planned work. Only a label: the downtime counts in full.
- `GET /api/outages`; `outage_id` on every incident in `GET /api/incidents`; `outage_id` and `drops` in webhook payloads.
- **Back closes a dialog.** With a dialog open on the dashboard (Dossier, an incident's details, Ask AI, Layout, ...),
  the browser's or phone's Back closes it and stays on the page, instead of leaving Pengumon.
- **Process telemetry on macOS and Windows**: Pengumon's own memory, threads and open handles are exported there too
  (they were Linux only).
- A standing line says when the last background route check failed, why, and from when the path shown is.
### Changed
- **Alerts per outage**: a flapping line sends one alert when the outage starts and one "resolved" when it is over (the
  gap after the last drop, so a few minutes later than before), not one pair per drop. The Alerts preview counts the
  same way. With grouping off, alerts are as before.
- The container is built on Python 3.12.16 (python:3.12-slim of 2026-11-05). `scripts/deploy.sh` and the test scripts
  now refresh the image's Debian security updates at least once a day; before, a local build could reuse an old cached
  upgrade and miss fixes (the published images were not affected).
- No speed test provider uses a proxy now (M-Lab used the environment's proxy settings).
- Amber CRT draws the speed history as lines, not steps (a chart can keep its own curve under a theme's chart style:
  `ownCurve`, docs/themes.md).
- Settings > Alerts > Send alerts for: "Outages only" is now called "Critical incidents only" (same setting, same
  behavior; "outage" now means a group of drops).
- With more than 500 incidents, the Incidents card says that it lists the newest 500 and where the older ones are.
- Settings: the "changed elsewhere" note names each setting in full; a problem inside a collapsed list card is shown in
  words, and a line above the buttons says why Save is off, with a link to the first problem; the Preview says only
  what differs for an outage still going on.
- The IPv6 internet line in the latency chart is dashed, so it is not told from the IPv4 one by color alone, and "Not
  probed" fits on one line in a long path.
- The install scripts say why a backup before an upgrade failed (the exit status when the backup printed nothing).
### Fixed
- A long run of "line busy" skips no longer moves the speed test schedule.
- Opening the Speed card no longer writes to the database.
### Security
- A speed test server could freeze Pengumon (probes and dashboard included) for seconds to minutes by sending its answer
  in tiny chunks; the client's parser is now linear and stricter.
- With a wrong router password, a speed test retried the router login about once a second, which can trigger the
  router's lockout; a refused login now stops those reads until a poll works.
- With the log level at debug, the dashboard's sign-in cookie or bearer token could appear in the console log (the
  handshake lines of `websockets`; the OpenTelemetry export takes only uvicorn's warnings, so it did not carry them); the value of a `Cookie`, `Set-Cookie`, `Authorization` or `Proxy-Authorization` header is now
  replaced by `[redacted]` at any level. An M-Lab test's access token is in the URL of its connection, which the
  `websockets.client` logger repeats at debug: that logger now stays at `info` unless `PENGUMON_LOG_LEVELS` names it.
- The speed test's HTTP client refuses line breaks in request headers and a malformed status code; a LibreSpeed
  server's address is left out of what AI assistants see whatever its case.

## [0.25.2] - 2026-11-07
### Changed
- **The speed test runs as often as you like**: Settings > Speed test > Run every takes any whole number of hours, days
  or weeks, from 1 hour to 365 days (a week for weekly, 30 days for about monthly), instead of 1, 3, 6, 12 or 24 hours.
  Installs that test every few days or weeks spread over the days. `speedtest.interval_hours` stays the key (1 to 8760).

## [0.25.1] - 2026-11-07
### Changed
- **The speed test measures the line.** While a direction runs, Pengumon reads the router's WAN counters about once a
  second (through the same session its polls use) and records the line's best 3-second rate: the test and the
  household's traffic, what the line carried while it was full. That is the headline speed when the router gives one
  ("measured by your router"), else the test's own ("test only"); the card, reports, MCP, AI pack and slow-line alert
  judge the headline, and say when the test itself got far less because of other traffic on the network.
  `measured_by` in `GET /api/speedtests` and the `speed_tests` tool.
- Each direction runs for about 8 seconds of data or its data limit, whichever comes first, on 8 streams, through a
  leaner HTTP client (about half the CPU per MB: one core no longer caps the test near 800 Mbps). Data per test (down)
  gains 500 MB and 1000 MB.
### Fixed
- A speed test that cannot connect while a proxy is set for Pengumon says that the test connects directly (a test
  through a proxy would measure the proxy, not the line).

## [0.25.0] - 2026-11-07
### Added
- **Scheduled speed test** (off by default; Settings > Speed test). Measures download and upload on a schedule (every 1
  to 24 hours, at a random minute of your own, optionally only inside a time window) and the delay a full line adds
  (bufferbloat grade A+ to F, from the probes' latency before and during the test). Three providers: Cloudflare (the
  default), M-Lab NDT7 (publishes your IP address with each result, as M-Lab does for every test) and your own LibreSpeed
  server. Light on metered lines: hard limits on the data per test (10 to 250 MB down, 5 to 50 MB up), an optional monthly
  limit, a live estimate of the data a month in Settings, and a test waits while the line is busy. A test too short to
  measure a fast line says so and how much data would do, and is left out of the reports' figures. A **Speed** card
  (last result against your plan speed, history, Run now), a speed section in the ISP dossier and the AI pack, an optional
  alert when a test measures under a share of your plan (off by default; event `speed_slow`), metrics and a Grafana row.
  While a test runs, its own delay is not an incident ("Speed Test Running") and an open incident is held, not resolved.
  `GET /api/speedtests`, `POST /api/speedtests/run`. Schema v17 (`speed_tests`). docs/speedtest.md.
- **Read-only MCP endpoint** (off by default; Settings > Sign-in > "Allow AI assistants to read"): an AI assistant such as
  Claude Code can ask Pengumon about the line at `/mcp`: `status`, `incidents`, `incident`, `report`, `devices`,
  `services`, `speed_tests` and `ai_pack`. Same sign-in and guards as the API; no tool changes anything; no secret, device
  address or client identity in any answer. Pengumon's own small server: no new runtime dependency. docs/mcp.md.
### Changed
- The unit tests run sandboxed like the browser tests: no network, not root, only the raw ICMP capability, a read-only
  image (docs/testing.md).
- **US spelling throughout**: the page, the docs, the code and the names Pengumon stores or sends (color, behavior,
  neighbor, canceled, gray, deprioritize, ...). A database is updated on start (schema v16): the incident kind
  `hop_deprioritised` becomes `hop_deprioritized` (with its titles), and the dashboard setting `dashboard.colours` becomes
  `dashboard.colors`, saved versions included; a browser's card colors saved by 0.24.0 are kept. Anything reading the
  API or the webhooks by these names sees the new ones: that kind, a settings preview's state and outcome `canceled`,
  a path discovery run's outcome `canceled` (a metric label too), the hop evidence source `neighbors`.

### Security
- The boot script links a theme's stylesheet and card views only from Pengumon's own theme folders (CodeQL).

## [0.24.0] - 2026-11-06
### Added
- **Themes.** The header's palette button picks this browser's theme, light or dark, and whether animated effects run;
  Settings > Display sets the theme every browser starts with. Twelve built in: Pengumon (the standard look); the
  palettes Blueprint, E-ink, High contrast, Nord, Dracula and Solarized; **Mission control**, a wall display (a verdict
  banner in the status color, large figures, a status band on every card); and four with their own shapes and a slow
  animated effect. **Starship console** restyles every card into one flowing console frame (a colored rail sweeping into
  each card's header bar, flat blocks, segmented gauges, tank meters); **Amber CRT** (a terminal: bracketed readouts,
  text bars, the incidents as a log) and **Synthwave** (neon readouts on a horizon grid) redraw path health, latency and
  one more card each; **Digital rain** redraws path health and latency as a terminal trace, with rain falling over the
  whole page. A theme may redraw a card, restyle it, or leave it to the standard look in its colors and frame; every
  value, button and help link a card has stays in every theme (a test checks it). Themes can also change the charts'
  lines (width, straight or stepped, grid). Effects stop while the tab is hidden and when the system asks for reduced
  motion, and can be turned off per browser. Your own themes: duplicate one and change its colors, or import a theme
  file; any theme exports as one. In Layout, each card can take another theme's colors or its own. A theme is a folder
  (docs/themes.md), and every theme is tested for text contrast and for chart colors that stay apart, also for
  color-blind eyes. Each page loads only the theme in use.
### Changed
- **Every status shows its word next to its icon**: path health's hops and the cable modem's gauges showed an icon only
  (the word was in the tooltip). **Host services** is a warning while a service is only degraded, and an outage only
  when one is down (it showed an outage for both).
- **Hop details is not redrawn while it is off screen** (it is drawn when it scrolls into view): about 10 to 15 % less
  work for the page while it runs.
- **Latency and jitter incidents are judged by the median, over a window you set.** Each part of the path counts as
  slow only when more than half of its last probes are slow (Settings > Outage rules, "Judge each hop by its last":
  10 probes, 10 s at one probe a second, by default). One stalled probe, such as a round in which everything past the
  modem answers after a second, no longer opens a 9-second "ISP Latency Spikes / Jitter" incident; trouble that lasts
  still does (with the defaults: 6 of the last 10 probes slow, for 10 rounds in a row). Jitter, in incidents and on
  the page, is now the median change from one probe to the next, so one slow probe no longer turns a hop amber for a
  minute either (stored samples keep the figure they had, an average). The Settings preview can try another window
  on your stored samples.
- **Every time on the page follows the Settings time zone** when one is set (incidents, charts, the inspector, lists);
  with none set, your browser's zone, as before.
- **Ask AI reads better** (tested with three AI models on three simulated faults; mean score 4.44 -> 4.70 of 5):
  Wi-Fi drops are listed with their times, a mesh node's falls and minutes at 0 clients are told apart, the modem's
  restart count is always shown, a bad cable channel names every way it is bad, and a section over its size limit
  keeps its table's first rows instead of leaving an introduction with nothing under it.
- The router panel and the path editor name the device ("ASUS router") instead of a driver id.

### Fixed
- **Uptime keeps its key when its data cannot be loaded**, and says so in the card (it threw a page error).
- **The header pill never cuts its figures** ("ms to internet"): the verdict's title gives way first, and on a narrow
  window the name beside the logo and the live tick make room.

## [0.23.0] - 2026-11-05
### Fixed
- **Stored passwords are safe from a damaged `secrets.json`.** If the file could not be read (a typo from a hand
  edit), the next password saved in Settings replaced it, and every other stored password was lost. Now nothing is
  written over it: the save is refused with a message that says the file is damaged and how to fix it.
- **Reports in your time zone, with daylight saving.** The reliability report, "When it breaks" and the uptime strips
  put days and hours in the zone set in Settings (else your browser's), and a day the clocks change has 23 or 25
  hours. Before, they used the browser's fixed offset, so a week spanning a clock change was an hour off on one side,
  and clicking a block after such a day opened the wrong hour.
- The reliability report's "Loss by hour" counted removed hops and hops that ignore ping as loss; it now leaves them
  out, as "When it breaks" does.
- Switching to the dark theme left the charts' grid lines light until the page was reloaded.
- The device password form showed a rate limit as "Sign-in refused"; it says to wait a minute, and other errors show
  what the server said.
- Plain messages instead of Python error names: a host app check or a device whose certificate file cannot be read.
- The latency chart's line labels no longer run into each other or over the time axis.
- A stray "Settings import: : not a setting" line in the log of every fresh start.
- On a Mac whose name has no DNS entry, finding this computer's LAN address could hang for a minute (the name was
  looked up over mDNS on every request for the dashboard's info). The name is now looked up only when the route
  gives no address, and given up on after a second.
- **Quiet hours and the daily route check in your time zone in a container.** The container ran on UTC, so quiet
  hours from 22:00 began at 17:00 in New York. `scripts/deploy.sh` now passes the host's zone (`TZ`), and the compose
  files take `TZ`; Settings offers "Use this browser's zone" when they differ.

### Added
- **Search in Settings:** type to find a setting by its name, help text or key, across every section.
- A fresh install without devices says how to add your router or modem, with a button to Settings.
- The path editor asks before an earlier path or an imported file replaces a draft you have edited.
- `install.sh --user` puts the `pengumon` command in `~/.local/bin` (and says so when that is not on your PATH); the
  Windows script prints how to add it to your PATH.

### Changed
- **Settings reads in plain words:** time zones by city ("New York (America/New_York)", UTC not "Etc/UTC"), labels
  for every choice ("Outages only", "Signed-in viewers only"), one "Standard: ..." line with units under every
  row, one "Layout for new browsers" row (with Reset) instead of two rows you could not change, related settings
  named in full, devices by name, aligned controls and one heading style.
- Plainer dialogs: the device sign-in names the device and says where the password is kept; the dossier gives its
  time zone, says "1 event" and "none", and no longer doubles hop numbers; rounded latencies in Local network;
  clearer router history, Trace and Alerts labels.
- The ISP dossier's hop table numbers the hops ("Hop 3") instead of showing internal ids.
- The Layout dialog says in plain words why a panel is not available and where to set it up.
- Settings fits its buttons on one row on a phone; Ask AI shows "Building the pack..." while it works.
- The target history names a hop's role with the path editor's words (Your network, Gateway (modem), Provider,
  Internet, DNS server, Other); its median of an even number of values is the mean of the middle two, as elsewhere.
- Lighter: building an AI pack needs about 20 MB less memory (the pack's text is unchanged); the uptime strips and
  the path chain redraw only when something they show changed.
- API: the report routes take `tz` (an IANA zone, as `/api/ai-pack`); `/api/uptime` answers each block's `bounds` and
  the `zone`, `/api/report/loss-grid` its `day_starts`, `hour_starts` and `zone`; `/api/targets` sends a `short_label`.
- Docs brought up to date with the Settings page (config.yaml now only seeds the first start), every API route, new
  screenshots, and a guide to Ask AI (`docs/ai-pack.md`).
- For developers: the app's core objects left the HTTP layer, every route lives in `api/`, notifications are one
  package, and shared helpers replace copies in both Python and JavaScript (`docs/architecture.md`, "Where things
  live").

## [0.22.0] - 2026-11-04
### Added
- **Ask AI** (header, and the incident inspector): one Markdown text to paste into ChatGPT, Gemini, Claude or any
  other assistant, which cannot see your network itself. Pengumon writes what it measured and works out, not raw data:
  each hop's normal and how far it strayed, where along the path problems start (loss that carries on to every later
  hop, or stays at one router that only ignores pings), patterns (time of day, how often, how long), what coincided
  (line errors, a modem or router restart, a busy line, Wi-Fi clients dropping, route or settings changes), its own
  verdict and a rule-based confidence per incident, minute-by-minute close-ups of the most significant ones, and what
  it cannot see. With instructions and plain questions for the assistant. About 5,500 tokens for a week.
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
  Advanced area for the hop colors and the log levels. Every row shows its value, its default, what it changes and
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
  would open or no longer open. Old incidents are never relabeled: new rules apply from the moment you save.
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
  that hop's color only; the incident rules are unchanged.
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
  Colors and time labels are no longer recomputed for every message. A hidden tab keeps only the newest message
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
  every member's latency in its own color, the median the node shows, per-member loss and uptime, and a switcher to
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
- Default target colors: members of one group always get distinct colors; the example config no longer pins them.
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

[Unreleased]: https://github.com/pengupowered/pengumon/compare/v0.27.0...HEAD
[0.27.0]: https://github.com/pengupowered/pengumon/compare/v0.26.0...v0.27.0
[0.26.0]: https://github.com/pengupowered/pengumon/compare/v0.25.2...v0.26.0
[0.25.2]: https://github.com/pengupowered/pengumon/compare/v0.25.1...v0.25.2
[0.25.1]: https://github.com/pengupowered/pengumon/compare/v0.25.0...v0.25.1
[0.25.0]: https://github.com/pengupowered/pengumon/compare/v0.24.0...v0.25.0
[0.24.0]: https://github.com/pengupowered/pengumon/compare/v0.23.0...v0.24.0
[0.23.0]: https://github.com/pengupowered/pengumon/compare/v0.22.0...v0.23.0
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
