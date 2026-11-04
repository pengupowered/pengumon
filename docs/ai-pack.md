# Ask AI: the troubleshooting pack

Pengumon has no AI built in and sends nothing to one. Instead it writes one Markdown document, the troubleshooting pack,
that you paste into any chat assistant (ChatGPT, Gemini, Claude, a local model). The pack holds Pengumon's own
findings and instructions for the assistant, so a model that cannot reach your network can still tell you what is
most likely wrong, where, and what to test next.

For a complaint to your provider, send the ISP dossier (Dossier in the header) instead: the pack is for working out
what is wrong; the dossier is the evidence for the provider.

## Using it

1. Open **Ask AI**: the sparkle button in the header (a time window), **Ask AI** in the incident inspector (that
   incident, with an hour on each side), or the button in the dossier.
2. Choose what to cover (the past hour, 24 hours or 7 days, or the incident), how long (Short, or With more
   examples) and how much to share (Full, Vague or Anonymized, below). The preview is exactly the text you will
   copy, with a token estimate.
3. **Copy** (or **Download .md**) and paste it into a new chat.
4. Before sending, fill in the last section, "What went wrong for you?": one line on what goes wrong, on which device,
   and roughly when ("video calls on the laptop freeze for a few seconds, most evenings"). The assistant answers from
   the data either way, but a symptom points it at the right incidents.

Tips:

- **Larger models answer better.** The pack is written so small models follow it too, but in our tests the larger
  ones weighed the evidence more carefully. A 7-day pack is about 5,000 to 6,000 tokens,
  well within any current model.
- **Over plain HTTP**, Chrome may block **Download .md** as an insecure download. **Copy** works on every connection;
  the dialog says so in its footer.
- **Times** are in the zone set in Settings, else in your browser's zone, else UTC; the pack's first lines name the
  zone, and a daylight-saving change in the window is named where it matters.
