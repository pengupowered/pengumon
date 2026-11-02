"""Schema v6: incidents get a kind, backfilled once from their titles (see db/migrations.py)."""

import sqlite3

import pytest

from pengumon.db import migrations
from pengumon.incidents.taxonomy import ISP_LINE_KINDS, KINDS, LOAD_EXPLAINABLE_KINDS, current_cause
from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.model import Incident

LOCAL, GATEWAY, ISP = "local_wifi_mesh_drop", "double_nat_congestion", "xfinity_last_mile_coax_drop"
HOP1, HOP2 = "Hop 1: Local Router (LAN)", "Hop 2: ISP Modem / Gateway"

# (title, cause_code, expected kind): the 17 titles the classifier writes today, with realistic names ...
CURRENT = [
    (f"{HOP1} Disconnect", LOCAL, "local_down"),
    (f"{HOP1} Packet Loss", LOCAL, "local_loss"),
    (f"{HOP1} Latency Spike", LOCAL, "local_latency"),
    (f"{HOP1} to {HOP2} Link Failure", GATEWAY, "gateway_down"),
    (f"{HOP2} Packet Loss", GATEWAY, "gateway_loss"),
    (f"{HOP2} Latency Delay", GATEWAY, "gateway_latency"),
    ("ISP Line / Hop 3: ISP Regional Hub Outage", ISP, "isp_outage"),
    ("ISP Line / Hop 3: ISP Regional Hub Packet Loss", ISP, "isp_loss"),
    ("ISP Upstream Backbone Packet Loss", ISP, "backbone_loss"),
    ("ISP Latency Spikes / Jitter", ISP, "upstream_latency"),
    ("DNS Resolution Outage", "dns_resolver_failure", "dns_outage"),
    ("DNS Query Packet Drops", "dns_resolver_failure", "dns_loss"),
    ("Slow DNS Queries", "dns_resolver_failure", "dns_slow"),
    ("Line Busy: Delay From Heavy Load", "load_induced_latency", "busy_latency"),
    ("Line Busy: ISP Hops Deprioritise Ping", "load_induced_latency", "busy_isp_hops"),
    ("ISP Hop Deprioritises Ping", "nominal", "hop_deprioritised"),
    ("All Network Hops Operational", "nominal", "nominal"),
]
# ... and every older shape found in the real backups.
OLDER = [
    ("ISP Local Node / Coax Packet Loss", ISP, "legacy_other"),
    ("ISP ISP Upstream Packet Loss", ISP, "legacy_other"),
    ("ISP Coax / ISP Last-Mile Outage", ISP, "legacy_other"),
    ("Virtual Network Adapter Latency Baseline (Pre-WSL Optimization)", LOCAL, "legacy_latency"),
    ("ISP Upstream Peering Packet Loss", ISP, "legacy_other"),
    ("ISP ISP Latency Spikes / Jitter", ISP, "legacy_latency"),
    ("ISP Coax / CMTS Hub Outage", ISP, "legacy_other"),
    ("Local Mesh Latency Spike", LOCAL, "local_latency"),
    ("ISP Coax / Regional CMTS Packet Loss", ISP, "legacy_other"),
    ("ISP ISP Upstream Packet Loss / Jitter", ISP, "legacy_latency"),
    ("Transient Upstream Transit Jitter", ISP, "legacy_latency"),
    ("ISP ISP Line Stabilization & Buffer Drain", ISP, "legacy_other"),
    (f"{HOP1} Packet Loss", LOCAL, "local_loss"),
    (f"{HOP2} Packet Loss", GATEWAY, "gateway_loss"),
]


def _old_load_explainable(title):
    """Frozen copy of the title rule engine.load used before incidents carried a kind."""
    return any(word in title for word in ("Latency", "Jitter", "Delay")) or (
        title.startswith("ISP Line /") and "Packet Loss" in title)


async def _v5_database(path, monkeypatch, rows):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:5])
    assert await migrations.migrate(path) == 5
    monkeypatch.undo()
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO incidents (start_time, cause_code, title, description, severity, affected_targets, resolved) "
        "VALUES (?, ?, ?, 'd', 'warning', '[]', 1)",
        [(1000.0 + i, cause, title) for i, (title, cause, _kind) in enumerate(rows)])
    con.commit()
    con.close()


def _rows(path, sql="SELECT title, cause_code, kind FROM incidents ORDER BY id"):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.mark.asyncio
async def test_backfill_maps_every_known_title_shape(tmp_path, monkeypatch):
    path = str(tmp_path / "v5.db")
    rows = CURRENT + OLDER
    await _v5_database(path, monkeypatch, rows)
    assert "kind" not in {r[1] for r in _rows(path, "PRAGMA table_info(incidents)")}

    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    stored = _rows(path)
    # schema v8 renames the ISP cause code; titles and kinds are untouched
    assert [(t, c, k) for t, c, k in stored] == [(t, current_cause(c), k) for t, c, k in rows]


@pytest.mark.asyncio
async def test_kind_sets_agree_with_the_old_title_predicates(tmp_path, monkeypatch):
    path = str(tmp_path / "v5.db")
    await _v5_database(path, monkeypatch, CURRENT + OLDER)
    await migrations.migrate(path)

    # The old backfill query, run in SQL on the same database
    like = {r[0] for r in _rows(path, "SELECT id FROM incidents WHERE title LIKE 'ISP Line / %' "
                                      "AND (title LIKE '% Packet Loss' OR title LIKE '% Outage')")}
    for row_id, title, kind in _rows(path, "SELECT id, title, kind FROM incidents"):
        assert (kind in LOAD_EXPLAINABLE_KINDS) == _old_load_explainable(title), title
        assert (kind in ISP_LINE_KINDS) == (row_id in like), title


