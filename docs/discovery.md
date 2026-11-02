# Network discovery

Discovery finds the path from this host to the internet and proposes it as the probe path: each hop with a role
(Your network, Line / gateway, Provider, Internet), how sure it is (**Sure**, **Likely** or **Unsure**, in the API
`high`, `likely`, `unknown`) and the reasons for it. It only proposes. The path editor shows the draft, and nothing
changes until you press Apply there. It runs

- **by itself on the first run**, when no path has ever been applied (once per server process: restarting Pengumon before you Apply runs it again): with the operator lookup off, so nothing about
  your path leaves your computer (see below), and the result is offered as "Pengumon found your network path · Review";
- **when you ask**, with **Rediscover** in the path editor, where the operator lookup is a box that is ticked by default.

It makes no assumption about your equipment. An ISP's all-in-one router is one home hop, a router and a modem are
two, and a mesh satellite adds one more. Carrier-grade NAT may or may not be there.

## At a glance: what leaves your computer

| | First run (automatic) | Rediscover |
|---|---|---|
| Echo requests with a short lifetime to 1.1.1.1, 8.8.8.8, 9.9.9.9 (and one IPv6 server), which is what any traceroute does | yes | yes |
| Your resolver: reverse-DNS names of the hop addresses | yes | yes |
| Team Cymru, through your resolver: the **public** hop addresses, to learn who operates them | **no** | yes, unless you untick "Look up who operates each hop" |
| One web page request to each device in your home (and UPnP on your LAN) | yes, home only | yes, home only |
| SNMP to your devices | no | only if you tick it |

Nothing is sent to Pengumon's authors or any other service, and nothing found is written to the database until you press
Apply.

## What it does, and what is sent where

