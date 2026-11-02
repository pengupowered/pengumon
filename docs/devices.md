# Devices (`devices`): modem, gateway, router

Pengumon reads the devices listed under `devices:`. Each entry names a **driver**, which decides what the device can
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
    options:                     # driver-specific, checked at startup
      clientlist_interval_seconds: 60  # asuswrt: the client list (the heaviest answer) is read this often
      # mesh_node_labels:            # asuswrt: name mesh nodes by MAC (or its last six hex digits), see clients.md
      #   "AA:BB:CC:00:11:22": "Hall"
```

- `xfinity_xb`
  - Devices: Xfinity/Comcast XB6, XB7, XB8
  - Capabilities: `access_line` (DOCSIS)
  - URL: `http(s)://`
  - Username: `admin`
  - Password: the gateway's admin password
- `asuswrt`
  - Devices: AsusWRT routers (RT-AX, ZenWiFi, ...)
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
at startup with a message naming the entry. Configuration errors never quote the values (a password stays out of
the log). Several devices of one kind (two routers, two modems) may be enabled: each one's readings are stored under
its id. The single-device views (the dashboard's modem / line and router panels, the live message, the incident inspector, the
dossier and the line-load learning) use the first enabled one in config order; when every device of that kind is
switched off (`enabled: false`), the first one still shows its stored history and the line capacity is still learned
from it. The live line load needs an enabled device.

Without a `devices:` list there are no devices (the probes, incidents and reports work without them). The ids are
the keys of each device's stored history and credentials: the shipped `config.yaml` uses `modem` and `router`, the
ids its history was stored under, so renaming a device starts its history afresh (the old rows are kept until
retention, and a startup warning names stored ids that are not in the config).

**Credentials** never go in `config.yaml`. For each device, the first of these that has a value wins (username and
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
add the same file: root's `list read '*'` takes in every ACL group, this one included. In `config.yaml`, set
`username: pengumon` (the driver's default is `root`), and the password like any other device's. Over `https://` the
router's certificate is checked as for any device (`tls:` above): OpenWrt's own is self-signed, so give its
certificate as `ca_file` (`/etc/uhttpd.crt`, converted to PEM) or set `verify: false`.

Tested against OpenWrt 24.10 (rpcd, uhttpd and LuCI as its x86-64 image ships them); older releases are untested.

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

**Adding a driver:** a module in `src/pengumon/drivers/` with a class that has `name`, `offers` (its
capabilities), `options_model` (a pydantic model of its `options:`, extra keys forbidden), `default_poll_seconds`,
a `from_config(cfg, credentials, client=None)` classmethod that builds it from a `devices:` entry (use
`drivers.http.DeviceHttp` and `tls_verify(cfg.tls)` for the HTTP client), and `host`, `missing_configuration()`,
`set_credentials()`, `poll()` (returning readings per capability, `capabilities.PollResult`) and `aclose()`. Two
class attributes are optional: `url_schemes` (default `("http", "https")`; snmp has `("snmp",)`) and
`default_username` (used when the entry sets no `username`; openwrt has `root`). Add it to `DRIVERS` in
`drivers/__init__.py`. Its test feeds recorded device answers through an
`httpx.MockTransport` (see `tests/test_router.py` and `tests/test_driver_http.py`). Scheduling, rates, health,
storage, logging and tracing come from the shared device runner.
