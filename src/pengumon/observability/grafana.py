"""Links from the dashboard to pengumon's telemetry in Grafana: its dashboard, its logs (Loki) and its traces (Tempo).

PENGUMON_GRAFANA_URL is Grafana's base URL (e.g. https://grafana.lan:3000); empty means no links. It must be a plain
http(s) base URL, without query, fragment or credentials: anything else stops the start with an error. The Grafana
dashboard is expected under the uid "pengumon".
"""

import json
import re
from typing import Dict, List, Mapping, Optional
from urllib.parse import quote

from pengumon.observability.export import SERVICE_NAME
from pengumon.settings.env import env_value

ENV = "PENGUMON_GRAFANA_URL"
DASHBOARD_UID = "pengumon"
_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")
_links: List[Dict[str, str]] = []


def _explore(grafana: str, datasource: str, query: dict, since: str = "now-6h") -> str:
    """A Grafana Explore link (the `panes` URL format of Grafana 10 and later) running one query."""
    pane = {"pm": {"datasource": datasource,
                   "queries": [{"refId": "A", "datasource": {"type": datasource, "uid": datasource}, **query}],
                   "range": {"from": since, "to": "now"}}}
    return f"{grafana}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(pane, separators=(',', ':')), safe='')}"


def telemetry_links(grafana: str) -> List[Dict[str, str]]:
    """The header links for a Grafana base URL; none for an empty one."""
    if not grafana:
        return []
    return [
        {"label": "Dashboard", "title": "Grafana: pengumon metrics", "url": f"{grafana}/d/{DASHBOARD_UID}/{SERVICE_NAME}"},
        {"label": "Logs", "title": "Grafana Explore: pengumon log (Loki)",
         "url": _explore(grafana, "loki", {"expr": f'{{service_name="{SERVICE_NAME}"}}', "queryType": "range"})},
        {"label": "Traces", "title": "Grafana Explore: pengumon traces (Tempo)",
         "url": _explore(grafana, "tempo", {"queryType": "traceql", "tableType": "traces", "limit": 50,
                                            "query": f'{{resource.service.name="{SERVICE_NAME}"}}'})},
    ]


def check_url(value: str, what: str = ENV) -> str:
    """`value` stripped, without a trailing "/", when it is a plain http(s) base URL (or empty). Raises ValueError,
    without repeating the value, otherwise."""
    grafana = (value or "").strip().rstrip("/")
    if grafana and not _URL_RE.fullmatch(grafana):
        raise ValueError(f"{what} must be a plain http(s) base URL such as https://grafana.lan:3000")
    return grafana


def configure(environ: Mapping[str, str]) -> List[Dict[str, str]]:
    """Reads PENGUMON_GRAFANA_URL and keeps the links for `links()`. Raises ValueError for a URL that is not a plain
    http(s) base URL. (The settings component then configures the effective display.grafana_url, configure_url.)"""
    return configure_url(env_value(ENV, environ))


def configure_url(url: Optional[str]) -> List[Dict[str, str]]:
    """Keeps the links of Grafana base URL `url` (None or empty: no links). Raises ValueError for one that is not a
    plain http(s) base URL; the links are then left as they were."""
    grafana = check_url(url or "")
    _links[:] = telemetry_links(grafana)
    return list(_links)


def links() -> List[Dict[str, str]]:
    return list(_links)
