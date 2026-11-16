"""Latency spikes in the database (db/spikes.py), the migration that converts the stored brief latency incidents, their
retention, and where they are counted: GET /api/latency-spikes, /api/report and the MCP `incidents` tool."""

import importlib
import json
import pkgutil
import sqlite3
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.db import migrations
from pengumon.db.repository import DatabaseRepository
from pengumon.db.storage_stats import KIND_OF_TABLE
from pengumon.incidents.spikes import LatencySpike
from pengumon.main import app, security_config
from pengumon.models.probes import ProbeSample, TargetConfig

DAY = 86400.0
T = 1_700_000_000.0


def _spike_migration():
    name = next(i.name for i in pkgutil.iter_modules(migrations.__path__) if i.name.endswith("_latency_spikes"))
    return importlib.import_module(f"{migrations.__name__}.{name}")


SPIKES = _spike_migration()


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the migration ------------------------------------------------------------------------------------------------

# name -> (kind, cause_code, start offset, duration (None: open), loss_seconds, expected)
STORED = {
    "a_isp_90s": ("upstream_latency", "access_line_drop", 0, 90.0, None, 0),
    "b_router_30s_expected": ("local_latency", "local_wifi_mesh_drop", 1000, 30.0, 0.0, 1),
    "c_legacy_line": ("legacy_latency", "access_line_drop", 2000, 60.0, None, 0),
    "d_gateway_just_under": ("gateway_latency", "double_nat_congestion", 3000, 119.9, None, 0),
    "e_isp_120s": ("upstream_latency", "access_line_drop", 4000, 120.0, None, 0),
    "f_isp_with_loss": ("upstream_latency", "access_line_drop", 5000, 60.0, 3.0, 0),
    "g_loss": ("isp_loss", "access_line_drop", 6000, 30.0, 30.0, 0),
    "h_open": ("upstream_latency", "access_line_drop", 7000, None, None, 0),
    "i_slow_dns": ("dns_slow", "dns_resolver_failure", 8000, 30.0, None, 0),
    "j_busy": ("busy_latency", "load_induced_latency", 9000, 10.0, None, 1),
}
CONVERTED = ("a_isp_90s", "b_router_30s_expected", "c_legacy_line", "d_gateway_just_under", "j_busy")


async def _old_database(path):
    """A database as the version before the spikes left it: the stored incidents above, evidence for two of them, and
    the path's categories."""
    repo = DatabaseRepository(db_path=str(path))
    await repo.initialize()
    await repo.close()
    con = sqlite3.connect(path)
    con.execute("DROP TABLE latency_spikes")
    con.execute(f"PRAGMA user_version={SPIKES.VERSION - 1}")
    for target_id, key, category in ((1, "lan", "local"), (2, "hop", "isp"), (3, "pub", "public")):
        con.execute("INSERT INTO targets (id, key) VALUES (?, ?)", (target_id, key))
        con.execute("INSERT INTO path_targets (id, position, config, updated) VALUES (?, ?, ?, ?)",
                    (key, target_id, json.dumps({"id": key, "category": category}), T))
    ids = {}
    for name, (kind, cause, offset, duration, loss, expected) in STORED.items():
        start = T + offset
        cur = con.execute(
            "INSERT INTO incidents (start_time, end_time, duration_seconds, cause_code, title, description, severity, "
            "affected_targets, resolved, expected, kind, loss_seconds) VALUES (?, ?, ?, ?, 't', 'd', 'warning', '[]', "
            "?, ?, ?, ?)", (start, None if duration is None else start + duration, duration, cause,
                            0 if duration is None else 1, expected, kind, loss))
        ids[name] = cur.lastrowid
    for name in ("a_isp_90s", "e_isp_120s"):
        iid, start = ids[name], T + STORED[name][2]
        con.execute("INSERT INTO incident_evidence (incident_id, written_at, window_start, window_end, resolution) "
                    "VALUES (?, ?, ?, ?, 'mixed')", (iid, T, start - 60, start + 180))
        con.execute("INSERT INTO incident_evidence_readings VALUES (?, 'router', 'r', ?, '{}')", (iid, start))
        for target, ts, width, latency, lat_max in (
                (2, start + 10, 0, 140.0, None), (3, start + 50, 0, 210.0, None),
                (3, start - 5, 0, 900.0, None),          # before the incident: not its peak
                (1, start + 20, 0, 500.0, None),         # the router: not the ISP stage's hops
                (2, start + 85, 10, None, 250.0)):       # a bucket that reaches into the incident: its highest
            con.execute("INSERT INTO incident_evidence_probes (incident_id, target, ts, width, n, ok, latency_ms, "
                        "lat_max) VALUES (?, ?, ?, ?, 1, 1, ?, ?)", (iid, target, ts, width, latency, lat_max))
    con.commit()
    con.close()
    return ids