| Step | Sent to | What |
|---|---|---|
| Traces | the routers on the path | Echo requests with TTL 1 to 20 towards 1.1.1.1, 8.8.8.8 and 9.9.9.9, one trace after the other: the same in-process traceroute as the trace dialog (`engine/mtr.py`), three probes per TTL. About 1.5 s each. When this host has an IPv6 route to the internet (the same check as the prober's, `netinfo.ipv6_route_check`), one more trace, over IPv6, to 2606:4700:4700::1111. |
| Ping | live home hops off the path | One echo request to each live home hop (local, gateway; at most eight) that the traces did not see. |
| Reverse DNS | your resolver | One PTR query per hop address (as `/etc/resolv.conf` or the OS settings say). |
| **ASN lookup** ("who operates each hop", ticked by default) | Team Cymru, through your resolver | For each **public** hop address, a TXT query for `<address reversed>.origin.asn.cymru.com`, then one `AS<n>.asn.cymru.com` query per AS found. So Team Cymru (and your resolver) learn the public addresses on your path, which is roughly what your provider and the networks it uses are. Never a private, CGNAT, link-local or special address, and never this host's own public address: the external address the router reports over UPnP, and any hop equal to it, are skipped. (If no gateway reports one, a router answering the trace from its WAN address cannot be told from a provider router, so treat the public router addresses of the path as what is sent.) Untick the box to skip it; the roles then come from names and the paths only. |
| **Device checks** (on by default) | devices in your home | For each private hop before the first public one (at most seven) and for 192.168.100.1 (where cable modems serve their status page): one GET of `/` over HTTP, or HTTPS when nothing listens on port 80. A redirect is followed only to the same address (at most twice); a redirect to another host is never followed. No credentials, no cookies kept, at most 64 KiB read, 2 s timeouts, certificates not checked (home devices have self-signed ones and nothing is sent that one would protect). Only the page title, the `Server` header and the driver fingerprint below are kept. |
| UPnP (part of the device checks) | your LAN (multicast) | One SSDP M-SEARCH for an Internet Gateway Device (`239.255.255.250:1900`, TTL 2). Only gateways that are hops of the traced path are asked further: for each one that answers from a private address with its description on that same address: one GET of the description (at most 64 KiB), then only the read-only SOAP actions `GetExternalIPAddress`, `GetStatusInfo` and `GetCommonLinkProperties` of the WAN services it lists, on that same address. Your external address is used to tell where your network ends and is never shown or logged. |
| **SNMP** (off by default) | the private hops | One SNMPv2c GET of `sysDescr` with the community `public`, through the snmp driver's client. Tick it in the review to use it. |

The results are kept in memory for ten minutes (at most eight runs) and are never written to the database. Starting
a run is rate limited (6 a minute) and single flight: a second start while one runs returns the running one.

## How it decides

Each hop gets evidence, each piece a plain-language reason (`evidence[].text`) of one kind:

| Kind | Meaning |
|---|---|
| `address_class` | Private (RFC 1918, IPv6 unique local), carrier-grade NAT (100.64.0.0/10), link-local, special or public; the first public address on the path; another private network than the hop before (double NAT). |
| `latency_step` | The largest significant jump in round-trip time between consecutive hops, from the home up to the first public hop (at least 1 ms, and the later hop at least 1.5 times slower). The access line is usually there. |
| `rdns` | The hop's reverse-DNS name, and whether its domain is the provider's. |
| `asn` | The hop's AS. The first public hop's AS is the provider's; later hops in another AS are outside it. |
| `divergence` | Whether the hop is on the way to all three servers (your connection's own way out) or past the point where their paths part. |
| `device_page` | What the hop's web page (or SNMP agent) says it is. |
| `upnp` | The hop announced itself as an internet gateway, and what its internet address says. |
| `neighbours` | A hop with no evidence of its own takes the owner of the hops either side when they agree; otherwise it stays unknown. |

The rules, in order:

1. **Where your network ends.** The leading run of private hops is home. If the largest latency step lies inside
   that run, the hops past it are behind the access line (a satellite ground station, a cellular core) unless one of
   them serves a web page or answers SNMP. A UPnP gateway on the path decides last: with a public internet address it
   is the edge; with a private one, another NAT follows and the next private hop is home too; with a CGNAT one, the
   provider's NAT comes next.
2. **Provider or internet**, for each hop past the edge, by its strongest evidence: its AS, else its reverse-DNS domain
   (the provider's domain is the first one past the edge, preferring a hop in the provider's AS), else the divergence
   point, else its address (CGNAT or private past the edge, or the first public hop, are the provider's). Once the path
   has left the provider it does not come back.
3. **Neighbours** fill in the hops with no evidence.

Confidence is **high** when two kinds of evidence agree and none disagrees, **likely** with one kind (or when weaker
evidence disagrees), **unknown** with none.

### Reading the badges and reasons

In the editor the badge on each card is that confidence: **Sure** (green) means two independent kinds of evidence agree,
for example "its address is private" and "its page is a router's login"; **Likely** means one kind, so check it; **Unsure**
(amber) means no evidence for it (a hop with nothing to go on takes the role of its neighbours when they agree). The
list under the name gives the reasons in words, one per piece of evidence: those are what the table above lists. A hop
marked **New**, **Changed** or **Not found by discovery** says how it differs from the path that is live. When two hops
are wrongly placed, change the role or the order yourself: discovery is a good first guess, not an authority. The automatic first run has no operator lookup, so more provider and internet hops read **Likely** than after a
**Rediscover** with it on; press Rediscover once if you are happy to send the public hop addresses to Team Cymru. Discovery
cannot see a router that does not answer the trace; add such a hop by its address.

Roles: the first home hop is "Your network" (`local`, named Router). A later home hop at the edge is "Line / gateway"
(`gateway`) when it is on another private network than the hop before it, or its page is an ISP gateway's. A cable
modem that answers at 192.168.100.1 but is not on the traced path (a modem in bridge mode is invisible to a trace) is
proposed as a `gateway` hop at the edge, named Cable modem. Provider hops are `isp` ("Provider hop 2 (example.net)"),
internet hops `public`, and the three servers are proposed as one node, group `internet`. The IPv4 path is the main chain. When this host has IPv6, the same three resolvers over IPv6 (2606:4700:4700::1111, 2001:4860:4860::8888, 2620:fe::fe; `family: 6`, ids `cloudflare_dns_v6`, `google_dns_v6`, `quad9_dns_v6`) are proposed as a second node, group `internet_v6` (“Internet (IPv6)”), with the evidence “this host has IPv6” and whether the IPv6 trace reached Cloudflare. The IPv6 path's own hops are not added. Without IPv6 there is no such group, and a note says why.

A hop that did not answer the trace has no address, so it cannot be probed and is not in the draft (the notes say how
many).

### Device fingerprints (`suggested_driver`)

Only what this project's drivers know of each device's web interface:

| Driver | Recognised by |
|---|---|
| `xfinity_xb` | A login form that posts to `check.jst`, or the logged-out markers `home_loggedout` / `Gateway > Login` (the driver's own login and session checks). |
| `asuswrt` | `Main_Login.asp`, the firmware's login page, which the front page sends the browser to (by script or redirect); or the `login_authorization` field of the login form, which the asuswrt driver posts to `login.cgi`. |
| `openwrt` | LuCI: `cgi-bin/luci` in the page or its redirect, or "LuCI" in the title. |
| `snmp` | An SNMP agent answered with the community `public` (only when SNMP is ticked) and no page was recognised. |

A page that matches none is still evidence that the hop is a device in your home.

### Reverse-DNS domains

The domain of a name is a small heuristic, not the Public Suffix List: the last two labels, or three when the
second-to-last is a common second-level label under a two-letter country code (`example.co.uk`, `example.com.au`,
`example.ne.jp`). Private registries (`*.github.io`) and deeper suffixes come out wrong; routers rarely have such
names, and a wrong guess only costs a "different domain" reason.

## The draft, and rediscover

Each draft hop has the fields of the path API (`id`, `name`, `address`, `category`, `enabled`, `position`, `group`,
`ignores_ping`, `color`, `device_id`) plus `evidence`, `confidence`, `suggested_driver` and `status`:

- Ids are stable for an address: `hop_203_0_113_9`, and the dashboard's default ids for the three servers
  (`cloudflare_dns`, `google_dns`, `quad9_dns`). A live hop at the same address keeps its id, so its history stays.
- `same`: a live hop at that address, with the same role and place. `changed`: the role or the place differs. The
  draft keeps what you set on it (name, colour, device, "ignores ping", enabled, group) and proposes only the role
  and the place.
- `new`: not in the live path. `gone`: a live path hop (local, gateway, isp or public) that discovery did not see; it
  is listed after the hop it followed. A live home hop (local, gateway) that the trace did not see gets one echo
  request first: while it answers, it stays (`same`), since a device in the home can be off the traced path (a cable
  modem in bridge mode). Live DNS and custom targets are not on the traced path; they are carried as
  they are (`same`), after the path.
- At most 64 hops; on a longer path the internet hops nearest the servers are left out.

## API

`POST /api/discovery` with `{"asn_lookup": true, "snmp": false, "device_checks": true}` (all optional; these are the
defaults) answers `{"id": "..."}`. `GET /api/discovery/{id}` answers
`{"id", "state": "running"|"done"|"failed", "progress": "...", "draft": {"hops": [...]}, "notes": [...], "options"}`;
the draft is empty until the run is done. 404 for an unknown id or one finished more than ten minutes ago.
