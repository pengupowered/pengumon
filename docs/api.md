# 🔌 Operations endpoints

- `GET /api/health`: `200` while the probe loop keeps finishing rounds, `503` otherwise. No token needed; `deploy.sh` uses it
- `GET /metrics`: Prometheus text format: per-target latency, loss and status, incident counts, WAN load %, device status (`pengumon_collector_status{device="<device id>",status=...}`, one series set per enabled device with a snapshot). Needs the token when one is set
- `GET /api/path`: The probe path: `{"revision", "hops", "managed_by": "dashboard", "device_matches"}`, hops in path order, each a target (`id`, `name`, `address`, `category`, `enabled`, `color`, `group`, `device_id`, ...) with its `position` (0 = nearest). `device_matches`: `{hop id: device id}` for the hops of your network or the line without a device that exactly one configured device's address matches (the editor links them). Revision 0: never applied (the default targets are probed)
- `PUT /api/path`: `{"base_revision", "hops"}`: validates every hop (ids, hosts, roles, unique ids and positions, at most 64 hops, no DNS-lookup id, a configured `device_id`), stores the path in one transaction and applies it without a restart; hops left out stop being probed, their history kept and hidden. 409 with the current `revision` when the path changed since `base_revision`. Discovery's draft-only fields are ignored. Rate limited
- `DELETE /api/path/history/{id}`: Deletes the stored probes of a hop removed from the path (404 for any other id, 400 for a hop on the path). Rate limited with PUT
- `POST /api/path/test`: `{"address", "family"}` (`family` 4 or 6, for a host name): one echo request through the app's ICMP backend, `{"kind" (reply, timeout, ttl_exceeded, unreachable, ipv6_unavailable, error), "rtt_ms", "responder", "address", "error"}`; `ipv6_unavailable`: an IPv6 address while this host has no IPv6, nothing sent. Rate limited (30 a minute)
- `GET /api/path/export`: The applied path as YAML (`pengumon-path.yaml`, a `targets:` list in config.yaml syntax)
- `POST /api/path/import`: That YAML (or a config.yaml) in the body: `{"hops": [...], "device_matches"}`, a draft for the editor; nothing is applied. 422 with the reason for a bad file
- `POST /api/targets`, `DELETE /api/targets/{id}`: One hop added at the end of the path, or removed (the add-target dialog, the trace dialog's Monitor); 400 for a taken id or past 64 hops
- `GET /api/report?days=30`: Reliability report: per-day availability per hop, incident count and minutes by cause category (`by_cause`: `cause` is router, gateway, line, isp, ...: docs/diagnosis.md; expected incidents excluded), worst hours of the day (the hops that ignore ping and those removed from the path left out, as in the loss grid). `days` is 1 to 400. Days and hours are bucketed in the viewer's time zone, see [Time zone of the report routes](#time-zone-of-the-report-routes)
- `GET /api/report/loss-grid?days=7`: Probe loss per calendar day and hour for the "when it breaks" heatmap: `days` 1 to 31, one row per day (oldest first) of 24 cells, each with `sent`, `lost` and `loss_pct` (`null` where nothing was sent). When each cell is (epoch seconds): `day_starts`, the start of every day plus the end of the last (`days` + 1 of them); `hour_starts`, per day `null` (hour h begins `h` × 3600 after the day's start) or, on a day the clocks change, the 25 instants its hours begin and the last one ends (the hour the clocks go back lasts two hours, the one they skip none). `zone` names the zone used (an IANA name, `UTC` or a fixed offset such as `+05:30`, as a browser's `Intl.DateTimeFormat` takes it) and `tz_offset_minutes` its offset now. Same time zone rule
- `GET /api/uptime?window=24h`: Availability strips for every target in one answer: `window` is `24h` (96 blocks of 15 minutes), `7d` or `30d` (calendar days); each block is `ok`, `degraded`, `outage`, `busy` (explained by line load) or `no_data`, with the availability over the window, from the per-minute rollups. Block i runs from `bounds[i]` to `bounds[i + 1]` (epoch seconds, `blocks` + 1 of them; a day can last 23 or 25 hours); `zone` and `tz_offset_minutes` as in the loss grid: the clock its times are meant to be read on. Same time zone rule
- `GET /api/history?start_time=&end_time=&target_ids=&bucket_seconds=&compact=`: The probe samples behind the latency chart (epoch seconds; default the last hour; `target_ids` repeated to narrow it). A range over an hour with no `bucket_seconds` is bucketed anyway (10 s up to a day, 60 s beyond), so one request cannot ask for every raw sample; `compact=true` answers `{"fields", "rows"}` instead of a list of objects. Rate limited (60 a minute)
- `GET /api/targets`: Live statistics (WinMTR style) for every monitored target; 503 while the prober starts. Each target's `short_label` is its role words (Router, Gateway, ISP hop 2, Internet, DNS lookup, Other hop 1; never a name or an address; `null` while it is switched off), the rule the AI pack uses too (`engine/path_rules.py` `hop_roles`); the live stream's targets carry it as well. `POST /api/targets` and `DELETE /api/targets/{id}` are the add and remove above
- `GET /api/incidents/count?scope=network`: `{"total", "expected"}`, how many incidents are recorded (`expected`: those explained by heavy line load); `scope` as for `/api/incidents`. The list loads a page at a time, so the dashboard asks here for the real figure
- `GET /api/incidents/{id}/inspect`: The post-mortem data of one incident (the incident inspector); 404 for an unknown id
- `GET /api/info`: The listen port, this host's LAN addresses and URLs, the ISP name, the sign-in state (`session`), `can_edit`, `timezone` (the named zone Settings sets, else null: every time on the page follows it), whether the first-run question is open (`access_setup`) and how probes are sent (`probe`: backend, resolution, and the fix when probing does not work)
- `POST /mcp`: The read-only [MCP](mcp.md) endpoint for AI assistants (JSON-RPC, tools `status`, `incidents`, `incident`, `report`, `devices`, `services`, `speed_tests`, `ai_pack`). Off by default (`mcp.enabled`; `404` while off, decided on each request); the same sign-in as the API (viewers cannot use it, it is a `POST`); rate limited (120 a minute; the `ai_pack` tool 6 builds a minute, as `GET /api/ai-pack`); every other method is `405` (`/mcp/` is the same endpoint). Counted in `pengumon_mcp_calls_total`
- `GET /api/services`: The state of your own apps (status, timings, last five incidents): [services.md](services.md)
- `GET /api/speedtests?hours=720&limit=100`: The speed test ([speedtest.md](speedtest.md)): `{"enabled", "provider", "next_run" (epoch seconds when the next scheduled test may start; `null` while it is off), "running", "month_used_mb", "monthly_cap_mb", "plan": {"down_mbps", "up_mbps"} (Settings > Your line; `null` where unset), "tests"}`. `tests` are those of the last `hours` (1 to 8760), newest first, at most `limit` (1 to 500): `id`, `ts` (start), `duration_s`, `trigger` (`scheduled` or `manual`), `provider`, `server`, `status` (`ok`, `partial` when one direction worked, `skipped`, `failed`), `reason`, `down_mbps`, `up_mbps`, `idle_ms`, `loaded_down_ms`, `loaded_up_ms` (median latency of the public probe targets before the test and during each direction), `bytes_down`, `bytes_up`, `wan_down_mbps`, `wan_up_mbps` (the line's peak rate over 3 s in each direction, from the router's WAN counters read once a second during the test; `null` without a WAN counter device), `headline_down_mbps`, `headline_up_mbps` (the speeds the test answers with: the router's when it gave one at least the test's own, else the test's own `down_mbps` / `up_mbps`), `down_measured_by`, `up_measured_by`, `measured_by` (`router` or `test`; `measured_by` is `router` only when every measured direction was), `traffic_note` (when the test itself got under 70 % of what the line carried: the rest was other traffic; else `null`), `bufferbloat` (A+ to F from the added latency, `null` without latency data), `confidence` (`ok`, or `low` when a direction lasted under a second, too short to measure the line; `null` without a speed) and `note` (for `low`: which direction, and about how much data per test would measure it). Answers whether or not the test is on
- `POST /api/speedtests/run`: Starts a test now, in the background: `202 {"started": true}`. `409 {"started": false, "reason"}` when the test is off, one is already running, or this month's data cap is reached (it ignores the window and a busy line); 503 before the app has started. Needs the token when one is set; rate limited (3 in 10 minutes)
- `POST /api/mtr/trace`: `{"target_host", "family"}`: an on-demand traceroute, hop by hop (the Trace dialog). Rate limited (6 a minute)
- `WS /ws`: The live stream the dashboard listens to (one message per probe round); the message is described in [architecture.md](architecture.md)
- `GET /api/incidents?limit=100&offset=0`: Network incidents, newest first (`scope`, `include_expected`); `offset` skips the newest, so a client loads a first page and older ones on demand
- `GET /api/incidents/dossier`: Evidence dossier for the ISP (window up to 30 days); conclusions are derived from the data, expected incidents are excluded and counted
- `GET /api/ai-pack?scope=window&hours=24&level=full&detail=short`: The AI troubleshooting pack, one Markdown document for a chat model (`markdown`, the exact text), with `modules` (`id`, `title`, `chars`, `budget_chars`, `trimmed` per module section included) and `roles`, the "who is who" key that is never part of the text. `scope=window` takes `hours` (1 to 168; the dashboard offers 1, 24 and 168), `scope=incident` takes `incident_id` (an hour on each side; 404 for an unknown id, 422 when `incident_id` and `scope` disagree). `level`: `full` (the default), `vague` or `anonymized`; `detail`: `short` (the default) or `detailed`; times in the zone set in Settings (`general.timezone`), else in `tz` (the viewer's IANA zone, `America/New_York`; the dialog sends the browser's), else at `tz_offset_minutes` (as in `/api/report`), else UTC, which the header then says; a named zone's daylight-saving changes are followed and named. Anyone who can view the dashboard may ask for any level; with viewing without the token on, that includes Full. Rate limited (6 a minute) and one build at a time (429 while another runs). Counted in `pengumon_ai_pack_exports_total`.
- `GET /api/status`: Current diagnosis, including `load_pct`
- `GET /api/themes`: Every theme, built in and custom: `{"default" (the theme id Settings sets, `default` when it names none), "effects" (Settings > Display > Animated effects), "themes": [{id, name, description, modes, tags, custom, skin, effect (the effect module's URL or null), effect_on, css (its stylesheet's URL, built-in themes with one), views (card id: the URL of the theme's drawing of that card), chart (its chart style), theme (the theme as an importable theme.json: every color per mode)}], "looks" (the page's short list, as <html data-looks>)}`. The page itself starts from `<html data-looks>`; docs/themes.md
- `GET /api/devices`: Every configured device: id, driver, host, url, model, `label` (what it is and its model, e.g. "ASUS router · RT-AX88U"; its id when no model is set), enabled, capabilities (`offers`), line technology, poll interval, `has_credentials`, and its last poll (`status`, `last_poll`, `error_kind`, `error_message`). Never a credential
- `GET /api/devices/{id}`: The device's latest snapshot (polled once if there is none yet); 404 for an unknown id
- `POST /api/devices/{id}/credentials`: `{"username", "password"}`: stored in `data/secrets.json` and tested with one poll (`success`, `snapshot`, and `overridden_by` when a higher-ranked source will win at the next start); same-origin only, rate limited with the other credential endpoints; a 422 never echoes the submitted values
- `POST /api/discovery`: Starts discovering the path (`{"asn_lookup": true, "snmp": false, "device_checks": true}`, all optional) and answers `{"id"}`; a second start while one runs returns the running id; rate limited. Never applies anything. What it sends where: [discovery.md](discovery.md)
- `GET /api/discovery/{id}`: A run's `state` (`running`, `done`, `failed`), `progress`, `draft` (`{"hops": [...]}`, path hops with `evidence`, `confidence`, `suggested_driver` and `status` new/same/changed/gone; empty until done) and `notes`; 404 for an unknown id or one finished over ten minutes ago
- `GET /api/notifications`: The server's alert channels, `{"channels": [{name, kind (ntfy or webhook), format, host}]}`; never a URL path, query, header or secret
- `POST /api/notifications/test`: Sends a test message through every channel now and answers `{"channels": [{name, ok, detail}]}` (`detail` is a short reason in plain words, such as `the server answered 404`, `could not connect` or `timed out`); 409 when no channel is configured; rate limited
- `GET /api/features`: Per feature (`line_load`, `line_health`, `router_system`, `clients`, `mesh`): `available`, `reason_kind` (`available`, `none`, `disabled`, `not_configured`), the devices that provide it, and otherwise why not (e.g. `router (asuswrt) provides wan_counters but is not configured: no password`)
- `GET /api/devices/{id}/clients`: The connected clients: `{ts, nodes, clients, summary: {online, wired, by_band, by_node}, privacy}`. Needs the `client_inventory` capability (400 otherwise, 404 for an unknown id); rate limited. What it shows of each client follows `privacy.client_details`, see "Clients and mesh" below
- `GET /api/devices/{id}/clients/history?start_time=&end_time=`: Client counts over time (online, wired, per band, per mesh node), bucketed on the server to at most 300 points (each the mean of its bucket); defaults to the last 24 hours. Counts only, never a client
- `GET /api/devices/{id}/history?start_time=&end_time=`: The stored polls (router metrics: WAN rates, CPU, memory, clients) of a device that offers `wan_counters` or `router_system`, oldest first, padded two minutes each side; defaults to the last hour. 404 for an unknown id, 400 for a device without those capabilities
- `GET /api/devices/{id}/history?start_time=&end_time=&bucket_seconds=&max_points=`: The same router history, bucketed on the server when `bucket_seconds` and/or `max_points` is given (the default raw answer above is unchanged otherwise): `{"bucket_seconds", "start_time", "end_time", "fields", "rows", "capacity"}`. Rows follow `fields`: bucket start (epoch s) then avg and max of WAN rx and tx Mbps, CPU %, memory % (`rx_avg`, `rx_max`, `tx_avg`, ... `mem_max`) and `clients_avg`, `clients_max`; a figure no poll in the bucket measured is `null`; no padding. The width is at least `bucket_seconds` and wide enough for `max_points` buckets (default 400, at most 1000), rounded to 10 s or whole minutes, so any range returns about 400 rows (about 25 KB). The range is clipped to the 7 day retention. `capacity` is `{"down_mbps", "up_mbps"}`, the line capacity in use (`null` where unknown), for a reference line. 422 for a bad parameter
- `GET /api/devices/{id}/channels/history?start_time=&end_time=&channel_id=&direction=`: Per-channel history of a device whose access line is DOCSIS: `{"fields", "rows"}` with compact rows `[ts, direction ("d" or "u"), channel_id, power_dbmv, snr_db, uncorrectables_recent]`, oldest first (`snr_db` is `null` for upstream channels; `ts` is whole seconds). Defaults to the last 24 h, clipped to the 7 day retention; `channel_id` and `direction` (`downstream` or `upstream`) narrow it. About 112 KB for 24 h and 780 KB for 7 d of a 37 channel modem polled every 15 min (before gzip). 404 for an unknown id, 400 for a device without `access_line` or whose line is not DOCSIS

## Where the routes live

One router module per feature in `src/pengumon/api/`: `targets.py`, `path.py` (the path editor's routes), `incidents.py` (list, inspect, dossier), `history.py`, `reports.py` (report, loss grid, uptime, AI pack), `ops.py` (info, status, health, traceroute, `/metrics`), `notifications.py` (the alert channels and the test message), `settings.py` (the Settings page; its bodies are in `settings_models.py`), `settings_preview.py` (the outage-rules preview), `themes.py` (the themes list), `devices.py`, `services.py`, `clients.py`, `discovery.py`, `speedtests.py` (the speed test), `live.py` (the `/ws` websocket) and `access.py` (the sign-in). `common.py` has what they share (`StateDep`, `json_response`, `device_or_404`). `api/router.py` lists every router in `ROUTERS` and mounts them; it holds no handlers (`tests/test_api_routers.py`). A new endpoint goes in its feature's module in `api/`, also for a feature that is a component (docs/architecture.md, "Where things live").

## The probe path's history, preview and route check

Added with the path editor's versions, preview and "Your path changed" notice. Each is under `/api/path`, needs the
token when one is set, and changes nothing that is live unless it says so.

- **`POST /api/path/preview`** takes `{"hops": [...]}` (a draft, as `PUT /api/path` takes it) and answers
  `{"id", "probed"}`. Every enabled hop whose address and family no live hop has is probed once a second for 30 s,
  through the app's ICMP backend, and nothing is recorded. A browser's new preview stops its previous one. At most two
  run at once in the app: a third gets 429 with `Retry-After: 30`. Rate limited.
- **`GET /api/path/preview/{id}`** answers `{"state": "running" | "done", "elapsed_s", "hops"}`. Each entry of `hops`
  (by hop id, only the probed ones) has `sent`, `received`, `loss_pct`, `median_ms`, `last_ms` and `kind` (`reply`,
  `timeout`, `unreachable` or `ipv6_unavailable`; a time-exceeded answer or an error counts as `unreachable`).
- **`DELETE /api/path/preview/{id}`** stops it and answers its final body. The editor sends it when it closes.
- **`GET /api/path/versions`** lists the applied paths, newest first (the last 50), each `{"revision", "applied_at",
  "hop_count", "summary"}`; `summary` says what changed (hops added, removed, renamed, a role or an address).
  Revision 1 is the import.
- **`GET /api/path/versions/{revision}`** answers `{"revision", "hops"}`, that version as a draft. Restoring it is
  applying that draft with `PUT /api/path`; a hop that comes back keeps its history.
- **`GET /api/path/events`** lists what happened to hops, oldest first: `{"at", "kind": "address_changed",
  "target_id", "old", "new", "revision"}`. `?target_id=` narrows it to one hop. The target history dialog marks them.
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
  viewing without it, the OTLP host, the version) is sent only to someone who may edit.
- **`GET /api/settings/values`**: `{"revision", "values": {key: effective value}}`.
- **`GET /api/settings/grafana-links`**: The header's Grafana links as they are now (the page refills its menu after a Save of `display.grafana_url`).

- **`POST /api/settings/validate`** (60 a minute): a change `{values: {key: value | null}, lists: {name: [...] |
  null}}` against the current state -> `{ok, errors, warnings, summary}`. Nothing is applied. `warnings` are not
  refusals (retention under 7 days, lowering the raw retention, client details shown to anyone without a token);
  `summary` is the version line the save would write.
- **`PUT /api/settings`** (20 a minute, bucket `settings`): `{base_revision, values, lists, secrets}`. `values[key] =
  null` resets a key (Reset), `lists[name] = null` a list, `secrets[slot] = ""` clears a slot; a secret's value goes
  only this way, never back. `?source=restore` marks the save of a version loaded with `GET /versions/{n}`,
  `?source=yaml_import` one of an Import draft (default `save`). The order: validate (422, or 400 for a key or slot a
  variable pins), write secrets.json (500, nothing saved), commit, apply live. A key or list the request touches that
  changed between `base_revision` and now answers 409 `{detail, revision, conflicts}`; untouched parts may have
  changed. Answer: `{revision, summary, applied, restart_needed, live_failed}`; `live_failed` parts are saved and
  apply at the next start.
- **`POST /api/settings/preview`** (6 a minute): `{values, days}` (`classifier.*`, `line.down_mbps` / `up_mbps` and
  `alerts.*` keys only; `days` defaults to the smaller of 7 and the retention) -> `{"id"}`. The stored samples are
  replayed with those draft rules in a separate process; nothing is changed. 422 for a key it cannot replay or a value
  out of range, 429 with `Retry-After: 30` while another replay runs. **`GET /api/settings/preview/{id}`** answers
  `{state: running | done | failed | canceled, progress, window, current, draft, recorded, changes, notes}` (the
  incident counts as recorded, as the current rules give them, and under the draft; polling keeps the job alive), and
  **`DELETE /api/settings/preview/{id}`** stops it. This is the Outage rules preview on the Settings page.
- **`GET /api/settings/versions`**: the kept versions (the last 50), newest first: `[{revision, applied_at, source:
  import | save | restore | yaml_import, summary}]`. **`GET /api/settings/versions/{n}`**: `{revision, values,
  lists}`, that version as a draft against the current state (null for a value stored now and not then); 404 when it
  is no longer kept.
- **`GET /api/settings/export`**: the stored values and the set lists as YAML in config.yaml syntax. No secret: a
  webhook has no URL and its headers name their secrets. Needs the token even when viewing without it is allowed.
- **`POST /api/settings/import`** (20 a minute): YAML in the body (an export, or a whole config.yaml; the path
  import's limits: 256 KiB, depth 16, no anchors, aliases or merge keys) -> `{values, lists, notes}`, a draft.
  Nothing is applied. Bootstrap keys and `targets:` are ignored, webhook URLs and plain header values are left out,
  each with a note.

Errors of the settings endpoints that change things use one shape: `{"detail", "errors": [{"where": "values.<key>" |
"lists.<name>[<i>].<field>" | "secrets.<slot>", "message"}]}`; a message never repeats the submitted value.

## Sign-in

Who can open the dashboard ([security.md](security.md#who-can-open-the-dashboard); `api/access.py`). The password itself
is never in an answer, except the one that set it. Every route but the GET needs the password
once one is set; the three that change something share a limit of 10 a minute (bucket `access`).

- **`GET /api/access`**: `{"mode": "open" | "password" | "locked", "from_environment", "pending", "https"}`.
  `locked`: secrets.json is damaged or missing and a password was set, so nobody can sign in. `pending`: the first-run
  question is open.
- **`POST /api/access/setup`**: `{choice: "open" | "password", password?, viewers?}`, the first-run question. 409 once
  it has been answered (by anyone). With a password: `{mode, password, viewers_saved}` and a session cookie for this
  browser; `viewers` turns on `access.allow_viewers` as a settings save of its own.
- **`PUT /api/access/password`**: `{password}` (8 to 128 characters, no control characters; surrounding spaces are
  dropped) sets or changes it, in force at once: every other browser is signed out, this one gets a new session cookie.
  409 while `PENGUMON_ACCESS_TOKEN` sets it. **`DELETE /api/access/password`** removes it.

Errors: `{"detail": "<sentence>"}`. A 422 never repeats the submitted value.
