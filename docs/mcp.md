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
| `status` | none | The current diagnosis (`status`, `cause`, `title`, `description`, `load_pct`), the open incident if any, and per stage of the path (`local`, `gateway`, `isp`, `public`, `dns`, `custom`; only those with hops) the worst hop status, mean latest latency, worst recent loss and jitter. No hop names or addresses. |
| `incidents` | `period` 24h, 7d (default), 30d; `limit` | Network incidents, newest first: `id`, `outage_id`, `start`, `end`, `duration_seconds`, `resolved`, `kind`, `cause`, `title`, `severity`, `load_pct`, `expected_from_load`. Also `outages` (the period's outages, newest first, capped like the incidents) with `id`, `start`, `end`, `ongoing`, `duration_seconds`, `down_seconds`, `drops`, `critical_drops`, `severity`, `places`, `title`, `likely_maintenance`, `incident_ids`, `outages_total_in_period` and `outage_note` (a sentence on how outages are joined, with the current gap). An outage joins the incidents at neighboring places of the path that follow each other within `incidents.group_gap_minutes` (a flapping line is several incidents but one outage); `likely_maintenance` is a guess from the hour of the day. |
| `incident` | `id` | One incident: description, affected hops, the path trace taken when it began (hops on your own network show as `private`, without address or name: a private address, the address of the router or gateway target, and every hop before the first one at an ISP target's address), and the evidence of `GET /api/incidents/{id}/inspect` (per-hop loss and latency over the incident, the modem correlation, the device events around it and the router's traffic in the hours it covers, clients by kind only) without the raw samples, whose counts it gives. |
| `report` | `period` 24h, 7d (default), 30d | Availability per hop with outage, degraded and busy block counts, availability per day, incidents by cause, the worst hours of the day for loss. |
| `devices` | none | The modem and router: what each is, the status of its last poll, its latest readings and grades, client counts (online, wired, by band, mesh nodes) and never who the clients are. No addresses, no credentials; a failed poll is told in fixed words by its kind (the driver's own message can name the device's address or login; the dashboard shows it). |
| `services` | none | The host app checks (`GET /api/services`). |
| `speed_tests` | `period` 24h, 7d (default), 30d; `limit` (default 20, at most 200) | Is the line as fast as the plan? Whether the [speed test](speedtest.md) is on, its provider (by name, never your LibreSpeed server's address), the next scheduled run, this month's data used against the monthly limit, the plan speed (Settings > Your line), a summary of the period (median and lowest download and upload, tests under half the plan speed, the bufferbloat grade) and the tests newest first: time, trigger, status, the reason a test failed, download and upload Mbps (the line's rate the router measured during the test, `measured_by: "router"`, else the test's own, `"test"`; the test's own figures beside them, and a `traffic_note` when the household's traffic took most of the line), idle and loaded latency, grade, MB moved. Empty with a note while the test is off. |
| `traffic` | `hours` 1 to 720 (default 24); `top` 1 to 50 (default 10) | Who used the internet: the router's own count (its Traffic Analyzer, per clock hour, published after the hour ends) over the last complete `hours`: `available` and `reason`, the totals, the three busiest hours, the devices ranked by download with their top apps and `main_app` (the app a device's traffic was mostly), and the apps of all devices together: for under 48 `hours` the router's latest 24-hour window, for 48 or more the sum of the whole local days up to the last midnight (`apps_days` says which and how many had figures). A device is its kind (`game console`, `phone`, `computer`, ... or `device`) and `vendor`, never its name or address. `available: false` with a reason when no router reports traffic. |
| `ai_pack` | `scope` window or incident; `hours` 1 to 168 (default 24); `incident_id`; `level` full (default), vague, anonymized; `detail` short (default), detailed | The [AI pack](ai-pack.md) as Markdown, with its module list. At most 6 builds a minute, as for `GET /api/ai-pack`, and one build at a time (shared with the dashboard): over either, an `isError` result says when to try again. |

Every tool is annotated `readOnlyHint: true` and `openWorldHint: false`. The tools read what the API routes read, in the
same process; none sends anything to the network.

## What a tool never returns

The sign-in token, session data, webhook addresses and header secrets, the ntfy address and token, device usernames and
passwords, SNMP communities. Client names, addresses and MAC addresses are not returned at all, only counts, whatever
`privacy.client_details` says (`traffic` and `incident` describe a device by its kind and maker). The AI pack keeps its own redaction levels: use `anonymized` when its text will leave
your household. A test plants a marker in every one of these places and checks that no tool's output holds it.

No device host either: the incident titles and descriptions (in `status`, `incidents` and `incident`) name the hops by
their role, as the AI pack does at `vague` ("Router Disconnect", "reaching Router" instead of the router's name and
address), and every tool but `services` and `ai_pack` replaces a home network address, a MAC address, a home
host name (`.lan`, `.local`, `.home.arpa`, ...) or a password in a URL with `[removed]` wherever it appears (a hop named
after its host name, a target id made from its address, a failed speed test's error). A second test plants such values
in the stored data and checks every tool's output.

## Security notes

- **Plain HTTP on the LAN sends the token in clear.** Anyone who can watch your network traffic can read it. If that
  matters, serve Pengumon over HTTPS (`PENGUMON_TLS_CERT` and `PENGUMON_TLS_KEY`, [security.md](security.md#https)) and
  use an `https://` URL. With the read token, what such a person gets is read access, not the password.
- The guards of the API apply: the `Host` header must be an allowed one (a DNS-rebinding page is refused), a browser's
  foreign `Origin` is refused (`403`; clients that are not browsers send none), the body size is limited, and there is no
  CORS. `POST /mcp` is rate limited to 120 a minute, and `ai_pack` to 6 builds a minute.
- Each tool call is counted in `pengumon_mcp_calls_total{tool,outcome}` ([telemetry.md](telemetry.md)), and the Grafana
  dashboard has a panel for it.
