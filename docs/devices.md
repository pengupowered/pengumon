# Devices (`devices`): modem, gateway, router

Add devices on the **Settings** page (Devices and services): a new device, its driver, address and options, then
**Set up** for its password. A save applies live ([below](#changing-devices-without-a-restart)). The YAML on this page
is the syntax that **Settings > Export** writes and **Import** reads; a `config.yaml` with a `devices:` list is read
only once, on the first start (see [configuration.md](configuration.md)).

Pengumon reads the devices in the `devices` list. Each entry names a **driver**, which decides what the device can
report (its *capabilities*: `access_line` for the line to the ISP such as DOCSIS levels and errors, `wan_counters`
for WAN throughput and line load, `router_system` for router CPU, memory and the client count, `client_inventory`
for the list of connected devices, `mesh` for the mesh nodes and how many clients each carries). Nothing else in the
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
```

## Supported devices

| Driver | Devices | Status | What it reads |
|---|---|---|---|
| `xfinity_xb` | Xfinity / Comcast XB6, XB7, XB8 gateways | Tested by the maintainer | The cable line (`access_line`): DOCSIS downstream and upstream levels, SNR, codeword errors |
| `asuswrt` | AsusWRT routers (RT-AX and others), with AiMesh | Tested by the maintainer | WAN traffic, CPU, memory, connected clients by band, mesh nodes |
| `openwrt` | OpenWrt routers (LuCI, or rpcd and uhttpd-mod-ubus) | Tested by the maintainer on OpenWrt 24.10 (x86-64 image); other releases untested | WAN traffic, CPU, memory, model, clients (DHCP leases, Wi-Fi stations by band) |
| `snmp` | Anything with an SNMPv2c agent: routers, switches, NAS boxes | Generic: a standard protocol, tested against net-snmp 5.9 | WAN (or uplink) traffic, CPU, memory, model; no client list |
| `upnp_igd` | Routers and gateways that answer UPnP's Internet Gateway Device profile | Generic: a standard profile, new in 0.24; some routers have UPnP switched off | WAN traffic (32-bit counters, wraps handled); no client list |

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
  - Capabilities: `wan_counters`, `router_system`, `client_inventory`, `mesh`
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
  - Devices: any SNMPv2c agent
  - Capabilities: `wan_counters`, `router_system`
  - URL: `snmp://host[:port]`
  - Username: not used
  - Password: the community string, see [SNMP](#snmp)

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

Everything is read once at startup, and the log names the source each device's password came from (never the
password). Credentials entered in the dashboard are written to `data/secrets.json` under `devices.<id>` (mode 0600).
A save that a higher-ranked source (the environment or a `/run/secrets` file) will override at the next start is
used until then; the answer names that source in `overridden_by` and the log warns. `docker-compose.yml` passes
`PENGUMON_DEVICE_MODEM_*` and `PENGUMON_DEVICE_ROUTER_*` from `.env` (empty when unset); for another device id, or a
`/run/secrets` file, add an `environment:` entry or a `secrets:` entry named `pengumon_<id>_password` there.

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

The `snmp` driver reads any SNMPv2c agent (most routers, managed switches and NAS boxes) over UDP, port 161 unless the
URL names another (`snmp://192.168.1.2:1161`). The community string is the device's password (environment, secret
file or the dashboard, as for any device); the username is not used. Only GET and GETBULK requests are sent. SNMPv1
and SNMPv3 are not supported. A v2c community string crosses the network in clear text, so give the agent a read-only
community that is used for nothing else, and limit it to pengumon's address if the device can.

- WAN byte counters: IF-MIB `ifHCInOctets` / `ifHCOutOctets` (64-bit), else `ifInOctets` / `ifOutOctets` (32-bit, wraps handled) (capability `wan_counters`)
- CPU %: HOST-RESOURCES-MIB `hrProcessorLoad` (each processor's one-minute average), else net-snmp's UCD-SNMP-MIB tick counters (capability `router_system`)
- memory in use: HOST-RESOURCES-MIB `hrStorage`: the RAM row (net-snmp's "Available memory", buffers and cache rows are used as `free` does) (capability `router_system`)
- model: the first line of `sysDescr`
- restarts: `sysUpTime` going back: the interfaces are looked up again and the counters start a new baseline

Which interface is the WAN: `options.wan_interface` names it by its `ifName` or `ifDescr` (exactly, else ignoring
case). Unset, it is the interface of the IPv4 default route (`ipRouteTable`, `inetCidrRouteTable` or
`ipCidrRouteTable`). When neither finds one, the device still reports CPU and memory and the WAN counters say what
to set (the message lists the interface names the agent has). For a switch, name its uplink port. There is no
client list. Options:

```yaml
    options:
      wan_interface: "eth1"   # ifName or ifDescr; unset: the default route's interface
      timeout_seconds: 2      # per try (default 2, at most 30)
      retries: 1              # more tries per request (default 1, at most 5)
```

The first poll walks a few tables (the interfaces, processors and storage) and every later one is one GET (more when
the agent is a large switch with many processors); the layout is looked up again every hour, after a restart, and
when a row it names disappears. A wrong community string looks like no answer at all (agents ignore those): the error
says so. On net-snmp, `rocommunity <community> <pengumon's address>` in `snmpd.conf` is enough. Tested against
net-snmp 5.9.

## Adding a driver

Your device is not listed above? A driver is one Python module, and nothing else in Pengumon needs to change for it.
[adding-a-device.md](adding-a-device.md) is the full guide: how Pengumon talks to devices, choosing capabilities, writing
the driver, recording and redacting your device's answers into tests, the Ask AI pack and the pull request checklist.
Start by opening the "Support for a new device" issue.
