"""Shared fixtures of the settings store tests: a config.yaml shaped like a real install (every section a live file
has; made-up addresses and names only) and a SettingsManager on a database and secrets.json in tmp_path."""

from pathlib import Path
from typing import Optional

from pengumon.config import load_config
from pengumon.db.repository import DatabaseRepository
from pengumon.secrets import SecretStore
from pengumon.settings.hub import SettingsHub
from pengumon.settings.manager import SettingsManager

OWNER_SHAPED = """\
server:
  host: 0.0.0.0
  port: 8000
  log_level: info
prober:
  interval_seconds: 1.0
  timeout_seconds: 1.0
  ping_count_per_probe: 1
targets:
  - {id: gw, name: Gateway, address: 192.168.88.1, category: gateway}
  - {id: pub, name: Public, address: 198.51.100.1, category: public}
dns_probes:
  - {id: dns_a, name: Lookup A, domain: example.com, record_type: A}
  - {id: dns_b, name: Lookup B, domain: example.org, nameserver: 192.0.2.53, record_type: AAAA}
classifier:
  consecutive_failures_for_incident: 3
  consecutive_failures_for_latency_incident: 10
  local_latency_spike_threshold_ms: 25
  public_latency_spike_threshold_ms: 90
  busy_load_pct: 70
  path_trace_target: 198.51.100.1
line:
  isp_name: Example ISP
  down_mbps: 940
  up_mbps: 35
database:
  db_path: data/pengumon.db
  retention_days: 7
  rollup_retention_days: 400
service_checks:
  interval_seconds: 30
  services:
    - {id: app1, name: App One, url: "http://192.0.2.10:8080/health"}
    - {id: app2, name: App Two, url: "https://app.example.com/healthz", timeout_seconds: 3}
devices:
  - {id: modem, driver: xfinity_xb, url: "http://192.168.88.2", poll_interval_seconds: 600}
  - id: router
    driver: asuswrt
    url: "http://192.168.88.1"
    model: Example-Mesh
    options: {mesh_node_labels: {"02:00:00:00:00:01": Attic}}
privacy:
  client_details: token
"""

WEBHOOKS = """\
notifications:
  public_url: https://pengumon.example.com
  webhooks:
    - name: Home
      url: "https://hooks.example.com/path-token-abc?k=q-secret"
      headers: {X-Plain: inline-secret-xyz, X-Ref: "secret:ha_token"}
"""


def write_config(tmp_path: Path, text: str = OWNER_SHAPED, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


async def make_manager(tmp_path: Path, text: Optional[str] = OWNER_SHAPED, *, environ=None, secrets: dict = None,
                       db: str = "pengumon.db", load: bool = True, hub: Optional[SettingsHub] = None):
    """(manager, config path). The live config is load_config(lenient_rest=True) of the file, as at the app's start;
    the repository is initialised; secrets.json is tmp_path/secrets.json (preloaded with `secrets`)."""
    path = write_config(tmp_path, text if text is not None else "")
    config = load_config(str(path), lenient_rest=True)
    repo = DatabaseRepository(db_path=str(tmp_path / db))
    await repo.initialize()
    store_path = tmp_path / "secrets.json"
    if secrets is not None:
        import json
        store_path.write_text(json.dumps(secrets), encoding="utf-8")
    manager = SettingsManager(config, repo, SecretStore(store_path), hub or SettingsHub(), config_path=path,
                              environ=environ if environ is not None else {}, secrets_dir=None)
    if load:
        await manager.load()
    return manager, path
