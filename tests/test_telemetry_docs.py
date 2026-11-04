"""docs/telemetry.md lists every instrument; docs/grafana/pengumon.json queries only metrics pengumon exports and holds
nothing of any network (an address, a host name)."""

import ast
import json
import re
from pathlib import Path

from pengumon.observability.gauges import GAUGES

ROOT = Path(__file__).resolve().parent.parent
TELEMETRY = ROOT / "src" / "pengumon" / "observability" / "telemetry.py"
DOC = (ROOT / "docs" / "telemetry.md").read_text(encoding="utf-8")
DASHBOARD = ROOT / "docs" / "grafana" / "pengumon.json"
# what the Prometheus names add for a unit (others add nothing; a unit in braces is dropped)
UNIT_WORDS = {"s": "seconds", "By": "bytes", "%": "percent", "Mbit/s": "Mbit_per_second", "dBmV": "dBmV", "dB": "dB",
              "dBm": "dBm"}


def _prometheus_name(otel_name: str, unit: str, kind: str) -> str:
    name = otel_name.replace(".", "_")
    if unit in UNIT_WORDS:
        name += "_" + UNIT_WORDS[unit]
    return name + "_total" if kind == "counter" else name


def _instruments():
    """[(kind, prometheus base name)] of every instrument created in telemetry.py and every gauge with an OTel name."""
    found = []
    for node in ast.walk(ast.parse(TELEMETRY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("create_"):
            kind = node.func.attr.removeprefix("create_")
            unit = next((k.value.value for k in node.keywords if k.arg == "unit"), "")
            found.append((kind, _prometheus_name(node.args[0].value, unit, kind)))
    found += [("gauge", _prometheus_name(g.otel, g.unit, "gauge")) for g in GAUGES if g.otel]
    return found


def test_the_instrument_scan_finds_the_instruments():
    kinds = dict((name, kind) for kind, name in _instruments())
    assert kinds["pengumon_probe_round_duration_seconds"] == "histogram"
    assert kinds["pengumon_probes_total"] == "counter" and kinds["pengumon_process_memory_rss_bytes"] == "gauge"
    assert len(kinds) > 60


def test_every_instrument_is_in_the_telemetry_doc():
    missing = sorted({name for _, name in _instruments() if name not in DOC})
    assert missing == [], f"add these to docs/telemetry.md: {missing}"


NEW_GAUGES = {"pengumon.process.memory.rss", "pengumon.process.cpu.time", "pengumon.process.threads",
              "pengumon.process.open_fds", "pengumon.asyncio.tasks", "pengumon.dns.lookups.stuck",
              "pengumon.db.sample_buffer", "pengumon.db.file.size", "pengumon.ws.clients", "pengumon.device.failures",
              "pengumon.device.backoff", "pengumon.alerts.held"}


def test_the_new_gauges_have_the_same_name_in_the_prometheus_text_and_over_otlp():
    by_otel = {g.otel: g for g in GAUGES if g.otel}
    assert NEW_GAUGES <= set(by_otel)
    for otel in NEW_GAUGES:
        gauge = by_otel[otel]
        assert gauge.prom == _prometheus_name(otel, gauge.unit, "gauge"), otel


def _dashboard():
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def test_the_dashboard_queries_only_metrics_pengumon_exports():
    known = set()
    for kind, name in _instruments():
        known.add(name)
        if kind == "histogram":
            known |= {f"{name}_bucket", f"{name}_sum", f"{name}_count"}
    known |= {g.prom for g in GAUGES}
    wrong = set()
    for panel in _dashboard()["panels"]:
        for target in panel.get("targets", []):
            for token in re.findall(r"\bpengumon_[A-Za-z0-9_]+", target.get("expr", "")):
                if token not in known:
                    wrong.add(token)
    assert wrong == set()


def test_the_dashboard_panels_are_well_formed():
    panels = _dashboard()["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))
    assert _dashboard()["uid"] == "pengumon"
    rows = {p["title"] for p in panels if p["type"] == "row"}
    assert {"pengumon itself", "Storage", "Dashboard clients", "Devices", "Discovery and alerts",
            "Settings and preview"} <= rows
    assert {"Cable line: $dev_cable", "WAN and line load: $dev_wan", "Router: $dev_router",
            "Wi-Fi and mesh: $dev_wifi"} <= rows
    for panel in panels:  # nothing overlaps: a panel starts below or beside the others
        box = panel["gridPos"]
        assert box["x"] + box["w"] <= 24 and box["w"] > 0 and box["h"] > 0
    cells = set()
    for panel in panels:
        box = panel["gridPos"]
        for x in range(box["x"], box["x"] + box["w"]):
            for y in range(box["y"], box["y"] + box["h"]):
                assert (x, y) not in cells, f"{panel['title']} overlaps another panel"
                cells.add((x, y))


def test_the_dashboard_holds_no_address_or_host_name():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
    assert not re.search(r"https?://", text)
    assert not re.search(r"\b[a-z0-9-]+\.(lan|local|home|internal)\b", text, re.I)


def test_every_dashboard_query_is_scoped_to_the_chosen_service():
    """The `service` variable (an OTEL_SERVICE_NAME; default pengumon) picks one instance: a second instance, or a
    test run exporting as another service, never mixes into the panels."""
    dashboard = _dashboard()
    (variable,) = [v for v in dashboard["templating"]["list"] if v["name"] == "service"]  # (the others list devices)
    assert variable["current"]["value"] == "pengumon"
    metric = re.compile(r"\b(?:pengumon|http_server)_\w+(\{[^}]*\})?")
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            query = target.get("expr") or target.get("query") or ""
            kind = (target.get("datasource") or {}).get("type")
            if kind == "prometheus":
                for m in metric.finditer(query):
                    assert m.group(1) and 'job="$service"' in m.group(1), f"{panel['title']}: {m.group(0)}"
            elif kind == "loki":
                assert 'service_name="$service"' in query, panel["title"]
            elif kind == "tempo":
                assert 'resource.service.name="$service"' in query, panel["title"]


def test_no_dashboard_query_needs_a_prometheus_feature_flag():
    """A stock Prometheus (the grafana/otel-lgtm image) refuses the experimental range modifiers `anchored` and
    `smoothed` (promql-extended-range-selectors): seven panels errored on them."""
    for panel in _dashboard()["panels"]:
        for target in panel.get("targets", []):
            query = target.get("expr", "")
            assert not re.search(r"\b(anchored|smoothed)\b", query), f"{panel['title']}: {query}"


def test_no_time_series_panel_replaces_missing_values():
    """A time series' `noValue` is drawn as a value: "" became a flat 0 before a device's first reading (an SNR of
    0 dB). Missing data must stay a gap."""
    for panel in _dashboard()["panels"]:
        if panel.get("type") == "timeseries":
            assert "noValue" not in panel.get("fieldConfig", {}).get("defaults", {}), panel["title"]
