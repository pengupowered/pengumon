# 📊 How Outage Root-Cause Diagnosis Works

Pengumon concurrently probes multiple network stages every second and classifies conditions in real-time:

- **All Nominal**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🟢 < 2ms
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🟢 < 20ms
  - DNS Query (`google.com`): 🟢 < 25ms
  - Diagnosed Root Cause: **Healthy**
- **Mesh / Wi-Fi Drop**
  - Local Mesh (`192.168.1.1`): 🔴 **Packet loss / high ping**
  - Gateway (`192.168.100.1`): 🔴 Loss
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🔴 Loss
  - DNS Query (`google.com`): 🔴 Fails
  - Diagnosed Root Cause: **Local Wi-Fi / Mesh Drop**
- **Gateway Not Answering**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🔴 **High loss / spike**
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🔴 Loss
  - DNS Query (`google.com`): 🔴 Fails
  - Diagnosed Root Cause: **the gateway itself** (or the link from the router to it), not the line
- **Line or First Provider Router**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🟢 < 2ms
  - ISP hop 1 (the provider's first router): 🔴 **Packet loss / Timeout**
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🔴 **Packet loss / Timeout**
  - DNS Query (`google.com`): 🔴 Fails
  - Diagnosed Root Cause: **the line or the provider's first router** (for a cable modem: the coax line or the CMTS)
- **DNS Resolver Outage**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🟢 < 2ms
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🟢 < 20ms
  - DNS Query (`google.com`): 🔴 **Timeout (above the Slow DNS level, 300 ms by default)**
  - Diagnosed Root Cause: **DNS Resolver Outage**

## How to read an incident

The status pill says what the classifier decided, in a word: **Healthy**, **Degraded**, **Down** or **Busy** (a line
load explains the delay, so it is expected and no incident opens; see Load-aware classification below). Loss from the
degraded level (15 % by default) or the down level (40 %) is what moves a stage there (The levels, below). An incident
has a **cause**, worded as a hypothesis, and a stable machine-readable **kind**; both place it in one category (router,
gateway, line, ISP, internet, DNS, load, service), the groups the incident timeline, the reliability report and the
dossier use. Open an incident in the list to get its inspector: the probes around it, stage by stage. The scenarios
and the placement rules follow; the panels are described in [dashboard.md](dashboard.md).

## Where a fault is placed

The gateway hop (shown as **Gateway**) is the gateway or modem answering on its home-side (LAN) address. Those pings
reach it through the router and never use the provider's line, so:

- **Loss starting at the gateway** while the router answers is the gateway itself (restarting, overloaded, its
  firmware or power) or the link from the router to it. Cause code `gateway_unreachable` (kinds `gateway_down`,
  `gateway_loss`), cause category `gateway`. If the provider supplied the gateway it is still the provider's equipment,
  but an equipment fault, not a line or signal one; a line fault shows here only when it made the gateway restart.
- **Loss starting at the provider's first hop** (ISP hop 1) while the gateway answers is the access line (cable, fiber
  or DSL) or the provider's first router. Cause code `access_line_drop` (kinds `isp_outage`, `isp_loss`), category
  `line`. The description words the line for its technology (`pengumon.lines`: coax and the CMTS for a DOCSIS modem).
  Line errors at the same time (bursts of uncorrectable codewords, signal out of range) point to the line itself: the
  incident inspector and the AI pack say so, the dossier asks for a line check.
- **Loss only past the first hop** (it answers, the internet does not) is the provider's network beyond it: cause code
  `internet_backbone_transit_loss` (an `isp_outage` worded as peering or backbone, and `backbone_loss`), category `isp`.
  Latency across the upstream routes (`upstream_latency`) keeps `access_line_drop`, and its category is `isp`.
- **Without a gateway target** nothing shows the gateway answering, so loss at the ISP hops names the gateway among the
  possible causes. **Without a provider hop** an internet outage past the gateway is `access_line_drop`, worded "Line or
  Provider Outage": the line cannot be told apart from the provider's network.

Loss at one ISP hop that does not carry on (below) and the load rules are unchanged by this placement.