- The same document is available from `GET /api/ai-pack` ([api.md](api.md)); dialog details are in
  [dashboard.md](dashboard.md#ask-ai-the-troubleshooting-pack).

## What is in the pack

A summary, not raw data: Pengumon's findings with the numbers behind them, sized for a chat.

| Section | What it holds |
|---|---|
| Header | the detail level in plain words, what the pack covers, Pengumon's version |
| For the AI assistant, How Pengumon measures, Questions | fixed instructions: cite the data, give confidences, ask only for facts that would change the answer; how probes, roles, "normal" and verdicts work; the five headings to answer under |
| The path and each hop's normal | one row per hop (Router, Gateway, ISP hops, Internet servers, DNS lookups): its 7-day normal latency, p90, jitter and loss next to the window's |
| Findings | the most important points first: how much went wrong overall, where loss starts, what coincided; one headline per device module |
| Incidents | the significant incidents in time order (20, or 40 with more examples), each with where it starts, Pengumon's verdict (*gateway*, *line* or *isp*), a confidence and what happened at the same time |
| Close-ups | the 3 (or 6) most significant incidents minute by minute, 10-second rows for an incident pack |
| A normal week | latency by time of day and, per day, incidents and lost minutes, plus the line's own columns where a module adds them |
| Your devices | one subsection per device module (below), or a short "What Pengumon cannot see here" without any |
| Host apps, changes, environment | host app checks, path and route changes, the ping backend, IPv6, gaps in the data |
| What went wrong for you? | your one line |

The verdicts are Pengumon's rule-based reading of the data ([diagnosis.md](diagnosis.md)): *gateway* means loss starts
at the modem or gateway while the router answers; *line* means loss starts at the provider's first hop while the
gateway answers (the line or the provider's first router); *isp* means the provider's network beyond that.

Never in the pack, at any level: descriptions you wrote, probe error strings, raw mesh node ids, and anything about an
individual client device (only counts). Credentials are never part of it.

## The three detail levels

More detail gives a better answer; less detail is for sharing the pack where you would rather not show your network.
Full is selected each time the dialog opens, and anyone who can view the dashboard may export any level.

| | Full | Vague | Anonymized |
|---|---|---|---|
| Your router, gateway and other home hops (names, addresses, reverse names) | shown | roles: "Router", "Gateway" | roles |
| Your devices' names and hosts; mesh node names | shown | roles: "Router", "Gateway", "Mesh node 2" | roles |
| Device models | shown | shown | removed |
| The provider's routers and public servers (addresses, reverse names) | shown | shown | roles: "ISP hop 2", "Internet 1" |
| The provider's name | shown | shown | removed |
| Incident titles | shown | names and addresses removed | the incident's kind in Pengumon's words |
| Dates | weekday, date, time, zone | weekday, date, time, zone | "Day 1" (the window's first day), time |
| Vendor and product words (an ASUS router, an Xfinity gateway) | shown | shown | removed; driver notes use their neutral wording |
| Measurements (latency, loss, line levels, rates, counts) | all | all | all |

For example, the same hop row reads `ISP hop 1 · cr01.example-isp.net (198.51.100.1)` at Full and Vague and
`ISP hop 1` at Anonymized; the router reads `Router · Example-Mesh hall (192.168.88.1)` at Full and `Router` at both
others.

Each value gets one role for the whole pack, so the assistant can still follow "Mesh node 2" from the findings to the
close-ups. **Who is who**, folded away in the dialog, is your key from roles back to names; it is never in the pack, so
you can read the assistant's answer against your own devices.

After the pack is built, a final scan reads the whole text again and replaces anything it should not hold at that
level: MAC addresses and passwords in URLs at every level; also home addresses and host names at Vague; any address,
date, host name or vendor word at Anonymized. The scan is a safety net: the rules above should already have removed
everything, and the tests require that it finds nothing.

Read the preview before you send it, especially at Full: it is your network's map.

## Device modules

What Pengumon reads from your devices adds a subsection to "Your devices", a headline to Findings and events to the
incidents' "At the same time". Which ones appear depends only on the devices configured, so a device that is switched
off still contributes its stored history.

| Module | Appears when a device offers | What it adds |
|---|---|---|
| Cable line (DOCSIS) | `access_line` with a DOCSIS line | channel power and SNR against the usual ranges, codeword error bursts, restarts; per-day lowest SNR and new uncorrectables |
| Line load | `wan_counters` | download and upload rates against the line's capacity, router restarts; per-day peak load, so a busy line is told apart from a fault |
| Router | `router_system` | CPU and memory, readings above the warning levels, times the router stopped answering |
| Wi-Fi and mesh | `client_inventory` (and `mesh`) | client counts per band and node, sudden drops in online clients, mesh nodes left with no clients, each node's backhaul (counts only, never a client) |

Each subsection starts with a short "How to read it" for the assistant (the ranges in force, filled in from your
configuration), then the findings and at most one small table. A driver may close its section with notes about itself
(how often it is read, what its status page lacks). A module with no readings in the window says so in one line.

The same modules feed OpenTelemetry and `/metrics` (line levels, codeword errors, WAN rates, router CPU and memory,
client and mesh counts), and the Grafana dashboard shows a row per module that appears only where a device reports it:
[telemetry.md](telemetry.md).

## Adding a module (developers)

A new device of a kind Pengumon knows (another DOCSIS modem, another router) needs only its driver
([devices.md](devices.md)): offering the capability is enough for the matching module, its events, redaction, gauges
and Grafana row.

A new kind of line (fiber, DSL) needs a technology contribution as well: a class meeting the `Contribution` protocol
in `capabilities/contrib.py`, registered in `lines/__init__.py` under `CONTRIBUTIONS`, next to its `WORDING`. The
capability modules are listed in `devices/contrib.py` (`CAPABILITY_CONTRIBUTIONS`). A contribution has:

- `id`, `title`, `layer` (`technology` or `capability`) and `budget_chars`, the most its prompt text, section and
  driver note may take together;
- `prompt_context(config)`: how to read its data, with the levels in force filled in from config; no addresses or
  device names;
- `collect(ctx)`: its facts, from the stored history of the window (`ContribContext`: the repository, the config,
  the devices, and the router's latest state while the app runs), with no HTTP and no driver, so it works offline;
- `events`: its `EventKind`s. Codes are technology-neutral where they can be (`line.error_burst`, `line.restart`,
  `line.signal_out_of_range`), so a new line technology corroborates the same verdicts; `supports` names the cause
  categories an event backs up, and `reach_seconds` how far from an incident it still counts;
- `emit(facts)` and `section(facts, detail)`: the events and the `ModuleSection` (headline, findings, at most one
  table, per-day columns);
- `redaction`: `RedactionHints`, what happens to each field at each level (keep, role, generic, drop or never), and
  `identify(facts)`, the strings that name its devices, so the redactor can replace them everywhere;
- `instruments`: the OpenTelemetry names it owns, each documented in [telemetry.md](telemetry.md), with labels that are
  only the device id and small fixed sets.

Facts refer to devices by role keys (`line_device`, `router_device`, `mesh_node:2`), never by name; the redactor turns
each key into the level's text.

A driver may add `pack_notes`, a `DriverNotes` with a `full` text and an `anonymized` one (or none). Notes state only
what the driver's own code establishes (its read intervals, its data source, firmware behaviour it works around),
never general advice about the vendor; placeholders such as `{poll}` are filled in from the device's configuration.

`tests/test_contributions.py` checks every registered contribution and driver note against this contract (budgets,
unique event codes, no names in the text, redaction coverage, instruments documented and recording), so a new one is
tested by being registered.
