# Devices (`devices`): modem, gateway, router

Add devices on the **Settings** page (Devices and services): a new device, its driver, address and options, then
**Set up** for its password. A save applies live ([below](#changing-devices-without-a-restart)). The YAML on this page
is the syntax that **Settings > Export** writes and **Import** reads; a `config.yaml` with a `devices:` list is read
only once, on the first start (see [configuration.md](configuration.md)).

Pengumon reads the devices in the `devices` list. Each entry names a **driver**, which decides what the device can
report (its *capabilities*: `access_line` for the line to the ISP such as DOCSIS levels and errors, `wan_counters`
for WAN throughput and line load, `router_system` for router CPU, memory and the client count, `client_inventory`
for the list of connected devices, `mesh` for the mesh nodes and how many clients each carries, `traffic_usage` for the
router's traffic per client, hour and app). Nothing else in the
code knows a vendor, an address or a device id.

```yaml
devices:
  - id: modem                    # 1-64 letters, digits, - or _; used in logs, the API and credential names
    driver: xfinity_xb           # Xfinity/Comcast XB6/XB7/XB8 gateways: access_line (DOCSIS)
    url: "http://192.168.100.1"
    poll_interval_seconds: 900   # optional; default per driver (xfinity_xb 1800, asuswrt, openwrt and snmp 10)
  - id: router
    driver: asuswrt              # AsusWRT routers (RT-AX, ...): wan_counters, router_system
    url: "http://192.168.1.1"
    model: "RT-AX88U"           # optional display name (the router API does not report it)
    username: admin              # optional, default "admin"
    enabled: true                # optional; a disabled device stays listed and shows as disabled
    tls:                         # optional, only used for https:// URLs (and redirects to one)
      verify: true               # default: certifi's CA bundle (or SSL_CERT_FILE / SSL_CERT_DIR); false: no check
      ca_file: /app/certs/router.pem  # optional: verify against this CA (or the device's own certificate)
    options:                     # driver-specific, checked when saved
      clientlist_interval_seconds: 60  # asuswrt: the client list (the heaviest answer) is read this often
      # mesh_node_labels:            # asuswrt: name mesh nodes by MAC (or its last six hex digits), see clients.md
      #   "02:00:00:00:11:22": "Hall"
      # traffic_usage: true            # asuswrt: read the Traffic Analyzer (bytes per client, hour and app)
      # traffic_interval_seconds: 3600 # asuswrt: at most this often (at least 60), see below
```

## Supported devices

| Driver | Devices | Status | What it reads |
|---|---|---|---|
| `xfinity_xb` | Xfinity / Comcast XB6, XB7, XB8 gateways | Tested by the maintainer | The cable line (`access_line`): DOCSIS downstream and upstream levels, SNR, codeword errors |
| `asuswrt` | AsusWRT routers (RT-AX and others), with AiMesh | Tested by the maintainer | WAN traffic, CPU, memory, connected clients by band, mesh nodes; uptime, WAN link speed, CPU temperature; traffic per client, hour and app (its Traffic Analyzer) |
| `openwrt` | OpenWrt routers (LuCI, or rpcd and uhttpd-mod-ubus) | Tested by the maintainer on OpenWrt 24.10 (x86-64 image); other releases untested | WAN traffic, CPU, memory, model, clients (DHCP leases, Wi-Fi stations by band) |
| `snmp` | Anything with an SNMPv2c or SNMPv3 agent: routers, switches, NAS boxes | Generic: a standard protocol, tested against net-snmp 5.9 | WAN (or uplink) traffic, CPU, memory, model; no client list |
| `upnp_igd` | Routers and gateways that answer UPnP's Internet Gateway Device profile | Generic: a standard profile, new in 0.24; some routers have UPnP switched off | WAN traffic (32-bit counters, wraps handled); no client list |

The `xfinity_xb` driver follows redirects (a gateway may send it from http to https), but only on the configured host: a redirect to another host, or from https down to plain http, fails the poll instead of sending the login there.

The status says who has seen the driver work. **Tested by the maintainer**: on the maintainer's own hardware.
**Tested by a contributor**: on a contributor's hardware (none listed yet). **Generic**: written from a standard, so it
should work with any device that follows it, but a particular device may differ; if you try one, tell us the model and
firmware in an [issue](https://github.com/pengupowered/pengumon/issues/new/choose) and it can be listed here.

Have a device that is not here? [Adding a device](adding-a-device.md) shows how to teach Pengumon to read it, and the
"Support for a new device" issue form is the place to start.

### Connection details

- `xfinity_xb`
  - Devices: Xfinity/Comcast XB6, XB7, XB8
  - Capabilities: `access_line` (DOCSIS)
  - URL: `http(s)://`
  - Username: `admin`
  - Password: the gateway's admin password
- `asuswrt`
  - Devices: AsusWRT routers (RT-AX and others)
  - Capabilities: `wan_counters`, `router_system`, `client_inventory`, `mesh`, `traffic_usage`
  - URL: `http(s)://`
  - Username: `admin`
  - Password: the router's admin password
- `openwrt`
  - Devices: OpenWrt routers
  - Capabilities: `wan_counters`, `router_system`, `client_inventory`
  - URL: `http(s)://`
  - Username: `root` (default)
  - Password: an rpcd login's password, see [OpenWrt](#openwrt)
- `snmp`
  - Devices: any SNMPv2c or SNMPv3 agent
  - Capabilities: `wan_counters`, `router_system`
  - URL: `snmp://host[:port]`
  - Username: not used for SNMPv2c; the SNMPv3 user name
  - Password: the community string (v2c) or the authentication password (v3), see [SNMP](#snmp)

The device's `model` from the config names it on the dashboard. A driver that learns the model from the device
(openwrt: the board name; snmp: the first line of `sysDescr`) stores it with each reading when the config sets none.

With `ca_file` the host name is still checked: the certificate's subjectAltName must cover the URL's host, which for
a router is usually an IP address (so the certificate needs an IP SAN entry). Python 3.13 and later also apply
`VERIFY_X509_STRICT`, which refuses some device-generated certificates (no SAN, a CA certificate without key-usage
extensions). A CA file that is missing or unreadable stops the start for an enabled `https://` device; for a disabled
or plain-HTTP one it is a warning. Certificate fingerprint pinning is not supported yet.

Unknown drivers, duplicate ids, unknown options, a URL with credentials in it or a password in the entry are refused
when you save, with a message naming the entry (in the one-time import of a `config.yaml`, the entry is left out and
the log says why). Configuration errors never quote the values (a password stays out of
the log). Several devices of one kind (two routers, two modems) may be enabled: each one's readings are stored under
its id. The single-device views (the dashboard's modem / line and router panels, the live message, the incident inspector, the
dossier and the line-load learning) use the first enabled one in list order; when every device of that kind is
switched off (`enabled: false`), the first one still shows its stored history and the line capacity is still learned
from it. The live line load needs an enabled device.

Without a `devices:` list there are no devices (the probes, incidents and reports work without them). The ids are
the keys of each device's stored history and credentials: the example `config.example.yaml` has `modem` and `router`
(commented out), the ids its history was stored under, so renaming a device starts its history afresh (the old rows are kept until
retention, and a startup warning names stored ids that are no longer configured).

**Credentials** never go in the settings or in `config.yaml`. For each device, the first of these that has a value wins (username and
password separately; `<ID>` is the id in upper case with `-` as `_`):

- Order 1: Password from environment `PENGUMON_DEVICE_<ID>_PASSWORD`. Username from environment `PENGUMON_DEVICE_<ID>_USERNAME`
- Order 2: Password from file `/run/secrets/pengumon_<id>_password` (a Podman/Docker secret: a regular file of at most 4 KiB; exactly one trailing `\n` or `\r\n` is removed). Username from `data/secrets.json` `devices.<id>.username`
- Order 3: Password from `data/secrets.json` `devices.<id>.password`. Username from the device's `username` (default `admin`)

An SNMPv3 device at security level `authPriv` also has a privacy password, from the same kinds of sources (in this
order): `PENGUMON_DEVICE_<ID>_PRIVACY_PASSWORD`, the file `/run/secrets/pengumon_<id>_privacy_password`, and
`data/secrets.json` `devices.<id>.privacy_password` (see [SNMPv3](#snmpv3)).

Everything is read once at startup, and the log names the source each device's password came from (never the
password). Credentials entered in the dashboard are written to `data/secrets.json` under `devices.<id>` (mode 0600).
A save that a higher-ranked source (the environment or a `/run/secrets` file) will override at the next start is
used until then; the answer names that source in `overridden_by` and the log warns. `docker-compose.yml` passes
`PENGUMON_DEVICE_MODEM_*` and `PENGUMON_DEVICE_ROUTER_*` from `.env` (empty when unset); for another device id, or a
`/run/secrets` file, add an `environment:` entry or a `secrets:` entry named `pengumon_<id>_password` there.

**During a speed test** the router whose WAN counters the line load reads (the first enabled device that reports
them) is also read about once a second for the WAN counters alone, while each direction of the test runs (about 8 s
each): `asuswrt` one `appGet.cgi` request with `netdev()`, `openwrt` one ubus batch (the interface dump and the device
statistics), `snmp` one GET of the uptime and the WAN octet counters, `upnp_igd` the two counter actions. These reads use
the same driver and session as the polls and never run during one (they wait for it); a refused login (a poll's or
theirs) stops them until a poll works again or the credentials change, so a wrong password costs no login attempt a
second. A device with a minimum interval between reads (the cable modem) is never read this way. Why:
[speedtest.md](speedtest.md#why-the-routers-view). While a test runs (and for 30 s after its last counter read) the
`asuswrt` driver reads none of its statistics pages (below).

## AsusWRT: traffic, uptime, WAN link and temperature

Besides the `appGet.cgi` hooks every poll sends, the `asuswrt` driver reads a few of the router's own status pages.
Every request is a read: the driver's `ALLOWED_REQUESTS` lists them (`login.cgi`, `appGet.cgi`, `update.cgi`,
`getWanTraffic.asp`, `getAppTraffic.asp`, `ajax_ethernet_ports.asp`, `ajax_coretmp.asp`), anything else is refused
before it is sent, and a test checks the list. Never `apply.cgi`, `applyapp.cgi`, `set_*.cgi`, `netool.cgi` or the
Traffic Analyzer's "clear the log". The pages need the session of the polls (the same login, User-Agent, and the
router's own `index.asp` as Referer). Answers are read only up to a cap, the rest never: `appGet.cgi` 4 MiB (the
client list is most of it, a few hundred bytes to about 2 KB per client), `update.cgi` and each page 1 MiB. A longer
`appGet.cgi` answer is that poll's problem (logged once), never parsed, and every answer is parsed in time linear in
its size.

- **With the client list** (every `clientlist_interval_seconds`, 60 s by default): the `uptime()` hook joins the poll's
  one `appGet.cgi` request, and `ajax_ethernet_ports.asp` (the WAN port's link speed: X down, M 100 Mb/s, G 1 Gb/s,
  Q 2.5 Gb/s, F 5 Gb/s, T 10 Gb/s) and `ajax_coretmp.asp` (`curr_cpuTemp`, °C) are read. They are the router card's
  uptime, WAN link and CPU temperature, stored with that poll (`device_samples`, schema v19) and in the history.
  A page the router does not have (404) is a quiet "not offered": no reading, one info line, asked again a day later.
  Radio temperatures are not read (unset on the routers checked).
- **The Traffic Analyzer** (`traffic_usage`, on by default; the router's "Traffic Analyzer > Statistic" must be on):
  bytes per client and clock hour, and per app over 24-hour windows, as the router counted them. The router publishes
  an hour a minute or so after it ends and never the hour in progress, so reading more often finds nothing new sooner.
  A reading round:
  1. the hourly series of every client together (`getWanTraffic.asp?client=all&mode=hour`, one request). If its newest
     hour is the one the last finished round saw, with the same figures, the round ends there;
  2. the 24-hour total of each client (`getAppTraffic.asp`) and the apps of every client together;
  3. for each client whose 24-hour total changed since the last round: its hourly series and its app list. A client
     whose total fell to 0 needs neither. A client whose total did not change keeps the app list last read for it, as
     this round's window (the hour that left the window moved what the hour that came in moved), at no request.

  So each round gives every client's apps over the 24 hours to the round's hour, and the round just after midnight
  (in the zone set in Settings) gives each one's whole day: Pengumon keeps that as the day's apps, which the Internet
  use card adds up over 7 and 30 days. A day whose midnight round did not run (Pengumon stopped, an interval of more
  than an hour that skips it) has no app figures.

  Rounds start `traffic_interval_seconds` apart (default 3600, at least 60); with an hour or more, at 5 minutes past
  the hour. The first round after a start reads the last 24 hours. A round makes at most 3 requests per 10 s poll
  (shared with the two status pages: no new one once 0.6 s went on them, and a page that takes over 2 s is given up),
  and none while a speed test runs. A transport error or a refused session stops the round (the next poll logs in
  again as usual) and the round is redone in full 5 minutes later. A router without the Traffic Analyzer pages (404), or with it switched off (no data), says
  so once in the device's log and in `GET /api/devices/{id}/traffic` (`available: false`, `reason`); the pages are
  asked for again a day later, or at the next round. Bad figures (negative, not a number, more than 10 TB an hour)
  are skipped, never stored. Each page is read up to 1 MiB (the rest never is): a longer one is no data, said once like
  the others; at most 48 hourly slots, 1024 clients and 1024 apps of a page are used, so a round always ends.

  Stored per client (by MAC) and hour, and the app windows (the latest per client, and the first after each local
  midnight, so app history exists by day): [storage.md](storage.md#router-readings-device-events-and-traffic-schema-v19).
  Kept for `database.traffic_retention_days` (400 days by default).

**Request budget**, measured with the test fixtures (8 clients, 7 with traffic; `tests/test_router_traffic.py`
`test_the_request_budget_per_hour`), per hour of steady polling at the default 10 s poll and 60 s client list:

| | `appGet.cgi` | status pages | Traffic Analyzer | in all |
|---|---|---|---|---|
| Before 0.27 | 360 | 0 | 0 | 360 |
| Default (3600 s), no client's total changed | 360 | 120 | 3 | 483 |
| Default (3600 s), every client's total changed | 360 | 120 | 17 (3 + 2 per client) | 497 |
| `traffic_interval_seconds: 60`, every client changed | 360 | 120 | 78 (one a minute, plus the round when the hour turns) | 558 |

The first round after a start adds 3 + 2 per client with traffic once (17 here). Each page costs the router 41-74 ms
and under 3 KB; no CPU change was visible on the router measured. `traffic_usage: false` turns the Traffic Analyzer
reads off (the capability is then not offered).

**Device events.** From the uptime and the WAN link speed, Pengumon records `restarted` (the router's boot time moved
on: it started again; found also when it happened while Pengumon was not running) and `wan_link` (the link speed
changed, went down or came back). They are not incidents (a restart already shows as an outage); they appear in
`GET /api/devices/{id}/events`, the incident inspector (within 5 minutes of the incident), the dossier and the AI pack.
The `snmp` driver gives the uptime too (`sysUpTime`), so its restarts are recorded the same way; `sysUpTime` counts
hundredths of a second in 32 bits and goes back to 0 about every 497 days, and a drop of exactly that much is taken as
the counter wrapping, not a restart. An uptime above 20 years is a bad number and is ignored. Events are kept as long
as the quarter-hour history (`database.long_rollup_retention_days`, 400 days by default).

## Changing devices without a restart

Devices are edited on the Settings page (Devices and services). A save applies live:

- a device whose settings did not change keeps its connection and polling;
- a changed device is stopped and rebuilt, a removed one is stopped and its connection closed, a new one starts;
- the line load and the access-line wording follow the new set of devices;
- path hops linked to a removed device are unlinked in one path version. Its stored readings stay until retention
  removes them.

A device's id is fixed once saved: its history and sign-in are kept under it. To use another id, remove the device and
add it again.

A device that cannot run (a CA file that is missing, say) no longer stops the start. It is left out, logged, and its
card on the Settings page shows why. Passwords are not part of the list: use Set up on the saved device (they stay in
`data/secrets.json`).

## OpenWrt

The `openwrt` driver reads the router through ubus's JSON-RPC at `/ubus` (uhttpd with `uhttpd-mod-ubus`, and `rpcd`
with `rpcd-mod-luci`, `rpcd-mod-file` and `rpcd-mod-iwinfo`; an image with LuCI has all of them). It logs in with
`session login`, and every poll is then one HTTP request with a batch of calls, plus one for the Wi-Fi stations when
the client list is due (every `clientlist_interval_seconds`, default 60). It only calls methods that report state:

- `network.interface dump`: which device the WAN interface (`options.wan_interface`, default `wan`) is on. In LuCI's ACLs for root: yes
- `luci-rpc getNetworkDevices`: that device's byte counters (64-bit). In LuCI's ACLs for root: yes
- `system info`: memory in use (total less available). In LuCI's ACLs for root: yes
- `system board`: the model (once an hour). In LuCI's ACLs for root: yes
- `file read /proc/stat`: CPU % between two polls. In LuCI's ACLs for root: no
- `luci-rpc getDHCPLeases`, `getWirelessDevices`: the clients, and the Wi-Fi interfaces and their bands. In LuCI's ACLs for root: yes
- `iwinfo assoclist`: each Wi-Fi station's signal, link rates and connected time. In LuCI's ACLs for root: yes
- `file read /proc/self/net/arp`: whether a client with a lease and no Wi-Fi link is online. In LuCI's ACLs for root: no

A call the ACL refuses only leaves out what needs it (one warning in the log names it): without `/proc/stat` there is
no CPU %, and without the ARP table every client with a DHCP lease that is on no radio counts as wired and online
(OpenWrt cannot otherwise tell a wired client from a Wi-Fi one that left before its lease ran out). A session the
router forgets (rpcd restarted, or it expired) is replaced by one new login.

**Set up a read-only user** (better than root, whose password can change everything). On the router, add the ACL as
`/usr/share/rpcd/acl.d/pengumon.json`:

```json
{
	"pengumon": {
		"description": "Pengumon: read-only monitoring (WAN counters, CPU, memory, clients)",
		"read": {
			"ubus": {
				"network.interface": [ "dump" ],
				"luci-rpc": [ "getNetworkDevices", "getDHCPLeases", "getWirelessDevices" ],
				"system": [ "board", "info" ],
				"iwinfo": [ "assoclist" ],
				"file": [ "read" ]
			},
			"file": {
				"/proc/stat": [ "read" ],
				"/proc/*/net/arp": [ "read" ]
			}
		}
	}
}
```

then a login for it in `/etc/config/rpcd` (`$p$pengumon` uses the password of a system user `pengumon`, made with
`passwd`; a `$1$...` crypt hash from `uhttpd -m <password>` works too), and restart rpcd:

```
config login
	option username 'pengumon'
	option password '$p$pengumon'
	list read 'pengumon'
```

```sh
/etc/init.d/rpcd restart
```

The `file` entries are paths rpcd resolves first: `/proc/net` is a link to `/proc/self/net`, hence `/proc/*/net/arp`.
The login has no `write` list, so it cannot change anything even through methods that would. To use root instead,
add the same file: root's `list read '*'` takes in every ACL group, this one included. On the Settings page, set
the username to `pengumon` (the driver's default is `root`), and the password like any other device's. Over `https://` the
router's certificate is checked as for any device (`tls:` above): OpenWrt's own is self-signed, so give its
certificate as `ca_file` (`/etc/uhttpd.crt`, converted to PEM) or set `verify: false`.

Tested against OpenWrt 24.10 (rpcd, uhttpd and LuCI as its x86-64 image ships them); older releases are untested.

## UPnP router (`upnp_igd`)

Most home routers and ISP boxes answer UPnP's Internet Gateway Device profile without a password. This driver reads
their WAN traffic, so Pengumon shows line load and notices a router restart with no vendor code. Add a device of kind
"UPnP router"; there is no sign-in to set up.

* **Address.** The router's address (`http://192.168.88.1`) is enough: Pengumon asks that router, and only that one,
  over UDP where its UPnP description is. Some routers do not answer; then give the description's own address, as the
  router's UPnP page or a discovery run shows it (`http://192.168.88.1:5000/rootDesc.xml`). Some routers pick a new
  port at every start; with a bare host Pengumon finds it again, with a full address you must update it.
* **What it reads**, every 30 s by default: the WAN byte counters (`GetTotalBytesReceived`, `GetTotalBytesSent`) and
  the connection's uptime (`GetStatusInfo`), used only to tell a counter wrap from a router restart. It never asks
  for the external IP address. It does not read the link speed (`GetCommonLinkProperties`): that is the router's
  claim, not a measurement, and there is no place for it in the stored readings; set the line capacity in Settings.
* **Limits.** UPnP counters are 32 bits wide, so they wrap at 4.3 GB (a few minutes at 100 Mbit/s): a counter that
  goes back is a wrap, unless the uptime went back too (a restart). Polling more slowly than every few minutes on a
  fast line can miss a whole wrap. Many routers have UPnP switched off and answer nothing. There is no CPU, memory or
  client list.
* **Safety.** Only the configured address is contacted; no redirect is followed; a control address on another host is
  never called; answers are read up to 64 KB and XML with a DOCTYPE or entity is refused. Network discovery proposes
  this driver for a router that answers UPnP and matches no vendor driver.

## SNMP

The `snmp` driver reads any SNMPv2c or SNMPv3 agent (most routers, managed switches and NAS boxes) over UDP, port 161
unless the URL names another (`snmp://192.168.88.2:1161`). Only GET and GETBULK requests are sent. SNMPv1 is not
supported.

- WAN byte counters: IF-MIB `ifHCInOctets` / `ifHCOutOctets` (64-bit), else `ifInOctets` / `ifOutOctets` (32-bit, wraps handled) (capability `wan_counters`)
- CPU %: HOST-RESOURCES-MIB `hrProcessorLoad` (each processor's one-minute average), else net-snmp's UCD-SNMP-MIB tick counters (capability `router_system`)
- memory in use: HOST-RESOURCES-MIB `hrStorage`: the RAM row (net-snmp's "Available memory", buffers and cache rows are used as `free` does) (capability `router_system`)
- model: the first line of `sysDescr`
- restarts: `sysUpTime` going back: the interfaces are looked up again and the counters start a new baseline

Which interface is the WAN: `options.wan_interface` names it by its `ifName` or `ifDescr` (exactly, else ignoring
case). Unset, it is the interface of the IPv4 default route (`ipRouteTable`, `inetCidrRouteTable` or
`ipCidrRouteTable`). When neither finds one, the device still reports CPU and memory and the WAN counters say what
to set (the message lists the interface names the agent has). For a switch, name its uplink port. There is no
client list.

The first poll walks a few tables (the interfaces, processors and storage) and every later one is one GET (more when
the agent is a large switch with many processors); the layout is looked up again every hour, after a restart, and
when a row it names disappears. Tested against net-snmp 5.9.

### SNMPv2c (the default)

The community string is the device's password (environment, secret file or the dashboard, as for any device); the
username is not used. A v2c community string crosses the network in clear text, so give the agent a read-only
community that is used for nothing else, and limit it to pengumon's address if the device can. A wrong community
string looks like no answer at all (agents ignore those): the error says so. On net-snmp,
`rocommunity <community> <pengumon's address>` in `snmpd.conf` is enough.

### SNMPv3

`options.version: "3"` uses the User-based Security Model (RFC 3414): the agent knows a user, and each request is
signed with the user's authentication key and, at `authPriv`, encrypted with its privacy key. Prefer it wherever the
agent has it: nothing secret crosses the network and a forged or replayed answer is refused.

| Setting | Where | Values |
|---|---|---|
| user name | the device's username (settings or `PENGUMON_DEVICE_<ID>_USERNAME`; the setup dialog asks for it) | at most 32 bytes |
| authentication password | the device's password, as for any device | at least 8 characters |
| privacy password | its own secret: the setup dialog, `PENGUMON_DEVICE_<ID>_PRIVACY_PASSWORD`, `/run/secrets/pengumon_<id>_privacy_password`, or `data/secrets.json` `devices.<id>.privacy_password` | at least 8 characters; `authPriv` only |
| `security_level` | options | `authPriv` (default), `authNoPriv`, `noAuthNoPriv` |
| `auth_protocol` | options | `SHA` (HMAC-SHA-96, default), `SHA-224`, `SHA-256`, `SHA-384`, `SHA-512` (RFC 7860) |
| `priv_protocol` | options | `AES` (AES-128-CFB, RFC 3826) |
| `context_name` | options | optional; most agents have only the default (empty) context |

```yaml
    options:
      version: "3"
      security_level: authPriv  # needs the privacy password
      auth_protocol: SHA-256    # as the user was created on the agent
      wan_interface: "eth1"     # ifName or ifDescr; unset: the default route's interface
      timeout_seconds: 2        # per try (default 2, at most 30)
      retries: 1                # more tries per request (default 1, at most 5)
```

The SNMPv3 options are refused with `version: "2c"`, so a setting is never silently ignored; the settings page shows
them only for version 3. MD5 and DES are not supported (both are broken): create the user with SHA (or SHA-2) and
AES. `noAuthNoPriv` has no password, so the setup dialog is not offered: set the user name in the device's settings.
A privacy password is never sent back to the page, like the password; the setup dialog asks for it only for an
`authPriv` device, and leaving it out of a credentials call keeps the stored one. The dashboard refuses (and does not
save) a user name over 32 bytes or a password under 8 characters. An SNMPv3 device whose settings are incomplete sends
nothing at all: it never falls back to SNMPv2c, and its password is never used as a community string.

What happens on the wire: the first request is preceded by an empty probe that learns the agent's engine id (and a
first guess of its clock); each password becomes a key once (1 MiB of hashing) and is localized to the engine once.
Every answer is checked before anything in it is used: its message id, its HMAC over the whole datagram, and the time
window (the agent's boot count must not go back and its clock not more than 150 s behind what pengumon expects); an
answer that fails is dropped and the error says why, as is a datagram that does not decode (anyone can send one; the
request waits on for the genuine answer). After an agent restart its authenticated "not in time window"
report resynchronizes the clock and the request is sent again, once. The agent's refusals become clear errors
(`auth`): an unknown user, a wrong authentication password or protocol (wrongDigests), a security level the user does
not have, a context the agent does not know. net-snmp drops a request it cannot decrypt, or one for a context it does
not have, without an answer: when such a request gets none while the agent still answers the probe, the error names
the privacy password (and the context name, when one is set).

On net-snmp (`/etc/snmp/snmpd.conf`, then restart snmpd; net-snmp moves `createUser` lines into its own store and
replaces the passwords by localized keys):

```
createUser pengumon SHA-256 "an authentication password" AES "a privacy password"
rouser pengumon priv
```

`rouser pengumon priv` gives read-only access at `authPriv`; `auth` instead allows `authNoPriv` (and `rouser -V <view>`
narrows what the user may read). Check it from another machine with
`snmpget -v3 -l authPriv -u pengumon -a SHA-256 -A '...' -x AES -X '...' <address> sysDescr.0`, then enter the user
name and both passwords in the device's setup dialog.

## Adding a driver

Your device is not listed above? A driver is one Python module, and nothing else in Pengumon needs to change for it.
[adding-a-device.md](adding-a-device.md) is the full guide: how Pengumon talks to devices, choosing capabilities, writing
the driver, recording and redacting your device's answers into tests, the Ask AI pack and the pull request checklist.
Start by opening the "Support for a new device" issue.