The categories (`router`, `gateway`, `line`, `isp`, `internet`, `dns`, `load`, `service`, `unknown`) are what the
incident timeline's lanes, the reliability report's downtime by cause, the dossier and the AI pack group by. They come
from the incident's `kind` where it says enough, else from its cause code, so stored incidents keep their codes and are
placed again when read: an older gateway incident (`double_nat_congestion`) is in the `gateway` lane, and an older
`isp_outage` recorded as `access_line_drop` whose description says the public internet timed out while the ISP hop
answered is read as `isp`.
## The levels (Settings, Outage rules)

Each level below is a setting (`classifier.*`); the defaults are what the classifier always used. A change applies
from the next probe round and never relabels stored incidents.

- **The window** ("Judge each hop by its last", `window_probes`, 10 probes; 3 to 60, shown in seconds of probe
  rounds): every level below is judged over each target's last N probes. **Loss** is the share of them lost.
  **Latency** is the median of the role's answered probes in the window (of an even count the lower middle value),
  and **jitter** the median change from one answered probe to the next, per target, averaged over the role's targets.
  So a role counts as slow only when more than half of its probes are slow, and as jittery only when more than half of
  the changes are large: one stalled probe (a round in which everything past the modem answers after about a second)
  is no spike and opens no incident. With the defaults, latency must be high in 6 of the last 10 probes for 10 rounds
  in a row (about 15 s of trouble) before a latency incident opens. Earlier versions averaged latency and jitter, and
  one such probe lifted the averages over the levels for exactly the 10 rounds it stayed in the window: a 9-second
  "ISP Latency Spikes / Jitter" incident.
