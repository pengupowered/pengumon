# 🔌 Operations endpoints

- `GET /api/health`: `200` while the probe loop keeps finishing rounds, `503` otherwise. No token needed; `deploy.sh` uses it
- `GET /metrics`: Prometheus text format: per-target latency, loss and status, incident counts, WAN load %, device status (`pengumon_collector_status{device="<device id>",status=...}`, one series set per enabled device with a snapshot). Needs the token when one is set
- `GET /api/path`: The probe path: `{"revision", "hops", "managed_by": "dashboard", "device_matches"}`, hops in path order, each a target (`id`, `name`, `address`, `category`, `enabled`, `color`, `group`, `device_id`, ...) with its `position` (0 = nearest). `device_matches`: `{hop id: device id}` for the hops of your network or the line without a device that exactly one configured device's address matches (the editor links them). Revision 0: never applied (the default targets are probed)
- `PUT /api/path`: `{"base_revision", "hops"}`: validates every hop (ids, hosts, roles, unique ids and positions, at most 64 hops, no DNS-lookup id, a configured `device_id`; for `probe: tcp` or `http` a `port` (required for TCP) outside the Fetch standard's bad ports, an `http_path` that starts with one `/` and has no `?` or `#`, role `custom` for HTTP, at most 8 such hops, none at a loopback, link-local or special-purpose address: 422 for the fields, 400 for the address and the count), stores the path in one transaction and applies it without a restart; hops left out stop being probed, their history kept and hidden. 409 with the current `revision` when the path changed since `base_revision`. Discovery's draft-only fields are ignored. Rate limited
- `DELETE /api/path/history/{id}`: Deletes the stored probes of a hop removed from the path (404 for any other id, 400 for a hop on the path). Rate limited with PUT
- `POST /api/path/test`: `{"address", "family"}` (`family` 4 or 6, for a host name): one echo request through the app's ICMP backend, `{"kind" (reply, timeout, ttl_exceeded, unreachable, ipv6_unavailable, error), "rtt_ms", "responder", "address", "error"}`; `ipv6_unavailable`: an IPv6 address while this host has no IPv6, nothing sent. With `"probe": "tcp"` or `"http"` (and `port`, `http_scheme`, `http_path`, `http_verb`, as on a hop) it makes that attempt instead, and the answer also has `status` and `connect_ms` (HTTP); the kinds are `reply`, `refused`, `timeout`, `tls`, `http_error` (a 5xx), `bad_answer`, `not_allowed` (a name that resolves to the host itself), `unreachable`, `ipv6_unavailable`, `error`. 400 for a loopback, link-local or special-purpose address, 422 for fields that do not belong together. Never returns headers or a body. Rate limited (30 a minute)
- `GET /api/path/export`: The applied path as YAML (`pengumon-path.yaml`, a `targets:` list in config.yaml syntax)
- `POST /api/path/import`: That YAML (or a config.yaml) in the body: `{"hops": [...], "device_matches"}`, a draft for the editor; nothing is applied. 422 with the reason for a bad file
- `POST /api/targets`, `DELETE /api/targets/{id}`: One hop added at the end of the path, or removed (the add-target dialog, the trace dialog's Monitor); 400 for a taken id or past 64 hops
- `GET /api/report?days=30`: Reliability report: per-day availability per hop, incident count and minutes by cause category, and `incidents.outages` (`count`, `down_minutes`, `longest_minutes`, `maintenance`: the same incidents joined into outages) (`by_cause`: `cause` is router, gateway, line, isp, ...: docs/diagnosis.md; expected incidents excluded), worst hours of the day (the hops that ignore ping and those removed from the path left out, as in the loss grid). `days` is 1 to 400. Days and hours are bucketed in the viewer's time zone, see [Time zone of the report routes](#time-zone-of-the-report-routes). The report, the loss grid and the uptime strips are kept for the rest of the minute (dropped at once when the path, a setting or an incident changes); a build that is not kept counts against a rate limit of 60 a minute per client, and at most two are built at once
- `GET /api/report/loss-grid?days=7`: Probe loss per calendar day and hour for the "when it breaks" heatmap: `days` 1 to 31, one row per day (oldest first) of 24 cells, each with `sent`, `lost` and `loss_pct` (`null` where nothing was sent). When each cell is (epoch seconds): `day_starts`, the start of every day plus the end of the last (`days` + 1 of them); `hour_starts`, per day `null` (hour h begins `h` × 3600 after the day's start) or, on a day the clocks change, the 25 instants its hours begin and the last one ends (the hour the clocks go back lasts two hours, the one they skip none). `zone` names the zone used (an IANA name, `UTC` or a fixed offset such as `+05:30`, as a browser's `Intl.DateTimeFormat` takes it) and `tz_offset_minutes` its offset now. Same time zone rule
- `GET /api/uptime?window=24h`: Availability strips for every target in one answer: `window` is `24h` (96 blocks of 15 minutes), `7d` or `30d` (calendar days); each block is `ok`, `degraded`, `outage`, `busy` (explained by line load) or `no_data`, with the availability over the window, from the rollups (quarter hours, with the minutes at the window's edges). An incident that began before the window and reaches into it marks the blocks it covers. Block i runs from `bounds[i]` to `bounds[i + 1]` (epoch seconds, `blocks` + 1 of them; a day can last 23 or 25 hours); `zone` and `tz_offset_minutes` as in the loss grid: the clock its times are meant to be read on. Same time zone rule
- `GET /api/history?start_time=&end_time=&target_ids=&bucket_seconds=&compact=`: The probe samples behind the latency chart (epoch seconds; default the last hour; `target_ids` repeated to narrow it). A range over an hour with no `bucket_seconds` is bucketed anyway (10 s up to a day, 60 s beyond), so one request cannot ask for every raw sample; `compact=true` answers `{"fields", "rows"}` instead of a list of objects, built without a dict per row. An answer of over 5,000 rows is shaped and encoded in a thread, so the page and the live feed keep running; the bytes are the same A `bucket_seconds` of whole tens of seconds (up to a day) is answered from the 10-second rollups and stays whole tens when it is widened to fit the row cap; whole quarter hours (and ranges older than the per-minute history) from the quarter-hour rollups ([storage.md](storage.md#rollups-and-retention-schema-v18)). Rate limited (60 a minute)
- `GET /api/targets`: Live statistics (WinMTR style) for every monitored target; 503 while the prober starts. Each target also has `probe` (`icmp`, `tcp`, `http`; `null` for a DNS lookup), `probe_label` (`TCP 443`, `HTTPS HEAD /health`; `null` for ping and DNS) and `probe_note` (one sentence on what the figure is and how exact). Each target's `short_label` is its role words (Router, Gateway, ISP hop 2, Internet, DNS lookup, Other hop 1; never a name or an address; `null` while it is switched off), the rule the AI pack uses too (`engine/path_rules.py` `hop_roles`); the live stream's targets carry it as well. `POST /api/targets` and `DELETE /api/targets/{id}` are the add and remove above
- `GET /api/incidents/count?scope=network`: `{"total", "expected"}`, how many incidents are recorded (`expected`: those explained by heavy line load); `scope` as for `/api/incidents`. The list loads a page at a time, so the dashboard asks here for the real figure
- `GET /api/incidents/{id}/inspect`: The post-mortem data of one incident (the incident inspector); 404 for an unknown id; rate limited (60 a minute). Besides the probes, modem and router snapshots: `device_events` (the device events within 5 minutes of the incident, as `GET /api/devices/{id}/events` lists them) and `traffic` (`null` without a router that reports traffic, else `{"hours": [...]}`: per clock hour the incident touches, as `hours` of the traffic explanation below; clients named as `/clients` names them for this request)
- `GET /api/info`: The listen port, this host's LAN addresses and URLs, the ISP name, the sign-in state (`session`), `can_edit`, `timezone` (the named zone Settings sets, else null: every time on the page follows it), whether the first-run question is open (`access_setup`) and how probes are sent (`probe`: backend, resolution, and the fix when probing does not work)
- `POST /mcp`: The read-only [MCP](mcp.md) endpoint for AI assistants (JSON-RPC, tools `status`, `incidents`, `incident`, `report`, `devices`, `services`, `speed_tests`, `traffic`, `ai_pack`). Off by default (`mcp.enabled`; `404` while off, decided on each request); the same sign-in as the API (viewers cannot use it, it is a `POST`); rate limited (120 a minute; the `ai_pack` tool 6 builds a minute, as `GET /api/ai-pack`); every other method is `405` (`/mcp/` is the same endpoint). Counted in `pengumon_mcp_calls_total`
- `GET /api/services`: The state of your own apps (status, timings, last five incidents): [services.md](services.md)
- `GET /api/speedtests?hours=720&limit=100`: The speed test ([speedtest.md](speedtest.md)): `{"enabled", "provider", "next_run" (epoch seconds when the next scheduled test may start; `null` while it is off), "running", "month_used_mb", "monthly_cap_mb", "plan": {"down_mbps", "up_mbps"} (Settings > Your line; `null` where unset), "tests"}`. `tests` are those of the last `hours` (1 to 8760), newest first, at most `limit` (1 to 500): `id`, `ts` (start), `duration_s`, `trigger` (`scheduled` or `manual`), `provider`, `server`, `status` (`ok`, `partial` when one direction worked, `skipped`, `failed`), `reason`, `down_mbps`, `up_mbps`, `idle_ms`, `loaded_down_ms`, `loaded_up_ms` (median latency of the public probe targets before the test and during each direction), `bytes_down`, `bytes_up`, `wan_down_mbps`, `wan_up_mbps` (the line's peak rate over 3 s in each direction, from the router's WAN counters read once a second during the test; `null` without a WAN counter device), `headline_down_mbps`, `headline_up_mbps` (the speeds the test answers with: the router's when it gave one at least the test's own, else the test's own `down_mbps` / `up_mbps`), `down_measured_by`, `up_measured_by`, `measured_by` (`router` or `test`; `measured_by` is `router` only when every measured direction was), `traffic_note` (when the test itself got under 70 % of what the line carried: the rest was other traffic; else `null`), `bufferbloat` (A+ to F from the added latency, `null` without latency data), `confidence` (`ok`, or `low` when a direction lasted under a second, too short to measure the line; `null` without a speed) and `note` (for `low`: which direction, and about how much data per test would measure it). Answers whether or not the test is on
- `POST /api/speedtests/run`: Starts a test now, in the background: `202 {"started": true}`. `409 {"started": false, "reason"}` when the test is off, one is already running, or this month's data cap is reached (it ignores the window and a busy line); 503 before the app has started. Needs the token when one is set; rate limited (3 in 10 minutes)
- `POST /api/mtr/trace`: `{"target_host", "family"}`: an on-demand traceroute, hop by hop (the Trace dialog). Rate limited (6 a minute)
- `WS /ws`: The live stream the dashboard listens to (one message per probe round); the message is described in [architecture.md](architecture.md) With `?delta=1` (the dashboard) each target's `recent_latencies` comes in full in the first message and when the page sends `{"resync": true}` (answered to that connection alone: at once at most every 2 s, else with the next message; the others get nothing more); every other message carries only the probes since the last one (`latency_tail`, with `latency_drop` and `latency_len`; the page puts the window back, `web/static/js/live-delta.js`). Without the flag every message is a full one. A viewer's frames are dropped by the sign-in layer, so a viewer's page that sees a gap reconnects instead.
- `GET /api/incidents?limit=100&offset=0`: Network incidents, newest first (`scope`, `include_expected`); `offset` skips the newest, so a client loads a first page and older ones on demand. Each incident has `outage_id`: the id of the [outage](diagnosis.md#outages) it belongs to (the id of the outage's first incident; the same on both sides of a page edge), or `null` for one that is in no outage (load, unknown, host apps, expected)
- `GET /api/outages?hours=168&limit=100`: The outages that overlap the last `hours` (1 to 10000), newest first, at most `limit` (1 to 500): `{"gap_minutes", "outages": [...]}`. `gap_minutes` is `incidents.group_gap_minutes` (0: grouping is off, every drop is an outage of one). An outage has `id` (its first incident's), `start_time`, `end_time` (`null` while a drop is open), `ongoing`, `settling` (it ended less than the gap ago, so it may still grow), `duration_seconds`, `down_seconds` (the critical drops' time, overlaps once), `affected_seconds` (all drops, overlaps once), `drops`, `critical_drops`, `severity` (the highest), `places` (path order: router, gateway, line, isp, internet, dns), `title` and `cause_code` (of the longest critical drop, else the longest drop), `likely_maintenance` (a label only, see [diagnosis.md](diagnosis.md#outages)) and `incident_ids` (start order). Computed on every read: changing the gap regroups the history. Same sign-in rules as `/api/incidents`
- `GET /api/incidents/dossier`: Evidence dossier for the ISP (window up to 30 days); conclusions are derived from the data, expected incidents are excluded and counted. Rate limited (20 a minute) and built one at a time (429 while another is being built)
- `GET /api/backup`: A consistent copy of the database as a download (`Content-Disposition: attachment; filename="pengumon-<version>-<UTC stamp>.db"`, `application/vnd.sqlite3`, not compressed): the history and the settings, never `secrets.json` or `sessions.json` (`pengumon backup` copies those too). Only for someone who may edit: a viewer gets 401 and the read token 403. Rate limited (2 per 10 minutes) and one at a time (429 with `Retry-After: 60` while another is being made or sent); 507 when the disk has less free space than the database is large, 404 when there is no database file. Made with `VACUUM INTO` in a private `.backup-tmp` folder in the data directory and deleted when the response ends. Counted in `pengumon_db_backups_total`, `pengumon_db_backup_size_bytes` and `pengumon_db_backup_duration_seconds`; the Settings page offers it under About this install ([storage.md](storage.md#backups)).
- `GET /api/ai-pack?scope=window&hours=24&level=full&detail=short`: The AI troubleshooting pack, one Markdown document for a chat model (`markdown`, the exact text), with `modules` (`id`, `title`, `chars`, `budget_chars`, `trimmed` per module section included) and `roles`, the "who is who" key that is never part of the text. `scope=window` takes `hours` (1 to 168; the dashboard offers 1, 24 and 168), `scope=incident` takes `incident_id` (an hour on each side; 404 for an unknown id, 422 when `incident_id` and `scope` disagree). `level`: `full` (the default), `vague` or `anonymized`; `detail`: `short` (the default) or `detailed`; times in the zone set in Settings (`general.timezone`), else in `tz` (the viewer's IANA zone, `America/New_York`; the dialog sends the browser's), else at `tz_offset_minutes` (as in `/api/report`), else UTC, which the header then says; a named zone's daylight-saving changes are followed and named. Anyone who can view the dashboard may ask for any level; with viewing without the token on, that includes Full. Rate limited (6 a minute) and one build at a time (429 while another runs). Counted in `pengumon_ai_pack_exports_total`.
- `GET /api/status`: Current diagnosis, including `load_pct`
- `GET /api/themes`: Every theme, built in and custom: `{"default" (the theme id Settings sets, `default` when it names none), "effects" (Settings > Display > Animated effects), "themes": [{id, name, description, modes, tags, custom, skin, effect (the effect module's URL or null), effect_on, css (its stylesheet's URL, built-in themes with one), views (card id: the URL of the theme's drawing of that card), chart (its chart style), theme (the theme as an importable theme.json: every color per mode)}], "looks" (the page's short list, as <html data-looks>)}`. The page itself starts from `<html data-looks>`; docs/themes.md
- `GET /api/devices`: Every configured device: id, driver, host, url, model, `label` (what it is and its model, e.g. "ASUS router · RT-AX88U"; its id when no model is set), enabled, capabilities (`offers`), line technology, poll interval, `has_credentials`, and its last poll (`status`, `last_poll`, `error_kind`, `error_message`). Never a credential. A viewer or the read token gets `url` without its path and a failed poll's `error_message` in fixed words by `error_kind` (the driver's own text can name the login it tried); a device that is not set up keeps Pengumon's own message
- `GET /api/devices/{id}`: The device's latest snapshot (polled once if there is none yet); 404 for an unknown id. Its `error_message` as in the list for a viewer or the read token
- `POST /api/devices/{id}/credentials`: `{"username", "password"}`: stored in `data/secrets.json` and tested with one poll (`success`, `snapshot`, and `overridden_by` when a higher-ranked source will win at the next start); same-origin only, rate limited with the other credential endpoints; a 422 never echoes the submitted values. For an SNMPv3 device, a user name over 32 bytes or a password (or privacy password) under 8 characters is refused with 400 and nothing is saved
- `POST /api/discovery`: Starts discovering the path (`{"asn_lookup": true, "snmp": false, "device_checks": true}`, all optional) and answers `{"id"}`; a second start while one runs returns the running id; rate limited. Never applies anything. What it sends where: [discovery.md](discovery.md)
- `GET /api/discovery/{id}`: A run's `state` (`running`, `done`, `failed`), `progress`, `draft` (`{"hops": [...]}`, path hops with `evidence`, `confidence`, `suggested_driver` and `status` new/same/changed/gone; empty until done) and `notes`; 404 for an unknown id or one finished over ten minutes ago
- `GET /api/notifications`: The server's alert channels, `{"channels": [{name, kind (ntfy or webhook), format, host}]}`; never a URL path, query, header or secret. `host` is `null` for a viewer or the read token (as `GET /api/settings` keeps a secret address's host from them)
- `POST /api/notifications/test`: Sends a test message through every channel now and answers `{"channels": [{name, ok, detail}]}` (`detail` is a short reason in plain words, such as `the server answered 404`, `could not connect` or `timed out`); 409 when no channel is configured; rate limited
- `GET /api/features`: Per feature (`line_load`, `line_health`, `router_system`, `clients`, `mesh`, `traffic`): `available`, `reason_kind` (`available`, `none`, `disabled`, `not_configured`), the devices that provide it, and otherwise why not (e.g. `router (asuswrt) provides wan_counters but is not configured: no password`)
- `GET /api/devices/{id}/clients`: The connected clients: `{ts, nodes, clients, summary: {online, wired, by_band, by_node}, privacy}`. Needs the `client_inventory` capability (400 otherwise, 404 for an unknown id); rate limited. What it shows of each client follows `privacy.client_details`, see "Clients and mesh" below. Each client has `traffic_hint`: the app the router counted at least 80 % of its last 24 hours' traffic as (null when none stands out, or without traffic figures)
- `GET /api/devices/{id}/clients/history?start_time=&end_time=`: Client counts over time (online, wired, per band, per mesh node), bucketed on the server to at most 300 points (each the mean of its bucket); defaults to the last 24 hours. Counts only, never a client
- `GET /api/devices/{id}/history?start_time=&end_time=`: The stored polls (router metrics: WAN rates, CPU, memory, clients) of a device that offers `wan_counters` or `router_system`, oldest first, padded two minutes each side; defaults to the last hour. 404 for an unknown id, 400 for a device without those capabilities
- `GET /api/devices/{id}/history?start_time=&end_time=&bucket_seconds=&max_points=`: The same router history, bucketed on the server when `bucket_seconds` and/or `max_points` is given (the default raw answer above is unchanged otherwise): `{"bucket_seconds", "start_time", "end_time", "fields", "rows", "capacity"}`. Rows follow `fields`: bucket start (epoch s) then avg and max of WAN rx and tx Mbps, CPU %, memory % (`rx_avg`, `rx_max`, `tx_avg`, ... `mem_max`) and `clients_avg`, `clients_max`, then `temp_avg`, `temp_max` (CPU °C) and `link_min` (the lowest WAN link speed in Mb/s, 0 = no link); a figure no poll in the bucket measured is `null`; no padding. The width is at least `bucket_seconds` and wide enough for `max_points` buckets (default 400, at most 1000), rounded to 10 s or whole minutes, so any range returns about 400 rows (about 25 KB). The range is clipped to the 7 day retention. `capacity` is `{"down_mbps", "up_mbps"}`, the line capacity in use (`null` where unknown), for a reference line. 422 for a bad parameter
- `GET /api/devices/{id}/traffic?hours=24&days=&top=`: The router's traffic per client (its Traffic Analyzer, schema v19) over the last `hours` complete clock hours (1 to 9600; the hour in progress is never counted), or with `days` (1 to 400; `hours` is then ignored) the last `days` whole days up to the last midnight in the zone set in Settings, with a total per day. Needs a driver that offers `traffic_usage` (400 otherwise, 404 for an unknown id); rate limited (60 a minute, shared with the next route). Clients follow `privacy.client_details` as in `/clients`. See "Router traffic" below
- `GET /api/devices/{id}/traffic/clients/{client}?hours=168&days=`: One client's traffic over the last `hours`, or the last `days` whole days as `/traffic` takes them: its hourly series (and `daily` with `days`), its apps over the range and its apps by day. `client` is the client's `key` (the same opaque key `/clients` and `/traffic` give it until the next start), or its MAC for a request that may see MACs. Prefer the key, as the dashboard does: request paths are written to the access log, and a key there names no device; 404 for a client with no stored traffic that is not in the client list. See "Router traffic" below
- `GET /api/devices/{id}/events?start_time=&end_time=`: The device's events, oldest first (default: the last 7 days): `{"start_time", "end_time", "events": [{"device_id", "ts", "kind", "detail", "text"}]}`. `kind` `restarted` (`detail`: `boot_at`, `previous_boot_at`, `uptime_s`) or `wan_link` (`detail`: `old`, `new` in Mb/s, 0 = no link); `text` in a few words, in the zone set in Settings ("WAN link went down (was 2.5 Gb/s)"). 404 for an unknown id
- `GET /api/devices/{id}/channels/history?start_time=&end_time=&channel_id=&direction=`: Per-channel history of a device whose access line is DOCSIS: `{"fields", "rows"}` with compact rows `[ts, direction ("d" or "u"), channel_id, power_dbmv, snr_db, uncorrectables_recent]`, oldest first (`snr_db` is `null` for upstream channels; `ts` is whole seconds). Defaults to the last 24 h, clipped to the 7 day retention; `channel_id` and `direction` (`downstream` or `upstream`) narrow it. About 112 KB for 24 h and 780 KB for 7 d of a 37 channel modem polled every 15 min (before gzip). 404 for an unknown id, 400 for a device without `access_line` or whose line is not DOCSIS

- `PUT /api/devices/{id}/clients/{client}/name` with `{"name": "PS5"}`: gives a client a name in Pengumon, shown instead of the router's wherever names are shown in full (`/clients`, `/traffic`, the incident inspector); `null` or `""` takes it away. `client` as in the route above. The name is trimmed and cut to 40 characters (longer is 422); curly brackets become round ones. Answers `{"key", "label", "router_label"}`. 403 to a request that may not see names (`privacy.client_details`), 404 for an unknown client, 409 past 1000 names; rate limited (30 a minute). In `/clients` and `/traffic` a client with such a name has `router_label`, the router's own name for it (null otherwise; only where names are shown)

## Router traffic

`GET /api/devices/{id}/traffic?hours=24&top=` answers (byte counts are integers; times epoch seconds):

```json
{
  "available": true, "reason": null, "read_at": 1794132300.0, "interval_seconds": 3600.0,
  "hours": 24, "start": 1794042000, "end": 1794128400,
  "window_end": 1794128400, "stored_at": 1794128700.0,
  "wan": [{"hour": 1794042000, "up": 257987832, "down": 2194895695}, {"hour": 1794045600, "up": null, "down": null}],
  "totals": {"up": 4512000000, "down": 211000000000},
  "clients": [{"key": "3f1c0a9b2d4e", "label": "Den console", "kind": "game", "vendor": "Example Games",
               "mac": "02:00:00:00:00:03", "up": 4000000000, "down": 198000000000,
               "apps": [{"app": "Sony PlayStation", "up": 4000000000, "down": 196000000000}],
               "traffic_hint": "Sony PlayStation"}],
  "apps": [{"app": "Sony PlayStation", "up": 4000000000, "down": 196000000000}],
  "apps_window_end": 1794128400,
  "apps_days": null, "days": null, "daily": null,
  "privacy": "full"
}
```

- `available` / `reason`: whether the router gives these figures; `reason` says why not ("this router does not offer
  the Traffic Analyzer pages", "the router's Traffic Analyzer has no data: it may be switched off ...", "switched off
  in the device's options (traffic_usage: false)", "the device is disabled in configuration"). `read_at`: when the
  router's traffic was last stored (null before the first read since the start); `interval_seconds`: the driver's
  `traffic_interval_seconds` (null when switched off).
- `start` / `end`: the hours covered, `[start, end)`, `end` the start of the hour in progress. `window_end`: the end of
  the newest hour stored (the router publishes an hour a minute or so after it ends); `stored_at`: when the newest
  rows were stored.
- `wan`: one entry per hour of the range, oldest first, the whole WAN; `up`/`down` null for an hour not stored (not
  published yet, or Pengumon was not reading then). `totals`: the WAN's sums over the range.
- `clients`: ranked by download over the range (clients with no bytes left out; `top` keeps the first N): `key` (the
  opaque key of `/clients`), `label` (as `/clients`: the name in full, else maker and last 4 hex digits of the MAC;
  a client no longer in the client list is its MAC, or "Device" and the digits), `kind` (phone, computer, game,
  network, printer or null), `vendor`, `mac` (only when the request may see names: `privacy` "full"), `up`/`down`
  over the range, `apps` (its top 3 apps by download, over the same windows or days as `apps` below) and
  `traffic_hint`.
- `apps`: every client together, ranked by download; `top` keeps the first N. Names are the router's (Trend Micro)
  labels, which it cuts at about 39 characters. The router counts apps only per 24 hours, so: for `hours` under 48, the
  router's latest 24-hour window (`apps_window_end` its end; `apps_days` null); for 48 hours or more, or with `days`,
  the sum of the whole local days (midnight to midnight, in the zone set in Settings) up to the last midnight, `hours //
  24` of them or `days`. Pengumon keeps a day's apps when it reads the router's window that ends at that midnight (the
  round just after it); a day it did not is not counted. `apps_days` is then `{"first", "last"` (the days wanted,
  "YYYY-MM-DD"), `"wanted"`, `"stored"` (how many had apps), `"days"` (those, oldest first)`}`, and `apps_window_end` is the
  end of the last day counted (null with none).
- `days`, `daily` (with `days` only, else null): `days` as asked; `start`/`end` are then local midnights and `hours`
  the hours between them (a daylight saving day has 23 or 25). `daily`: one entry per day, oldest first: `{"day",
  "start", "end", "up", "down", "hours", "stored"}`, `up`/`down` the WAN's sums over the hours stored that day (null
  with none), `stored` how many of its `hours` were. With `days`, `wan` is empty (the hours of 30 days would be most of
  the answer, and the dashboard draws days): ask with `hours` for the hours.
- `privacy`: "full" or "masked", what this request was shown.

`GET /api/devices/{id}/traffic/clients/{client}?hours=168&days=` answers `available`, `reason`, `read_at`,
`interval_seconds` and `privacy` as above, and `client` (the client's row as in `clients` above, with
`traffic_hint` over the range), `hours`, `range_days` (the `days` asked, else null), `start`, `end`, `daily` (with
`days`: its totals per day, as in `/traffic`, and `series` empty), `series` (one entry per hour, oldest first: `{"hour", "up", "down"}`, 0 for
an hour the router has with nothing for this client, null for an hour not stored at all), `totals` (`{"up", "down"}`
over the range), `apps` (ranked by download, over the range as `/traffic` counts them: its latest 24-hour window
under 48 hours, else the sum of its whole days), `apps_window_end`, `apps_days` (as in `/traffic`), and `days`
(`[{"day": "2026-11-09", "window_end", "apps": [...top 10]}]`, oldest first: the whole local days, midnight to
midnight in the zone set in Settings, whose window ends at or after `start`).

**Explaining a span** (`traffic.hours` in the incident inspector; the dossier's busy hours): per clock hour,
`{"hour", "available", "state", "up", "down", "clients", "text"}`. `state` "ok", "not yet available" (newer than the
newest hour stored: the router publishes it after the hour) or "no data" (older, never read); `clients` the top 3 by
download in that hour (`key`, `label`, `kind`, `vendor`, `mac` as above, `who` (the client in words), `up`, `down`,
`share` of the WAN's download); `text` e.g. "Den console (02:00:00:00:00:03) downloaded 75 GB in 08:00-09:00 (all
clients: 80 GB)", clock times in the zone set in Settings. An empty list when no traffic was ever stored. The dossier
and the AI pack name a client only by its kind and main app ("a game console (Sony PlayStation)").

## Where the routes live

One router module per feature in `src/pengumon/api/`: `targets.py`, `path.py` (the path editor's routes), `incidents.py` (list, inspect, dossier), `outages.py` (the outage list), `history.py`, `reports.py` (report, loss grid, uptime, AI pack), `ops.py` (info, status, health, traceroute, `/metrics`), `notifications.py` (the alert channels and the test message), `settings.py` (the Settings page; its bodies are in `settings_models.py`), `settings_preview.py` (the outage-rules preview), `themes.py` (the themes list), `devices.py`, `services.py`, `clients.py`, `discovery.py`, `speedtests.py` (the speed test), `traffic.py` (the router's traffic and device events), `live.py` (the `/ws` websocket) and `access.py` (the sign-in). `common.py` has what they share (`StateDep`, `json_response`, `device_or_404`). `api/router.py` lists every router in `ROUTERS` and mounts them; it holds no handlers (`tests/test_api_routers.py`). A new endpoint goes in its feature's module in `api/`, also for a feature that is a component (docs/architecture.md, "Where things live").

## The probe path's history, preview and route check

Added with the path editor's versions, preview and "Your path changed" notice. Each is under `/api/path`, needs the
token when one is set, and changes nothing that is live unless it says so.

- **`POST /api/path/preview`** takes `{"hops": [...]}` (a draft, as `PUT /api/path` takes it) and answers
  `{"id", "probed"}`. Every enabled hop whose address and family no live hop has is probed once a second for 30 s,
  through the app's prober (ICMP, or a TCP or HTTP attempt for a hop set to one), and nothing is recorded. A browser's new preview stops its previous one. At most two
  run at once in the app: a third gets 429 with `Retry-After: 30`. Rate limited.
- **`GET /api/path/preview/{id}`** answers `{"state": "running" | "done", "elapsed_s", "hops"}`. Each entry of `hops`
  (by hop id, only the probed ones) has `sent`, `received`, `loss_pct`, `median_ms`, `last_ms` and `kind` (`reply`,
  `timeout`, `refused` (a TCP or HTTP hop's closed port), `failed` (a TLS, HTTP 5xx, not-HTTP or not-allowed answer: Test says why), `unreachable` or `ipv6_unavailable`; a time-exceeded answer or an error counts as `unreachable`). A hop probed live by the same method at the same address is not previewed; a draft with a loopback TCP or HTTP hop, or past 8 of them, is a 400.
- **`DELETE /api/path/preview/{id}`** stops it and answers its final body. The editor sends it when it closes.
- **`GET /api/path/versions`** lists the applied paths, newest first (the last 50), each `{"revision", "applied_at",
  "hop_count", "summary"}`; `summary` says what changed (hops added, removed, renamed, a role or an address).
  Revision 1 is the import.
- **`GET /api/path/versions/{revision}`** answers `{"revision", "hops"}`, that version as a draft. Restoring it is
  applying that draft with `PUT /api/path`; a hop that comes back keeps its history. Needs the password (a viewer or
  the read token gets 401: it is a whole path document, as the export is).
- **`GET /api/path/events`** lists what happened to hops, oldest first: `{"at", "kind": "address_changed" | "probe_changed",
  "target_id", "old", "new", "revision"}` (`probe_changed`: `old` and `new` are the method as the editor words it, `ping` or `TCP 443`). `?target_id=` narrows it to one hop. The target history dialog marks them.
- **`GET /api/path/drift`** is the latest background route check, compared with the path applied now:
  `{"checked_at", "state", "changes", "discovery_id"}`. `state` is `same`, `changed`, `failed` or `never`. Each change
  is `{"kind": "new" | "gone" | "changed", "hop", "text"}`; only hops of your network, the line and the provider count.
  `discovery_id` opens the check's draft in the editor (`GET /api/discovery/{id}`). It clears after an Apply that
  takes the change.
- **`POST /api/path/drift/dismiss`** marks the changes shown now as seen: they are not shown or announced again while
  later checks see them.

`GET /api/path` and `POST /api/path/import` also answer `device_matches`, and `GET /api/devices` gives each device a
`label` (see the list above).

## Time zone of the report routes

`/api/report`, `/api/report/loss-grid` and `/api/uptime` put each sample in a day and an hour of the viewer's local
time. They take `tz`, an IANA zone name such as `America/New_York` (the dashboard sends the browser's), and still
accept `tz_offset_minutes` (-840 to 840) for older pages. The zone used is, in this order: the `general.timezone`
setting (unless it is UTC, the default in a container), else `tz`, else `tz_offset_minutes`, else UTC; `/api/ai-pack` follows the same order. A named zone follows
daylight-saving changes, so a day can have 23 or 25 hours; a fixed offset does not.

Each answer is kept for the rest of the minute (per route and parameters). Answers that are not kept are built two at a
time (others wait their turn, so the reads of the rest of the app always find a database connection) and count against
a rate limit of 60 a minute per client, shared by the three routes (429 with `Retry-After`); a kept answer is free.

## Settings

The Settings page reads these. The bodies are pydantic models in `src/pengumon/api/settings_models.py`; the list of
settings, their sections and words come from `src/pengumon/settings/registry.py`, and each setting's type, range and
default from its config section model. A key is a dotted config.yaml path (`classifier.down_loss_pct`); values are in
stored units (probe rounds, fractions, seconds) and the page converts them with the schema's `scale` and `rounds`.
Secrets (the ntfy topic and token, webhook URLs, header secrets) are never sent back: only whether each is set,
where from, and for a URL its host.

- **`GET /api/settings/schema`**: `{"round_seconds", "sections": [{id, title, order}], "settings": [...], "lists",
  "secrets"}`. Each setting has its registry entry (`key`, `section`, `group`, `label`, `effect`, `unit`, `scale`,
  `rounds`, `live`, `env`, `advanced`, `export`, `order`, `related`, `also_in`, `choice_labels`: the words shown for
  each stored choice, `empty`: what an empty value means) and, read from its field, `type`
  (number, integer, boolean, text, choice, time, timezone, layout, theme, hours: a whole number of hours shown in hours, days or weeks), `nullable`, `min`, `max`, `exclusive_min`,
  `exclusive_max`, `max_length`, `step`, `choices` and `default`. `lists` has `devices`, `services`, `webhooks`,
  `dns_probes` and `themes` (the custom themes, docs/themes.md), each with its `item_fields` and `default_items`; devices also have `drivers` (label, poll defaults,
  default user name, URL schemes and the driver's `options` as fields; `mesh_node_labels` is type `map`). Webhook
  items have no `url`: it is the secret slot `webhook.<id>.url`. Sent with an ETag of the app version.
- **`GET /api/settings`**: `{"revision", "values", "lists", "secrets", "learned", "about", "can_edit"}`. `values`:
  per key `{value, stored, source: default | stored | env, pending_restart, live_failed}`; a key a variable pins
  (`PENGUMON_LOG_LEVEL`, `PENGUMON_LOG_LEVELS`, `PENGUMON_GRAFANA_URL`; set and not empty) has source `env`.
  `lists`: per list `{set, items, errors}`; `set` false means the default items. `secrets`: per slot
  `{set, source: env | secrets_file | secret_file | null, host}`. `learned`: the learned line capacity
  `{down_mbps, up_mbps}`. `about` (listen address, data folder, config and database paths, whether a token is set,
  viewing without it, the OTLP host, the version) is sent only to someone who may edit. A viewer (no password,
  "Others may look") gets every value, and the lists with the dashboard's own facts (ids, names, on or off, a device's
  address, a check's host and port) but without a device's user name and driver options (they hold the mesh labels),
  a check's path, query and CA file path, a webhook's headers, the host of a secret address and the header secrets.
  Each list holds at most 50 devices, 100 host app checks, 20 webhooks, 50 DNS lookups or 30 custom themes; more is
  refused with "too many". A number that is Infinity or NaN is refused (422 for the time parameters of the history
  routes, `ok: false` / 422 for settings).
- **`GET /api/settings/values`**: `{"revision", "values": {key: effective value}}`.
- **`GET /api/settings/grafana-links`**: The header's Grafana links as they are now (the page refills its menu after a Save of `display.grafana_url`).

- **`POST /api/settings/validate`** (60 a minute): a change `{values: {key: value | null}, lists: {name: [...] |
  null}}` against the current state -> `{ok, errors, warnings, summary}`. Nothing is applied. `warnings` are not
  refusals (retention under 7 days, lowering the raw retention, client details shown to anyone without a token);
  `summary` is the version line the save would write.
- **`PUT /api/settings`** (20 a minute, bucket `settings`): `{base_revision, values, lists, secrets, current_password?}`. `values[key] =
  null` resets a key (Reset), `lists[name] = null` a list, `secrets[slot] = ""` clears a slot; a secret's value goes
  only this way, never back. `?source=restore` marks the save of a version loaded with `GET /versions/{n}`,
  `?source=yaml_import` one of an Import draft (default `save`). The order: validate (422, or 400 for a key or slot a
  variable pins), write secrets.json (500, nothing saved), commit, apply live. A key or list the request touches that
  changed between `base_revision` and now answers 409 `{detail, revision, conflicts}`; untouched parts may have
  changed. Answer: `{revision, summary, applied, restart_needed, live_failed}`; `live_failed` parts are saved and
  apply at the next start. A save (a restore, an import) that stores `access.allow_viewers` as on while it is not
  stored so needs `current_password` while a password is in force (the environment's too): 403 "Enter the current
  password..." without it, "The current password is not right." for a wrong one (a failed sign-in, as below), before
  anything is checked or saved. A 422 never repeats what was sent.
- **`POST /api/settings/preview`** (6 a minute): `{values, days}` (`classifier.*`, `line.down_mbps` / `up_mbps` and
  `alerts.*` keys only; `days` defaults to the smaller of 7 and the retention) -> `{"id"}`. The stored samples are
  replayed with those draft rules in a separate process; nothing is changed. 422 for a key it cannot replay or a value
  out of range, 429 with `Retry-After: 30` while another replay runs. **`GET /api/settings/preview/{id}`** answers
  `{state: running | done | failed | canceled, progress, window, current, draft, recorded, changes, notes}` (the
  incident counts as recorded, as the current rules give them, and under the draft; polling keeps the job alive), and
  **`DELETE /api/settings/preview/{id}`** stops it. This is the Outage rules preview on the Settings page.
- **`GET /api/settings/versions`**: the kept versions (the last 50), newest first: `[{revision, applied_at, source:
  import | save | restore | yaml_import | upgrade, summary}]`; for a viewer or the read token a header secret's change reads
  "secret of a webhook header changed" (its slot name names the header). **`GET /api/settings/versions/{n}`**: `{revision, values,
  lists}`, that version as a draft against the current state (null for a value stored now and not then); 404 when it
  is no longer kept; needs the password (a viewer gets 401: it is the whole document, as the export is).
- **`GET /api/settings/export`**: the stored values and the set lists as YAML in config.yaml syntax. No secret: a
  webhook has no URL and its headers name their secrets. Needs the token even when viewing without it is allowed.
- **`POST /api/settings/import`** (20 a minute): YAML in the body (an export, or a whole config.yaml; the path
  import's limits: 256 KiB, depth 16, no anchors, aliases or merge keys) -> `{values, lists, notes}`, a draft.
  Nothing is applied. Bootstrap keys and `targets:` are ignored, webhook URLs and plain header values are left out,
  each with a note.

Errors of the settings endpoints that change things use one shape: `{"detail", "errors": [{"where": "values.<key>" |
"lists.<name>[<i>].<field>" | "secrets.<slot>", "message"}]}`; a message never repeats the submitted value.

## Sign-in

Who can open the dashboard ([security.md](security.md#who-can-open-the-dashboard); `api/access.py`). The password and
the read token are never in an answer, except the one that set them. Every route but the GET needs the password once one
is set (the read token gets 403); the ones that change something share a limit of 10 a minute (bucket `access`).

- **`GET /api/access`**: `{"mode": "open" | "password" | "locked", "from_environment", "pending", "https",
  "read_token"}`. `locked`: secrets.json is damaged or missing (or its `access_hash` is no password hash) and a password
  was set, so nobody can sign in. `pending`: the first-run question is open. `read_token`: whether a read token exists.
- **`POST /api/access/setup`**: `{choice: "open" | "password", password?, viewers?}`, the first-run question. 409 once
  it has been answered (by anyone). With a password: `{mode, password, viewers_saved}` and a session cookie for this
  browser; `viewers` turns on `access.allow_viewers` as a settings save of its own.
- **`PUT /api/access/password`**: `{password, current_password}` (8 to 128 characters, no control characters;
  surrounding spaces are dropped) sets or changes it, in force at once: every other browser is signed out, this one gets
  a new session cookie, and the read token is removed (`read_token_removed` says whether there was one). 409 while
  `PENGUMON_ACCESS_TOKEN` sets it. **`DELETE /api/access/password`** `{current_password}` removes it, and the read
  token with it: `{"mode": "open", "read_token_removed"}`. Once a password is set on the page, both need it again in `current_password`: 403
  "Enter the current password..." without it, 403 "The current password is not right." for a wrong one, which counts as
  a failed sign-in of this address (after 5 in a minute, 429 with `Retry-After` without checking). Not asked while there
  is no password, nor by the first-run question.
- **`POST /api/access/read-token`** `{current_password}`: makes the read token, or replaces the one there was (the old
  one stops working at once, and live connections are closed): `{"read_token": "pgr_..."}`, shown this once. 409
  without a password (or with a damaged secrets.json). **`DELETE /api/access/read-token`** `{current_password}` removes
  it: `{"read_token": false}`. Both need the password in force as `current_password`, the one from
  `PENGUMON_ACCESS_TOKEN` too (403 as above): a browser left signed in cannot leave a read token behind.

Errors: `{"detail": "<sentence>"}`. A 422 never repeats the submitted value.

**The read token** (`Authorization: Bearer pgr_...`) reads what a viewer may (every GET of the API but the exports and
device credentials, `/metrics`, the live socket receive-only), whether or not viewing without the password is on, and may
POST to `/mcp` (read-only). Everything else answers 403. It is never `can_edit`: client names and addresses are masked
for it as for a viewer. **Failed sign-ins** (a wrong Bearer token, a wrong password on `/login` or as
`current_password`) are counted per address (an IPv6 address by its /64): after 5 in a minute that address's tokens are
not checked for a minute (429 with `Retry-After`; a browser that is signed in, and the read token, keep working). Past
20 failures a minute from all addresses together, addresses that have not signed in within a day are refused the same
way. `POST /login` takes the same `Origin` rule as every change (a foreign `Origin`, or `Sec-Fetch-Site: cross-site` or
`same-site`: 403, not counted), so another site's page cannot drive the count; a script sends neither header. The
client's address is the connection's: `X-Forwarded-For` is ignored unless `PENGUMON_TRUSTED_PROXIES` names the proxy
([security.md](security.md#settings)).
