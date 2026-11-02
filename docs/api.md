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
- `GET /api/report?days=30`: Reliability report: per-day availability per hop, incident count and minutes by cause (expected incidents excluded), worst hours of the day. `tz_offset_minutes` buckets days and hours in the viewer's timezone
- `GET /api/incidents/dossier`: Evidence dossier for the ISP (window up to 30 days); conclusions are derived from the data, expected incidents are excluded and counted
- `GET /api/status`: Current diagnosis, including `load_pct`
- `GET /api/devices`: Every configured device: id, driver, host, url, model, `label` (what it is and its model, e.g. "ASUS router · RT-AX88U"; its id when no model is set), enabled, capabilities (`offers`), line technology, poll interval, `has_credentials`, and its last poll (`status`, `last_poll`, `error_kind`, `error_message`). Never a credential
- `GET /api/devices/{id}`: The device's latest snapshot (polled once if there is none yet); 404 for an unknown id
- `POST /api/devices/{id}/credentials`: `{"username", "password"}`: stored in `data/secrets.json` and tested with one poll (`success`, `snapshot`, and `overridden_by` when a higher-ranked source will win at the next start); same-origin only, rate limited with the other credential endpoints; a 422 never echoes the submitted values
- `POST /api/discovery`: Starts discovering the path (`{"asn_lookup": true, "snmp": false, "device_checks": true}`, all optional) and answers `{"id"}`; a second start while one runs returns the running id; rate limited. Never applies anything. What it sends where: [discovery.md](discovery.md)
- `GET /api/discovery/{id}`: A run's `state` (`running`, `done`, `failed`), `progress`, `draft` (`{"hops": [...]}`, path hops with `evidence`, `confidence`, `suggested_driver` and `status` new/same/changed/gone; empty until done) and `notes`; 404 for an unknown id or one finished over ten minutes ago
- `GET /api/notifications`: The server's alert channels, `{"channels": [{name, kind (ntfy or webhook), format, host}]}`; never a URL path, query, header or secret
- `POST /api/notifications/test`: Sends a test message through every channel now and answers `{"channels": [{name, ok, detail}]}` (`detail` is a short reason such as `HTTP 404` or `timed out`); 409 when no channel is configured; rate limited
- `GET /api/features`: Per feature (`line_load`, `line_health`, `router_system`, `clients`, `mesh`): `available`, `reason_kind` (`available`, `none`, `disabled`, `not_configured`), the devices that provide it, and otherwise why not (e.g. `router (asuswrt) provides wan_counters but is not configured: no password`)
- `GET /api/devices/{id}/clients`: The connected clients: `{ts, nodes, clients, summary: {online, wired, by_band, by_node}, privacy}`. Needs the `client_inventory` capability (400 otherwise, 404 for an unknown id); rate limited. What it shows of each client follows `privacy.client_details`, see "Clients and mesh" below
- `GET /api/devices/{id}/clients/history?start_time=&end_time=`: Client counts over time (online, wired, per band, per mesh node), bucketed on the server to at most 300 points (each the mean of its bucket); defaults to the last 24 hours. Counts only, never a client
- `GET /api/devices/{id}/history?start_time=&end_time=`: The stored polls (router metrics: WAN rates, CPU, memory, clients) of a device that offers `wan_counters` or `router_system`, oldest first, padded two minutes each side; defaults to the last hour. 404 for an unknown id, 400 for a device without those capabilities
- `GET /api/devices/{id}/history?start_time=&end_time=&bucket_seconds=&max_points=`: The same router history, bucketed on the server when `bucket_seconds` and/or `max_points` is given (the default raw answer above is unchanged otherwise): `{"bucket_seconds", "start_time", "end_time", "fields", "rows", "capacity"}`. Rows follow `fields`: bucket start (epoch s) then avg and max of WAN rx and tx Mbps, CPU %, memory % (`rx_avg`, `rx_max`, `tx_avg`, ... `mem_max`) and `clients_avg`, `clients_max`; a figure no poll in the bucket measured is `null`; no padding. The width is at least `bucket_seconds` and wide enough for `max_points` buckets (default 400, at most 1000), rounded to 10 s or whole minutes, so any range returns about 400 rows (about 25 KB). The range is clipped to the 7 day retention. `capacity` is `{"down_mbps", "up_mbps"}`, the line capacity in use (`null` where unknown), for a reference line. 422 for a bad parameter
- `GET /api/devices/{id}/channels/history?start_time=&end_time=&channel_id=&direction=`: Per-channel history of a device whose access line is DOCSIS: `{"fields", "rows"}` with compact rows `[ts, direction ("d" or "u"), channel_id, power_dbmv, snr_db, uncorrectables_recent]`, oldest first (`snr_db` is `null` for upstream channels; `ts` is whole seconds). Defaults to the last 24 h, clipped to the 7 day retention; `channel_id` and `direction` (`downstream` or `upstream`) narrow it. About 112 KB for 24 h and 780 KB for 7 d of a 37 channel modem polled every 15 min (before gzip). 404 for an unknown id, 400 for a device without `access_line` or whose line is not DOCSIS

## Where the routes live

One router module per feature in `src/pengumon/api/`: `targets.py`, `path.py` (the path editor's routes), `incidents.py` (list, inspect, dossier), `history.py`, `reports.py` (report, loss grid, uptime), `ops.py` (info, status, health, traceroute, `/metrics`), plus `devices.py`, `clients.py`, `services.py` and `engine/discovery/api.py`. `common.py` has what they share (`StateDep`, `json_response`, `device_or_404`). `api/__init__.py` lists the feature routers in `ROUTERS`; `api/router.py` only mounts them and holds no handlers (`tests/test_api_routers.py`). A new endpoint goes in its feature's module.

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
