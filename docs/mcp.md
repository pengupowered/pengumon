# 🤖 MCP: let an AI assistant ask Pengumon

Pengumon can answer an AI assistant's questions about your network ("is the internet OK?", "what went wrong last night?")
through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP). The endpoint is **read-only**: no tool
changes a setting, starts a probe or writes anything. It is **off by default**; a public install grows no endpoint
unless you ask for one.

## Turn it on

Settings > Sign-in > **AI assistants (MCP)** > **Allow AI assistants to read (MCP)**, or `mcp: {enabled: true}`. The
setting applies at once, with no restart; while it is off, every request to `/mcp` (or `/mcp/`), whatever its method, is a
plain `404`.

The endpoint is `POST /mcp` on the same address and port as the dashboard (`http://<host>:8000/mcp`). It speaks MCP's
"streamable HTTP" transport in its simplest form: one JSON-RPC message per `POST`, a JSON answer, no sessions, no event
stream (every method but `POST` answers `405`). Batches are refused (`-32600`), also under `2025-03-26`, the one
revision that allowed them; the MCP clients we know send none. Protocol revisions `2025-11-25`, `2025-06-18`,
`2025-03-26` and `2024-11-05` (the `initialize` handshake) are spoken. A client that probes for the newer per-request
revision (`server/discover`) is told "method not found" and falls back to `initialize`, as the specification says.

## Connect Claude Code

```
claude mcp add --transport http pengumon http://<host>:8000/mcp --header "Authorization: Bearer <read token>"
```

Use the **read token**, not the password: make one in Settings > Sign-in > Read token (it asks for the current password,
and shows the token once, with Copy). It can do what MCP needs and nothing more, so an assistant's config file (or
anyone who reads it) cannot change your settings or sign you out, and you can replace or remove it without changing the
password. A new password removes it: make a new one and update the config. Leave the `--header` off when
no password is set. Other MCP clients need the same two things: the URL and the header.

## Sign-in

`/mcp` has the sign-in the API has. **With a password set** (Settings > Sign-in, or `PENGUMON_ACCESS_TOKEN`), a request
needs `Authorization: Bearer <the read token>` (or the password itself, which also works but can change everything);
without either the answer is `401`. Five wrong tokens a minute from one address, and that address gets `429` for a
minute. **With no password set, the endpoint is open** to anyone who can reach the port, like the rest of the API, and
there is no read token. People who may only look at the dashboard ("Others may look, but not change anything") cannot use
MCP without the read token: it is a `POST`, which a viewer may not send.

## The tools

`incidents` takes `limit` (default 20, at most 200); the other lists are short (one entry per hop, day, device
or service). Times are ISO 8601 in the zone set in Settings > Display (UTC when
none). Results come back as compact JSON, as text and as structured content. A tool's own failure (an unknown incident id,
the app still starting) is a result with `isError: true` and a plain sentence; unknown tools and bad arguments are
JSON-RPC `-32602` errors.

