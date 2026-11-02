# 🛠️ Configuration (`config.yaml`)

The sections below are the core ones. Devices, service checks and clients have their own pages: [devices.md](devices.md), [services.md](services.md), [clients.md](clients.md).

`config.example.yaml` is the tracked template; copy it to `config.yaml` (git-ignored) and edit that copy.

## Where files live, and the listen address

Everything one `pengumon paths` prints (it also says where each value came from):

- **Data directory** (the database, `secrets.json`, `sessions.json`, `backups/`): set with `PENGUMON_DATA`. The
  default is `%LOCALAPPDATA%\Pengumon` on Windows, `~/Library/Application Support/Pengumon` on macOS, otherwise
  `$XDG_STATE_HOME/pengumon` or `~/.local/state/pengumon` (created with mode 0700).
- **Config file**: set with `PENGUMON_CONFIG`. The default is `<data directory>/config.yaml`; a missing file means
  the defaults.
- **Database**: set with `database.db_path`. The default is `<data directory>/pengumon.db`. A relative `db_path` is
  relative to the working directory (the old meaning: the container's `data/pengumon.db` with working directory
  `/app`); new configs leave it out.
- **Listen address**: set with `PENGUMON_HOST` and `PENGUMON_PORT`, then `server.host` and `server.port`. The
  default is `127.0.0.1`, port `8000`.

The container image sets `PENGUMON_DATA=/app/data`, `PENGUMON_CONFIG=/app/config.yaml` and `PENGUMON_HOST=0.0.0.0`, so
the compose files behave as before. Outside a container the default is this computer only; set `PENGUMON_HOST=0.0.0.0`
(and an access token) to reach it from the LAN.

`pengumon doctor` checks that this host can probe: the ICMP backend, an echo to 127.0.0.1, a TTL=1 probe, the gateway,
the data directory and the listen address, and the fix when ICMP is not permitted ([probing.md](probing.md)).

`pengumon backup [--to DIR]` copies the database (a consistent snapshot, safe while Pengumon runs), `secrets.json` and
`sessions.json` into a timestamped folder (default `<data directory>/backups/`). `pengumon version` prints the version.

## Running from the wheel by hand

The supported ways to install are in [install.md](install.md) (a script per system, or the container). To run it from a
wheel yourself you need Python 3.11 or newer. On Linux, ICMP without root needs `net.ipv4.ping_group_range` covering your
user (one `sudo` step on Debian, Ubuntu, Mint and Raspberry Pi OS: [probing.md](probing.md)); macOS and Windows need
nothing. Run `pengumon doctor` to check.

```bash
python3 -m venv ~/.venvs/pengumon
~/.venvs/pengumon/bin/pip install pengumon-X.Y.Z-py3-none-any.whl     # the wheel from a build or a release
~/.venvs/pengumon/bin/pengumon paths
~/.venvs/pengumon/bin/pengumon doctor                                  # can it probe? (exit 0 when it can)
~/.venvs/pengumon/bin/pengumon run                                     # http://127.0.0.1:8000
```

Build the wheel from a checkout with `pip wheel --no-deps --no-build-isolation -w dist .` (needs the locked
`requirements-build.txt`). Put your `config.yaml` in the data directory shown by `pengumon paths`.

## The probe path (targets): edited in the dashboard

The targets are stored in the database and edited in the dashboard (**Path health > Edit**), not in config.yaml. The
editor, Rediscover, the first-run view, Export and Import and the upgrade from 0.17 are described in
[path-editor.md](path-editor.md); how discovery works is in [discovery.md](discovery.md).

What config.yaml still does with `targets:`: a first start of a database that has no path yet imports the list once (a
new install can leave it out and have the path discovered instead); after that the list is ignored and the startup log
says so. It is an optional fixed list, used on the first start only; `config.example.yaml` has none, so a copy of it gives you discovery. A first import of more than 64 targets keeps the first 64 as the path and the extras as removed hops that you can restore in the editor. The syntax is the one Export writes and Import reads:

```yaml
targets:                         # imported once on a first start (then edited in the dashboard); in path order
  - id: "local_router"
    name: "Hop 1: Local Router (LAN)"
    category: "local"
    address: "192.168.1.1"
    enabled: true
    color: "#58a6ff"             # optional chart colour (#rrggbb), also on dns_probes; default: the next of eight series colours, then one no other target of its category has

  - id: "isp_gateway"
    name: "Hop 2: ISP Gateway"
    category: "gateway"
    address: "192.168.100.1"
    enabled: true

  - id: "cloudflare_dns"
    name: "Public: Cloudflare Anycast"
    category: "public"
    address: "1.1.1.1"
    enabled: true

  - id: "cloudflare_v6"
    name: "Public: Cloudflare over IPv6"
    category: "public"
    address: "one.one.one.one"   # an IPv4 or IPv6 address (fe80::1%eth0 for a link-local one), or a host name
    family: 6                    # a host name is probed at its IPv4 (4, the default) or IPv6 (6) address

dns_probes:                      # left out: two default lookups; [] : none
  - id: "dns_system_google"
    name: "DNS Lookup (google.com)"
    domain: "google.com"
    enabled: true
```

Categories place a target on the path: `local` (router or mesh), `gateway` (modem or the provider's box), `isp` (the
provider's hops, in path order), `public` (servers on the internet; the Internet figure is their median). Any number of
each works, none included. `custom` targets (a NAS, a VPN endpoint, a server) are probed and charted but are no stage of
the path: Path health lists them under the chain. The editor's fields are optional keys of a target: `group` (adjacent
hops of one role with the same group are one node), `device_id` (an id from `devices:`; set by itself for a hop of
your network or the line at a device's address) and `device_unlinked` (`true`: the owner chose "No device", so no
automatic link is made). Whether a hop ignores ping is observed, never set: an `ignores_ping`
key is ignored.

**IPv4 and IPv6.** An `address` can be an IPv4 or an IPv6 address; it is probed over ICMPv4 or ICMPv6 accordingly. A
link-local IPv6 address needs its interface, `fe80::1%eth0`. A host name is looked up before every probe and probed at
its first address of `family`: `4` (the default, as before 0.17) or `6`. `family` does nothing for an address. The
add-target dialog and `POST /api/targets` take the same field. When the host has no IPv6 (no route to the internet, or
none in the operating system), an IPv6 target records no samples and shows **IPv6 unavailable on this host**, a neutral
state like a hop that ignores ping: no loss, no outage, no incident. Pengumon checks every five minutes (and as soon as
a send fails for lack of a route), so a network that gains or loses IPv6 is followed. Loopback (`::1`) and link-local
targets need no route to the internet. [probing.md](probing.md) has the details.

## The other settings

Access (the token, sign-in, view only, allowed host names) is set with environment variables, not in `config.yaml`:
see [Security](security.md).

You can customize probe frequencies and alert thresholds in `config.yaml` (start from `config.example.yaml`).

```yaml
prober:
  interval_seconds: 1.0
  timeout_seconds: 1.0
  hop_status:                    # when a single target turns amber / red on the dashboard (all optional)
    degraded_loss_fraction: 0.1  # share of the last 10 probes lost, strictly above: degraded
    down_loss_fraction: 0.4      # strictly above: down
    local_latency_ms: 20         # average latency / jitter above these turn a target amber:
    local_jitter_ms: 10          #   local_*, gateway_*, public_* and dns_* (ISP hops are judged on loss only)
    gateway_latency_ms: 30
    gateway_jitter_ms: 15
    public_latency_ms: 60
    public_jitter_ms: 25
    dns_latency_ms: 100
    dns_jitter_ms: 40
```

New in 0.2.0 (all optional, shown with example values; the defaults are noted):

```yaml
database:
  retention_days: 7              # raw probe samples, DOCSIS, router metrics
  rollup_retention_days: 400     # per-minute rollups (history charts, report, dossier)

line:                            # internet line capacity; unset = learned from the router history
  down_mbps: 1000
  up_mbps: 40
  isp_name: "Acme"               # optional; names the dossier ("Acme Outage & Escalation Dossier", acme_outage_dossier_24h.md); default "ISP"

classifier:
  busy_load_pct: 60              # WAN load (% of capacity) from which latency / ISP-hop loss counts as "busy"
```

### Route-change checks (`discovery:`)

Pengumon reruns discovery quietly once a day, and about ten minutes after an incident ends, and tells you when your
provider's route no longer matches the applied path ("Your path changed · Review" on Path health, and one
notification through ntfy or your webhooks). Both settings are optional; these are the defaults:

```yaml
discovery:
  background: true     # false: no background checks (Rediscover in the path editor still works)
  asn_lookup: false    # true: background runs also ask Team Cymru who operates each public hop
```

A background run sends the traces, the reverse-DNS queries and one ping and one front-page request to each of the
common gateway addresses that are not on the traced path; nothing else (no device pages, UPnP or SNMP). With
`asn_lookup: false` nothing about your path leaves your computer. Details: [discovery.md](discovery.md).
