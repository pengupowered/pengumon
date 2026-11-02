# 🛠️ Configuration (`config.yaml`)

The sections below are the core ones. Devices, service checks and clients have their own pages: [devices.md](devices.md), [services.md](services.md), [clients.md](clients.md).

`config.example.yaml` is the tracked template; copy it to `config.yaml` (git-ignored) and edit that copy.

## Where files live, and the listen address

Everything one `pengumon paths` prints (it also says where each value came from):

| What | Setting | Default |
|---|---|---|
| Data directory (database, `secrets.json`, `sessions.json`, `backups/`) | `PENGUMON_DATA` | Windows `%LOCALAPPDATA%\Pengumon`, macOS `~/Library/Application Support/Pengumon`, otherwise `$XDG_STATE_HOME/pengumon` or `~/.local/state/pengumon` (created with mode 0700) |
| Config file | `PENGUMON_CONFIG` | `<data directory>/config.yaml`; a missing file means the defaults |
| Database | `database.db_path` | `<data directory>/pengumon.db`. A relative `db_path` is relative to the working directory (the old meaning: the container's `data/pengumon.db` with working directory `/app`); new configs leave it out |
| Listen address | `PENGUMON_HOST`, `PENGUMON_PORT`, then `server.host`, `server.port` | `127.0.0.1`, `8000` |

The container image sets `PENGUMON_DATA=/app/data`, `PENGUMON_CONFIG=/app/config.yaml` and `PENGUMON_HOST=0.0.0.0`, so
the compose files behave as before. Outside a container the default is this computer only; set `PENGUMON_HOST=0.0.0.0`
(and an access token) to reach it from the LAN.

`pengumon backup [--to DIR]` copies the database (a consistent snapshot, safe while Pengumon runs), `secrets.json` and
`sessions.json` into a timestamped folder (default `<data directory>/backups/`). `pengumon version` prints the version.

## Running without a container (preview)

Not the supported install yet (that comes with the native packages); for trying it out. Needs Python 3.11 or newer and,
for ICMP without root, `net.ipv4.ping_group_range` covering your user (Linux).

```bash
python3 -m venv ~/.venvs/pengumon
~/.venvs/pengumon/bin/pip install pengumon-X.Y.Z-py3-none-any.whl     # the wheel from a build or a release
~/.venvs/pengumon/bin/pengumon paths
~/.venvs/pengumon/bin/pengumon                                         # http://127.0.0.1:8000
```

Build the wheel from a checkout with `pip wheel --no-deps --no-build-isolation -w dist .` (needs the locked
`requirements-build.txt`). Put your `config.yaml` in the data directory shown by `pengumon paths`.

You can customize monitored targets, probe frequencies, and alert thresholds in `config.yaml` (start from `config.example.yaml`):

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

targets:                         # the dashboard shows them in this order, then the dns_probes
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

dns_probes:                      # left out: two default lookups; [] : none
  - id: "dns_system_google"
    name: "DNS Lookup (google.com)"
    domain: "google.com"
    enabled: true
```

Categories place a target on the path: `local` (router or mesh), `gateway` (modem or the provider's box), `isp` (the
provider's hops, in path order), `public` (servers on the internet; the Internet figure is their median). Any number of
each works, none included. `custom` targets (a NAS, a VPN endpoint, a server) are probed and charted but are no stage of
the path: Path health lists them under the chain.

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