| Tool | Arguments | Returns |
|---|---|---|
| `status` | none | The current diagnosis (`status`, `cause`, `title`, `description`, `load_pct`), the open incident if any (its title and description name the hops, or by role word with the setting off, see below), and per stage of the path (`local`, `gateway`, `isp`, `public`, `dns`, `custom`; only those with hops) the worst hop status, mean latest latency, worst recent loss and jitter. No hop names or addresses in the stages. |
| `incidents` | `period` 1h, 24h, 7d (default), 30d, 90d, all; `limit` | Network incidents, newest first: `id`, `outage_id`, `start`, `end`, `duration_seconds`, `resolved`, `kind`, `cause`, `title`, `severity`, `load_pct`, `expected_from_load`. Also `outages` (the period's outages, newest first, capped like the incidents) with `id`, `start`, `end`, `ongoing`, `duration_seconds`, `down_seconds`, `drops`, `critical_drops`, `severity`, `places`, `title`, `likely_maintenance`, `incident_ids`, `outages_total_in_period` and `outage_note` (a sentence on how outages are joined, with the current gap). An outage joins the incidents at neighboring places of the path that follow each other within `incidents.group_gap_minutes` (a flapping line is several incidents but one outage); `likely_maintenance` is a guess from the hour of the day. And `latency_spikes`: the period's brief latency spikes (latency or jitter with no loss that ended within `classifier.latency_incident_after_seconds`, 2 minutes by default, so not incidents): `count` (expected ones left out, counted in `expected`), `by_stage` (`stage`, `count`), `per_day` (`day`, `count`, `partial`: the first and last day are cut by the period), `incident_after_seconds` and a `note`. |
| `incident` | `id` | One incident: description, affected hops, the path trace taken when it began (as traced; with "Answers use real names and addresses" off, hops on your own network show as `private`, without address or name: a private address, the address of the router or gateway target, and every hop before the first one at an ISP target's address), and the evidence of `GET /api/incidents/{id}/inspect` (per-hop loss and latency over the incident, the modem correlation, the device events around it and the router's traffic in the hours it covers, clients named as for `traffic`) without the raw samples, whose counts it gives. |
| `report` | `period` 1h, 24h, 7d (default), 30d, 90d, all | (`all`: the whole probe retention, from the oldest stored probe, in calendar days up to 90 days and local weeks beyond; `block_seconds` says which. For `incidents` it is the retention of the probes and for `speed_tests` that of the speed tests: both are settings, 180 days by default.) (1h: the last four 15-minute blocks.) Availability per hop with outage, degraded and busy block counts, availability per calendar day, incidents by cause, the worst hours of the day for loss. One rolling window ending now (`window_start`, `window_end`) for every part; the first and last day of `daily_availability` are cut by it (`partial`, with the `hours` they hold) and so are the first and last of the uptime blocks (`partial_blocks`). |
| `devices` | none | The modem and router: what each is, the status of its last poll, its latest readings and grades, client counts (online, wired, by band, mesh nodes) and never who the clients are. No addresses, no credentials; a failed poll is told in fixed words by its kind (the driver's own message can name the device's address or login; the dashboard shows it). |
| `services` | none | The host app checks (`GET /api/services`). |
| `speed_tests` | `period` 1h, 24h, 7d (default), 30d, 90d, all; `limit` (default 20, at most 200) | Is the line as fast as the plan? Whether the [speed test](speedtest.md) is on, its provider (by name, never your LibreSpeed server's address), the next scheduled run, this month's data used against the monthly limit, the plan speed (Settings > Your line), a summary of the period (median and lowest download and upload, tests under half the plan speed, the bufferbloat grade) and the tests newest first: time, trigger, status, the reason a test failed, download and upload Mbps (the line's rate the router measured during the test, `measured_by: "router"`, else the test's own, `"test"`; the test's own figures beside them, and a `traffic_note` when the household's traffic took most of the line), idle and loaded latency, grade, MB moved. Empty with a note while the test is off. |
| `traffic` | `hours` 1 to 720 (default 24); `top` 1 to 50 (default 10) | Who used the internet: the router's own count (its Traffic Analyzer, per clock hour, published after the hour ends) over a rolling window of the last `hours` hours ending at the newest hour the router published (`from`, `to`): `available` and `reason`, the totals, the three busiest hours, the devices ranked by download with their top apps and `main_app` (the app a device's traffic was mostly), and the apps of all devices together. The router counts apps only per 24 hours, so the apps are the sum of its 24-hour counts that lie inside the window (`apps_covered` says which), and `apps_not_broken_down` is the rest of the total, so the apps plus it add up to `totals`; under 24 hours no count lies inside and nothing is broken down. A device is named as the dashboard names it for the caller (`device`, `vendor` and, where the dashboard shows addresses, `address`); with "Answers use real names and addresses" off, only by its kind (`game console`, `phone`, `computer`, ... or `device`) and `vendor`. `available: false` with a reason when no router reports traffic. |
| `ai_pack` | `scope` window or incident; `hours` 1 to 720 (default 24); `incident_id`; `level` full (default), vague, anonymized; `detail` short (default), detailed | The [AI pack](ai-pack.md) as Markdown, with its module list. At most 6 builds a minute, as for `GET /api/ai-pack`, and one build at a time (shared with the dashboard): over either, an `isError` result says when to try again. |

Every tool is annotated `readOnlyHint: true` and `openWorldHint: false`. The tools read what the API routes read, in the
same process; none sends anything to the network.

## What a tool returns: real names, or role words

Settings > Sign-in > **AI assistants (MCP)** > **Answers use real names and addresses** (`mcp.real_names`, **on** by
default; it applies at once). The reason it is on: an assistant that is given fake or blanked names has to guess, and
guesses wrong.

- **On.** The tools return what the dashboard shows the same caller: hops and devices by their names, host names and
  home addresses, the incident titles and descriptions as the classifier wrote them, the path trace as traced.
  Connected clients follow `privacy.client_details` (Settings > Privacy) for *that caller*, as the dashboard does: a
  caller with the password, or anyone while `client_details` is "Always", gets their names and addresses
  (`traffic`, and the traffic hours of `incident`); the read token, or no password set under the default, gets a
  maker and the last four characters of the hardware address. Hardware addresses are shown only where client details are.
- **Off.** Hops and devices are role words ("Router Disconnect", "reaching Router"), clients are their kind and maker
  ("a game console (Sony)"), and every tool but `services` and `ai_pack` replaces a home network address, a MAC address
  or a home host name (`.lan`, `.local`, `.home.arpa`, ...) with `[removed]` wherever it appears (a hop named after its
  host name, a failed speed test's error); the incident's path trace shows your own side as `private`.
- **Either way**, never: the sign-in token, session data, webhook addresses and header secrets, the ntfy address and
  token, device usernames and passwords, SNMP communities, and a password or token in a URL (`[removed]`). `devices`
  gives counts of clients only and no device address or login: a failed poll is told in fixed words by its kind (the
  driver's message can name the device's address or login; the dashboard shows it). `services` shows your own check
  URLs as you typed them, and the AI pack keeps its own redaction levels: use `anonymized` when its text will leave your
  household.

What the assistant sees goes to the company that runs it. Tests plant a marker in every secret place and check that no
tool's output holds it, in both modes, and plant household names and addresses and check each tool's output with the
setting on and off.

## Security notes

- **Plain HTTP on the LAN sends the token in clear.** Anyone who can watch your network traffic can read it. If that
  matters, serve Pengumon over HTTPS (`PENGUMON_TLS_CERT` and `PENGUMON_TLS_KEY`, [security.md](security.md#https)) and
  use an `https://` URL. With the read token, what such a person gets is read access, not the password.
- The guards of the API apply: the `Host` header must be an allowed one (a DNS-rebinding page is refused), a browser's
  foreign `Origin` is refused (`403`; clients that are not browsers send none), the body size is limited, and there is no
  CORS. `POST /mcp` is rate limited to 120 a minute, and `ai_pack` to 6 builds a minute.
- Each tool call is counted in `pengumon_mcp_calls_total{tool,outcome}` ([telemetry.md](telemetry.md)), and the Grafana
  dashboard has a panel for it.
