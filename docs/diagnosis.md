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
- **Double NAT / Link Fault**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🔴 **High loss / spike**
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🔴 Loss
  - DNS Query (`google.com`): 🔴 Fails
  - Diagnosed Root Cause: **Gateway Link Congestion**
- **Coax Line Drop**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🟢 < 2ms
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🔴 **Packet loss / Timeout**
  - DNS Query (`google.com`): 🔴 Fails
  - Diagnosed Root Cause: **ISP Last-Mile Drop (DOCSIS)**
- **DNS Resolver Outage**
  - Local Mesh (`192.168.1.1`): 🟢 < 2ms
  - Gateway (`192.168.100.1`): 🟢 < 2ms
  - Public Anycast (`1.1.1.1` / `8.8.8.8`): 🟢 < 20ms
  - DNS Query (`google.com`): 🔴 **Timeout (>400ms)**
  - Diagnosed Root Cause: **DNS Resolver Outage**

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
the `isp`-category hops (intermediate routers such as Hop 3 and Hop 4, and the ISP's DNS address)
that does not carry through to the end targets is therefore not packet loss. When the ISP hops lose 15% or
more, but the public targets lose under 15% **and** under half of what the ISP hops lose, and the local
router, gateway and DNS probes are healthy, the status is `healthy` ("Hop 3 drops some ping replies, but later
hops and public targets are fine: routers often deprioritise ping replies, so this is not packet loss"), no
incident is opened and no failure counter builds up. The busy rule above runs first and keeps its behaviour for
the loaded case.

On the first start of 0.2.0 a one-time backfill (recorded in `meta` as
`incident_ping_deprioritisation_backfill`) re-checks old "ISP Line / ... Packet Loss|Outage" incidents that
still have raw samples (7 days): if the public targets stayed under 15% and under half the ISP loss, the
incident is marked expected. Incidents without raw samples are left alone. A second, separate backfill
(`incident_load_backfill`) annotates old incidents with the router load around them and marks latency-only and
ISP-hop-loss incidents under heavy load as expected.

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
by the display title, so titles can be reworded without changing behaviour. Schema version 6 adds the column and
labels the existing incidents from their titles once; back up the database before the first start of a build
with v6, and do not run an older build against a v6 database (its new incidents would have no `kind`).

**Loss span.** An incident's `start_time` and `end_time` are detection times: it opens a few probe rounds after the first
lost probe and closes once enough rounds are clean again, so it runs a few seconds late on both ends (7 s of lost probes
are typically recorded as a 12 s incident). When an incident closes, the probes of its affected targets from 10 s before
its start to 2 s after its end give `loss_start` (the first lost probe) and `loss_seconds` (first to last lost probe plus
one probe interval; a target that answered nothing in that window, such as a hop that ignores ping, is left out;
intermittent loss counts from its first lost probe to its last). `loss_seconds` is 0 when nothing was lost (an incident
raised on latency) and null when no such probe is stored. Both are in `/api/incidents`; the incident list shows the loss
duration with the recorded one on hover, the inspector shows both ("Loss 7s · incident 12s"). Schema v12 adds the two
columns; every start fills them for resolved incidents whose raw samples are still kept (`incidents/loss.py`,
`db/incidents.py`).

Faults on the line to the ISP have the cause code `access_line_drop` (any line technology; the description names
technology-specific causes, e.g. coax and CMTS for a DOCSIS modem). Schema v8 renames the stored
`xfinity_last_mile_coax_drop` incidents to it, and the old name is still read as the new one. An older build cannot
read the new code, so going back to one after v8 needs the pre-deploy database backup that `scripts/deploy.sh`
makes. Schema v7 adds a nullable `error_kind` to the modem and router snapshots (older builds work against it).

The per-target status colours on the dashboard (up / degraded / down) come from `prober.hop_status`; the defaults
are the values the dashboard has always used.