async def test_the_migration_turns_brief_latency_incidents_without_loss_into_spikes(tmp_path):
    path = tmp_path / "old.db"
    ids = await _old_database(path)
    assert await migrations.migrate(str(path)) == migrations.LATEST_VERSION
    con = sqlite3.connect(path)
    spikes = con.execute("SELECT start_time, end_time, kind, stage, expected, peak_ms FROM latency_spikes "
                         "ORDER BY start_time").fetchall()
    assert spikes == [
        (T, T + 90, "upstream_latency", "isp", 0, 250.0),
        (T + 1000, T + 1030, "local_latency", "router", 1, None),       # no evidence: no peak
        (T + 2000, T + 2060, "legacy_latency", "line", 0, None),         # the cause code decides
        (T + 3000, T + 3119.9, "gateway_latency", "gateway", 0, None),
        (T + 9000, T + 9010, "busy_latency", "load", 1, None),
    ]
    kept = {r[0] for r in con.execute("SELECT id FROM incidents")}
    assert kept == {ids[n] for n in STORED if n not in CONVERTED}
    for table in ("incident_evidence", "incident_evidence_probes", "incident_evidence_readings"):
        assert {r[0] for r in con.execute(f"SELECT DISTINCT incident_id FROM {table}")} == {ids["e_isp_120s"]}, table
    con.close()
    # once only: the next start finds nothing more to do
    assert await migrations.migrate(str(path)) == migrations.LATEST_VERSION
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM latency_spikes").fetchone()[0] == 5


async def test_a_new_database_gets_the_table_and_nothing_to_convert(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "new.db"))
    await repo.initialize()
    assert await repo.latency_spikes_between(0, time.time()) == []
    await repo.close()


# --- stored, kept like the incidents -------------------------------------------------------------------------------

async def test_spikes_are_stored_read_by_window_and_bump_the_incidents_version(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "s.db"))
    await repo.initialize()
    before = repo.incidents_version
    for start, stage in ((T, "isp"), (T + 100, "router"), (T + 200, "isp")):
        await repo.create_latency_spike(LatencySpike(start_time=start, end_time=start + 30, kind="upstream_latency",
                                                     stage=stage, peak_ms=99.5))
    assert repo.incidents_version == before + 3
    rows = await repo.latency_spikes_between(T + 50, T + 200)
    assert [(r["start_time"], r["stage"], r["expected"], r["peak_ms"]) for r in rows] == [
        (T + 100, "router", False, 99.5), (T + 200, "isp", False, 99.5)]
    await repo.close()


async def test_spikes_are_pruned_with_the_incidents_and_kept_forever_with_them(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "r.db"), incident_retention_days=30)
    await repo.initialize()
    now = time.time()
    for age_days in (40, 10):
        await repo.create_latency_spike(LatencySpike(start_time=now - age_days * DAY, end_time=now - age_days * DAY + 30,
                                                     kind="upstream_latency", stage="isp"))
    assert (await repo.cleanup_old_samples(incident_retention_days=0)).get("latency_spikes", 0) == 0
    version = repo.incidents_version
    assert (await repo.cleanup_old_samples())["latency_spikes"] == 1
    assert repo.incidents_version == version + 1
    assert len(await repo.latency_spikes_between(0, now)) == 1
    assert KIND_OF_TABLE["latency_spikes"] == "incident"  # /api/storage counts them with the incidents
    await repo.close()


# --- where they are counted -------------------------------------------------------------------------------------------

GW = TargetConfig(id="gw", name="Gateway", address="192.0.2.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop 1", address="198.51.100.9", category="isp")


