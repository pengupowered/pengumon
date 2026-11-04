# The path editor

The probe path is the list of addresses Pengumon pings every second, in the order a packet meets them: your router, the
modem, your provider's routers, the internet. It is stored in the database and edited in the dashboard, not in
`config.yaml`. Open it with **Edit** in the header of **Path health** (or with **Review** on the first-run banner or the
"Your path changed" notice).

Nothing you do in the editor changes anything until you press **Apply**. **Cancel** throws the draft away.

## The chain

At the top, the draft is drawn the way Path health draws it: this host, then one node per hop, nearest first, each
with what Pengumon measures there now. Click a node to jump to its card below.

Two neighbouring hops of the same role can be shown as one node on Path health (the three public DNS servers are, by
default): press **Merge** on the link between them. A merged node has a **Split** button between its hops to undo it.

## The hops

Each hop is a card, nearest first.

**The top line** is the name and what the prober measures now, from the same live data as Path health, updated while
the editor is open:

- "11.2 ms · 0% loss (last minute)": the median round trip and the loss over the last 60 probes, coloured like Path
  health (grey, amber, red).
- "Not answering": no reply, and nothing after it answers either. That is a real outage (red).
- "No reply · ignores ping (not counted as an outage)": no reply, but the hops after it answer, so traffic passes
  through it. Many provider routers do this. Pengumon works it out by itself ([diagnosis.md](diagnosis.md)); there is
  nothing to set.
- "IPv6 unavailable on this host": an IPv6 hop while this computer has no IPv6 (grey, not an error).
- "Not probed yet · Test": a hop you added, or one whose address you changed. **Test** sends one probe now;
  **Preview** (next to Apply) probes every such hop once a second for 30 seconds, without recording anything, and
  shows the median and loss here before you apply.

**The second line** is the address, **Change address**, the role in plain words, and badges (New, Changed, Sure,
Likely, IPv6, ...).

**The controls**:

- **Name**: type over it. Path health shows a short label (Router, Line, ISP hop 2, Internet) with this name under it.
  If names start with "Hop 1:", "Hop 2:" (older configs numbered them), the editor offers **Drop the prefixes**; the
  new names are kept when you apply.
- **Role**: Your network, Line / gateway, Provider, Internet, DNS server or Other. A line under the menu says what the
  role changes: where an outage is placed ([diagnosis.md](diagnosis.md)) and what the provider dossier says. *Other* is
  for anything that is not a stage of the path (a NAS, a VPN endpoint, a server): it is probed and charted but never
  blamed for an outage.
- **Probe the name's IPv4 / IPv6 address**: only for a host name. An IP address is probed as it is.
- **More > Warn above [ ] ms**: an optional latency level for this hop alone, for a far-away server that is always slow.
  Empty by default (the placeholder shows the role's level, "role level: 60 ms", read from Settings). The hop's colour
  turns amber above it; incidents still use the role's level, so this changes nothing but that hop's colour.
- Up and down arrows move the hop; **Remove** takes it out.

**Change address** replaces the address of a hop in place, for a router your provider renumbered. The hop keeps its
id, so its history stays with it; its live figures start over at the new address. The hop's history (click it on Path
health or in Hop details) marks the moment: "Address changed: old → new" on its charts and its 24-hour strip.

**The device** (only on hops whose role is Your network or Line / gateway) names the configured device this hop is,
with its own current status as its card shows it: "Readings from: ASUS router · RT-AX88U · Healthy · CPU 12% · 23
clients", or for a modem "Xfinity gateway · modem · Line up · SNR 35.9 dB · 32 channels". Pengumon reads that device
directly (devices are set up on the Settings page, see [devices.md](devices.md)); its details are in the Router or Line
panel, which the link under the device line opens (it closes the editor, and asks first if you changed something).
The hop's own figures are still its ping. Path health's Router and Line nodes name the device in their tooltip.

