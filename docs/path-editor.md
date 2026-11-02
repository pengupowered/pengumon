# The path editor

The probe path is the list of addresses Pengumon pings every second, in the order a packet meets them: your router, the
modem, your provider's routers, the internet. It is stored in the database and edited in the dashboard, not in
`config.yaml`. Open it with **Edit** in the header of **Path health** (or with **Review** on the first-run banner).

Nothing you do in the editor changes anything until you press **Apply**. **Cancel** throws the draft away.

## The hops

Each hop is a card, nearest first, with:

- **Name**: type over it. Path health shows a short label (Router, Line, ISP hop 2, Internet) with this name under it.
- **Role**: Your network, Line / gateway, Provider, Internet, DNS server or Other. The role decides where the hop sits
  on Path health and how an outage is placed ([diagnosis.md](diagnosis.md)). *Other* is for anything that is not a stage
  of the path (a NAS, a VPN endpoint, a server): it is probed and charted but never blamed for an outage.
- **Ignores ping**: many provider routers pass traffic but do not answer ping. *Decide automatically* (the default)
  treats a hop as up when a later hop answers; *Yes* and *No* force it.
- **Device**: the configured router or modem this hop is, so its card can show that device's readings. A hop that
  looks like a device Pengumon has a driver for says so, with a **Connect** button when the driver is set up in
  `config.yaml`, or what to add there when it is not ([devices.md](devices.md)).
- **One node with hop N**: shows two neighbouring hops of the same role as one node on Path health. Discovery uses this
  for the three public DNS servers.
- **Probe the name's IPv4 / IPv6 address**: only for a host name. An IP address is probed as it is.
- Up and down arrows move the hop; **Remove** takes it out; **Test** sends one probe now and shows the answer, or
  "IPv6 unavailable on this host" (grey, not an error) if this computer has no IPv6.

**Add a hop** takes an IPv4 or IPv6 address or a host name, a role and a place in the path. At most 64 hops.

A removed hop is listed under "Removed when you apply" with an **Undo**. Removing stops the probing; the hop's history
is kept, hidden from every chart and report, and comes back if you add the same hop again. Tick "Delete its history too"
to delete it on Apply instead (that cannot be undone).

## Apply

**Apply** stores the whole path in one step and makes it live without a restart; the page then reloads so every panel
draws the new path. If the path was changed elsewhere (another tab or device) after you opened the editor, Apply is
refused and nothing changes; **Load the current path** starts over from the current one.

## Rediscover

**Rediscover** traces the path again and puts the result in the editor as a draft, next to what is live: each hop is
*New*, *Changed* or *Not found by discovery*, and has a badge (**Sure**, **Likely** or **Unsure**) and the reasons for
it. Your names, device links, ignore-ping choices and groups are kept; discovery only proposes roles and places. What
is sent where, and how a role is judged, is in [discovery.md](discovery.md). Two boxes control it: "Look up who operates
each hop" (on by default; sends the public hop addresses to Team Cymru's DNS lookup) and "Ask devices over SNMP" (off).

> [!WARNING]
> Without an access token, any device that can reach the dashboard can edit the path and delete a removed hop's history,
> including hops that came from `config.yaml`. Set `PENGUMON_ACCESS_TOKEN` if other people's devices share your network
> ([security.md](security.md)).

## Export and import

**Export** downloads the applied path as `pengumon-path.yaml`, a `targets:` list in the syntax `config.yaml` used to
take. **Import** reads such a file (or a whole `config.yaml`) into the editor as a draft to review and Apply. It is for
moving a path between installs or keeping a copy. Files are limited to 256 KB and a limited nesting depth, and YAML anchors, aliases and merge keys are refused. A device that the other install has and this one does not is dropped
from the hops.

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

## Upgrading from 0.17 or older

On the first start of this version, the `targets:` list in `config.yaml` and the targets you added from the dashboard
are imported into the database once, with their ids, so all history stays attached (more than 64 targets: the extras come in as removed hops that you can restore in the editor). After that the `targets:` list in
`config.yaml` is ignored (the startup log says so); edit the path here. Every other section stays in `config.yaml`.
The API is in [api.md](api.md) (`/api/path`).