- **Loss of a role** (the window's probes of its targets): degraded from `degraded_loss_pct` (15 %), down from
  `down_loss_pct` (40 %). DNS lookups are degraded from `dns_degraded_loss_pct` (25 %) and down from the same
  down level.
- **DNS cooldown:** after trouble upstream of the home network (loss at the gateway, ISP hops or public targets,
  or slow public targets), DNS faults are not judged for `dns_cooldown_seconds` (90 s), so a line blip is not
  reported as a DNS outage.
- **Latency and jitter** per role: `local_*`, `gateway_*` and `public_*_spike_threshold_ms`. The ISP hops have
  `isp_latency_spike_threshold_ms`; empty, they use the internet's level (80 ms).
- **When incidents start and end** (rounds of 1 s): a loss incident opens after
  `consecutive_failures_for_incident` faulty rounds (3), a latency-only one after
  `consecutive_failures_for_latency_incident` (10), and either ends after `consecutive_successes_for_recovery`
  healthy rounds in a row (3). The end time is the last faulty round.
- **Internal, not settings:** DNS is judged only while every lower layer loses under 10 %; an ISP outage names the
  ISP hop from 30 % ISP-hop loss, otherwise peering; a sample over 150 ms makes its target an affected one.

A hop's own "warn above" level in the path editor (`warn_latency_ms`) changes only that hop's colour on the
dashboard; the incident rules above still judge its role's median.

## Load-aware classification

A line running near its capacity queues packets in the modem and router (bufferbloat), so ping times rise and
intermediate ISP routers drop ICMP replies first. That is what a busy line does, not a fault, so the classifier
looks at the WAN load (from the Asus router's 10 s byte counters) when it judges the probes:

* **Capacity** is `line.down_mbps` / `line.up_mbps` when set, otherwise learned: the 99.9th percentile of the
  router rates of the last 7 days (refreshed hourly, floor 10 Mbps, needs about 6 hours of history). Load is
  the peak of `max(rx/down, tx/up)` over the router samples of the last 20 s. Router data older than 60 s
  means the load is unknown and classification works as before.
* At or above `classifier.busy_load_pct` (default 60%), **latency or jitter alone** gives status `busy`
  (cause `load_induced_latency`, severity `info`) and no incident; the description names the load, e.g.
  "download 954 Mbps, 95% of the learned 1000 Mbps capacity".
* **Loss seen only at the ISP-category hops**, while the local router, gateway and public targets are fine,
  is `busy` too under that load.
* **Any other loss under load** is still an incident; it stores `load_pct` (the peak during the incident) and
  its description ends with "During heavy load (X%)".

## Intermediate-hop ping loss (the classic MTR rule)

Routers answer ping from a slow path and drop those replies first, whether or not the line is busy. Loss at
the `isp`-category hops (intermediate routers such as the second and third provider hops, and the ISP's DNS address)
that does not carry through to the end targets is therefore not packet loss. When the ISP hops lose the degraded
level (15% by default) or more, but the public targets lose under it **and** under half of what the ISP hops lose,
and the local
router, gateway and DNS probes are healthy, the status is `healthy` ("<hop name> drops some ping replies, but later
hops and public targets are fine: routers often deprioritise ping replies, so this is not packet loss"), no
incident is opened and no failure counter builds up. The busy rule above runs first and keeps its behaviour for
the loaded case.

On the first start of 0.2.0 a one-time backfill (recorded in `meta` as
`incident_ping_deprioritisation_backfill`) re-checks old "ISP Line / ... Packet Loss|Outage" incidents that
still have raw samples (7 days): if the public targets stayed under the degraded level and under half the ISP
loss, the incident is marked expected. Incidents without raw samples are left alone. A second, separate backfill
(`incident_load_backfill`) annotates old incidents with the router load around them and marks latency-only and
ISP-hop-loss incidents under heavy load as expected. It runs once: a later capacity or busy level does not
re-annotate them.

**Expected incidents** are hidden on the dashboard by default (a toggle shows them with a "busy line" badge)
and `GET /api/incidents?include_expected=false` omits them. The dossier, the reliability report and the
notifications exclude them and say how many were left out.

Diagnosis descriptions state the measurement first and phrase causes as hypotheses ("Possible causes: ...");
incident titles and cause codes are stable.

Every incident also carries a machine-readable `kind` (`local_down`, `local_loss`, `local_latency`,
`gateway_down`, `gateway_loss`, `gateway_latency`, `isp_outage`, `isp_loss`, `backbone_loss`,
`upstream_latency`, `dns_outage`, `dns_loss`, `dns_slow`, and `internet_outage` / `internet_loss` when internet
targets fail while no local router or gateway target is configured, so the fault cannot be placed on the ISP line;
incidents recorded before it existed get
`legacy_latency` or `legacy_other` where their title matches no current template). It is part of the incident
JSON in `/api/incidents` and the websocket. The intermediate-hop rule and the two backfills decide by `kind`, not
by the display title, so titles can be reworded without changing behaviour.

**Loss span.** An incident's `start_time` and `end_time` are detection times: it opens a few probe rounds after the first
lost probe and closes once enough rounds are clean again, so it runs a few seconds late on both ends (7 s of lost probes
are typically recorded as a 12 s incident). When an incident closes, the probes of its affected targets from 10 s before
its start to 2 s after its end give `loss_start` (the first lost probe) and `loss_seconds` (first to last lost probe plus
one probe interval; a target that answered nothing in that window, such as a hop that ignores ping, is left out;
intermittent loss counts from its first lost probe to its last). `loss_seconds` is 0 when nothing was lost (an incident
raised on latency) and null when no such probe is stored. Both are in `/api/incidents`; the incident list shows the loss
duration with the recorded one on hover, the inspector shows both ("Loss 7s · incident 12s"). Every start fills the two
columns for resolved incidents whose raw samples are still kept (`incidents/loss.py`, `db/incidents.py`).

Faults on the line to the ISP have the cause code `access_line_drop` (any line technology; the description names
technology-specific causes, e.g. coax and CMTS for a DOCSIS modem). Loss at the gateway has `gateway_unreachable`
(older builds raised `double_nat_congestion` for it, which now stands for delay at the gateway only); the stored
`xfinity_last_mile_coax_drop` incidents were renamed to `access_line_drop`, and the old name is still read as the new
one. Which schema versions an older build cannot read, and why a backup matters before an upgrade, is in
[storage.md](storage.md#schema-versions).

The per-target status colours on the dashboard (up / degraded / down) come from `prober.hop_status`; the defaults
are the values the dashboard has always used.