**Change** picks another device or *No device*; **Connect** links the device discovery suggests, or the only one
configured, and opens its sign-in; with several devices it asks which. A hop is linked by itself when exactly one
configured device is at its address (a host name in the device's URL is looked up first). That happens on the first
import of the path, once for a path stored by version 0.19, when you apply a new hop, and when the editor opens on a
hop without a device ("linked by its address" on the card: Apply keeps it). A hop you set to *No device* is remembered
as such and never linked by itself again.

**Add a hop** takes an IPv4 or IPv6 address or a host name, a role and a place in the path. At most 64 hops.

A removed hop is listed under "Removed when you apply" with an **Undo**. Removing stops the probing; the hop's history
is kept, hidden from every chart and report, and comes back if you add the same hop again. Tick "Delete its history too"
to delete it on Apply instead (that cannot be undone).

## Apply

**Apply** stores the whole path in one step and makes it live without a restart; the page then reloads so every panel
draws the new path. If the path was changed elsewhere (another tab or device) after you opened the editor, Apply is
refused and nothing changes; **Load the current path** starts over from the current one.

When an access token is set and viewing without it is allowed, a visitor who has not signed in sees the editor as
"View only": Apply, Preview, Rediscover and Restore stay off until they sign in ([security.md](security.md)).

## Earlier paths

Every Apply is kept as a version. **Earlier paths**, at the bottom of the editor, lists them (newest first: when, how
many hops, what changed). **Restore the previous path**, or **Load** on any of them, puts that path in the editor as a
draft; Apply restores it, and a hop that comes back keeps its history.

## Rediscover, and "Your path changed"

**Rediscover** traces the path again and puts the result in the editor as a draft, next to what is live: each hop is
*New*, *Changed* or *Not found by discovery*, and has a badge (**Sure**, **Likely** or **Unsure**) and the reasons for
it. Your names, device links and groups are kept; discovery only proposes roles and places. What is sent where, and how
a role is judged, is in [discovery.md](discovery.md). Two boxes control it: "Look up who operates each hop" (on by
default; sends the public hop addresses to Team Cymru's DNS lookup) and "Ask devices over SNMP" (off).

Pengumon also checks the route by itself now and then (daily, and after an incident ends). When it finds a change,
Path health shows "Your path changed" with what changed; **Review** opens that draft in the editor and **Dismiss**
hides the notice until the route changes again.

> [!WARNING]
> Without an access token, any device that can reach the dashboard can edit the path and delete a removed hop's history,
> including hops that came from `config.yaml`. Set `PENGUMON_ACCESS_TOKEN` if other people's devices share your network
> ([security.md](security.md)).

## Export and import

**Export** shows the applied path as YAML in the editor, with **Copy** and a **Download file** link (`pengumon-path.yaml`; Chrome blocks a download from a page served over plain HTTP on another machine, Copy works everywhere). It is a `targets:` list in the syntax `config.yaml` used to
take. **Import** reads such a file (or a whole `config.yaml`) into the editor as a draft to review and Apply. It is for
moving a path between installs or keeping a copy. Files are limited to 256 KB and a limited nesting depth, and YAML
anchors, aliases and merge keys are refused. A device that the other install has and this one does not is dropped
from the hops. An `ignores_ping` key from an older export is read and ignored: whether a hop ignores ping is observed,
not set.

```yaml
targets:
  - { id: router, name: "Home router",  category: local,   address: "192.168.1.1" }
  - { id: modem,  name: "Cable modem",  category: gateway, address: "192.168.100.1" }
  - { id: isp1,   name: "First provider hop", category: isp, address: "198.51.100.1" }
  - { id: cf,     name: "Cloudflare",   category: public,  address: "1.1.1.1" }
```

`category` is `local`, `gateway`, `isp`, `public`, `dns` or `custom` (the roles above, in that order).

## First run

On an install that has never stored a path, the default targets (the default gateway and two public resolvers) are
probed, discovery starts by itself (without the operator lookup, so nothing about your path leaves your computer), and
Path health shows "Pengumon found your network path · Review". **Review** opens the editor with the draft. Until you
Apply, the defaults keep being probed.

## Upgrading

From 0.17 or older: on the first start of a newer version, the `targets:` list in `config.yaml` and the targets you
added from the dashboard are imported into the database once, with their ids, so all history stays attached (more than
64 targets: the extras come in as removed hops that you can restore in the editor). After that the `targets:` list in
`config.yaml` is ignored (the startup log says so); edit the path here. Every other setting is on the Settings page.

From 0.19: the "Ignores ping" setting is gone. Any value stored with a hop is dropped at the first start (the startup
log says so), and the hops of your network and the line are linked to their devices by address once.

The API is in [api.md](api.md) (`/api/path`).