@pytest.mark.asyncio
async def test_every_produced_kind_is_a_known_kind(tmp_path, monkeypatch):
    path = str(tmp_path / "v5.db")
    await _v5_database(path, monkeypatch, CURRENT + OLDER)
    await migrations.migrate(path)
    kinds = {r[0] for r in _rows(path, "SELECT DISTINCT kind FROM incidents")}
    assert kinds <= set(KINDS)
    assert {"legacy_latency", "legacy_other"} <= kinds


@pytest.mark.asyncio
async def test_second_migrate_is_a_no_op_and_existing_kinds_are_kept(tmp_path, monkeypatch):
    path = str(tmp_path / "v5.db")
    await _v5_database(path, monkeypatch, CURRENT[:3])
    # a kind that is already there is not rewritten: add the column by hand and label one row differently
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE incidents ADD COLUMN kind TEXT")
    con.execute("UPDATE incidents SET kind = 'isp_loss' WHERE id = 1")
    con.commit()
    con.close()

    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    first = _rows(path)
    assert [k for _t, _c, k in first] == ["isp_loss", "local_loss", "local_latency"]
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    assert _rows(path) == first


@pytest.mark.asyncio
async def test_kind_round_trips_through_the_repository_and_the_api(tmp_path, app_state):
    from httpx import ASGITransport, AsyncClient
    from pengumon.main import app

    repo = DatabaseRepository(db_path=str(tmp_path / "rt.db"))
    await repo.initialize()
    new_id = await repo.create_incident(Incident(
        start_time=1000.0, cause_code=ISP, title="ISP Line / Hub Packet Loss", description="d",
        severity="warning", kind="isp_loss"))
    (incident,) = await repo.get_recent_incidents()
    assert incident.id == new_id and incident.kind == "isp_loss"
    assert (await repo.get_incident_by_id(new_id)).kind == "isp_loss"

    app_state.repository = repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/incidents")
    assert res.status_code == 200
    assert res.json()[0]["kind"] == "isp_loss"
    await repo.close()


@pytest.mark.asyncio
async def test_an_unknown_kind_does_not_break_reading(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "unknown.db"))
    await repo.initialize()
    await repo.create_incident(Incident(
        start_time=1.0, cause_code="nominal", title="t", description="d", severity="info", kind="from_the_future"))
    assert (await repo.get_recent_incidents())[0].kind == "from_the_future"
    await repo.close()


@pytest.mark.asyncio
async def test_start_backfills_incidents_an_older_build_wrote_without_a_kind(tmp_path):
    path = str(tmp_path / "old-build.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    await repo.close()
    # an older build, still pointed at the migrated database, inserts without the kind column
    con = sqlite3.connect(path)
    con.execute("INSERT INTO incidents (start_time, cause_code, title, description, severity, affected_targets, resolved) "
                "VALUES (1.0, ?, ?, 'd', 'warning', '[]', 1)", (ISP, "ISP Line / Hop 3: Hub Packet Loss"))
    con.execute("INSERT INTO incidents (start_time, cause_code, title, description, severity, affected_targets, resolved, kind) "
                "VALUES (2.0, ?, 'Slow DNS Queries', 'd', 'warning', '[]', 1, 'isp_outage')", ("dns_resolver_failure",))
    con.commit()
    con.close()
    assert _rows(path, "SELECT kind FROM incidents ORDER BY id") == [(None,), ("isp_outage",)]

    repo = DatabaseRepository(db_path=path)
    version = repo.incidents_version
    await repo.initialize()
    assert [k for (k,) in _rows(path, "SELECT kind FROM incidents ORDER BY id")] == ["isp_loss", "isp_outage"]  # kept
    assert repo.incidents_version > version
    await repo.close()

    repo = DatabaseRepository(db_path=path)  # nothing left to do: idempotent, cache version untouched
    version = repo.incidents_version
    await repo.initialize()
    assert repo.incidents_version == version
    await repo.close()


@pytest.mark.asyncio
async def test_update_incident_classification_is_stored_and_refreshes_the_cache(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "classify.db"))
    await repo.initialize()
    new_id = await repo.create_incident(Incident(
        start_time=1.0, cause_code=LOCAL, title="Router Packet Loss", description="loss", severity="warning",
        kind="local_loss"))
    assert (await repo.get_recent_incidents())[0].kind == "local_loss"  # primes the cache
    await repo.update_incident_classification(new_id, "local_down", LOCAL, "Router Disconnect", "gone", "critical")
    stored = await repo.get_incident_by_id(new_id)
    assert (stored.kind, stored.cause_code, stored.title, stored.description, stored.severity) == (
        "local_down", LOCAL, "Router Disconnect", "gone", "critical")
    assert (await repo.get_recent_incidents())[0].kind == "local_down"  # the cache was invalidated
    await repo.close()


def test_the_legacy_isp_cause_code_is_read_as_access_line_drop():
    """Schema v8 renames stored rows; a row an older build writes afterwards still reads as the new code."""
    from pengumon.incidents.model import Incident, SystemDiagnosis
    incident = Incident(start_time=1.0, cause_code="xfinity_last_mile_coax_drop", title="t", description="d",
                        severity="warning")
    assert incident.cause_code == "access_line_drop"
    assert SystemDiagnosis(cause_code="xfinity_last_mile_coax_drop").cause_code == "access_line_drop"
    assert current_cause("dns_resolver_failure") == "dns_resolver_failure"