@pytest.fixture
async def served(app_state, tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "c.db"), sample_flush_seconds=0)
    await repo.initialize()
    await repo.import_path([GW, ISP])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=now - 600 + i, success=True, latency_ms=2.0)
                               for t in (GW, ISP) for i in range(120)])
    app_state.repository = repo
    app_state.config = SimpleNamespace(targets=[GW, ISP], dns_probes=[])
    app_state.settings = SimpleNamespace(revision=1)
    yield app_state
    await repo.close()


async def _spike(repo, ago, stage="isp", expected=False):
    now = time.time()
    await repo.create_latency_spike(LatencySpike(start_time=now - ago, end_time=now - ago + 20, kind="upstream_latency",
                                                 stage=stage, expected=expected, peak_ms=150.0))


async def test_the_route_answers_the_window_by_day_and_stage(served):
    repo = served.repository
    for ago, stage, expected in ((300, "isp", False), (1.5 * DAY, "router", False), (2 * 3600, "isp", False),
                                 (3600, "load", True), (10 * DAY, "isp", False)):
        await _spike(repo, ago, stage, expected)
    async with _client() as ac:
        week = (await ac.get("/api/latency-spikes?days=7&tz=America/New_York")).json()
        hours = (await ac.get("/api/latency-spikes?hours=3")).json()
        both = (await ac.get("/api/latency-spikes?hours=3&days=2")).json()
        bad = await ac.get("/api/latency-spikes?days=0")
    assert (week["count"], week["expected"], week["incident_after_seconds"]) == (3, 1, 120.0)
    assert week["by_stage"] == [{"stage": "isp", "count": 2}, {"stage": "router", "count": 1}]
    assert week["window_end"] - week["window_start"] == 7 * DAY
    days = week["days"]
    assert sum(d["count"] for d in days) == 3 and len(days) in (7, 8)
    assert days[0]["partial"] and days[-1]["partial"] and not any(d["partial"] for d in days[1:-1])
    assert days[0]["start"] == week["window_start"] and days[-1]["end"] == week["window_end"]
    assert (hours["count"], hours["window_end"] - hours["window_start"]) == (2, 3 * 3600)
    assert both["window_end"] - both["window_start"] == 2 * DAY and both["count"] == 3
    assert bad.status_code == 422


async def test_a_new_spike_shows_at_once_although_the_answer_is_kept(served):
    async with _client() as ac:
        assert (await ac.get("/api/latency-spikes?days=1")).json()["count"] == 0
        await _spike(served.repository, 60)
        assert (await ac.get("/api/latency-spikes?days=1")).json()["count"] == 1


async def test_the_report_counts_spikes_per_day_of_its_days(served):
    await _spike(served.repository, 300)
    await _spike(served.repository, 200, "router")
    await _spike(served.repository, 100, "load", expected=True)
    async with _client() as ac:
        report = (await ac.get("/api/report?days=1")).json()
    availability, spikes = report["availability"], report["latency_spikes"]
    assert len(availability["latency_spikes"]) == len(availability["days"])
    assert sum(availability["latency_spikes"]) == spikes["count"] == 2 and spikes["expected"] == 1
    assert spikes["by_stage"] == [{"stage": "router", "count": 1}, {"stage": "isp", "count": 1}]
    assert report["incidents"]["count"] == 0  # spikes are not incidents


async def test_the_mcp_incidents_tool_gives_the_counts_per_day_and_by_stage(mcp_state):
    from pengumon.mcp.tools import TOOLS
    tool = next(t for t in TOOLS if t.name == "incidents")
    before = await tool.handler(mcp_state, {"period": "7d"})
    await _spike(mcp_state.repository, 3600)
    await _spike(mcp_state.repository, 2 * DAY, "router")
    out = await tool.handler(mcp_state, {"period": "7d"})
    spikes = out["latency_spikes"]
    # a tie in path order: the router first
    assert spikes["count"] == 2 and spikes["by_stage"] == [{"stage": "router", "count": 1}, {"stage": "isp", "count": 1}]
    assert sum(d["count"] for d in spikes["per_day"]) == 2 and spikes["per_day"][0]["partial"]
    assert "not listed as an incident" in spikes["note"] and spikes["incident_after_seconds"] == 120.0
    assert out["total_in_period"] == before["total_in_period"]  # no incident was added
    assert "latency_spikes" in tool.description
