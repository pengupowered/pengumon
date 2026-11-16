"""Retention by kind of data (0.28 step 5): each kind is pruned by its own setting and only its tables, the settings are
validated as the plan says, an earlier install's stored key becomes the new one, and GET /api/storage tells what each kind
holds on disk (admin only)."""

import os
import sqlite3
import time

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import security
from pengumon.capabilities import TrafficApps, TrafficHour
from pengumon.config import AppConfig, DatabaseConfig
from pengumon.db import migrations, storage_stats
from pengumon.db.core import KIND_SETTINGS, RETENTION_SETTINGS, SPEED_TEST_MIN_DAYS
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import DeviceSample, LineSample, MeshCount
from pengumon.incidents.model import Incident
from pengumon.main import app, security_config
from pengumon.models.probes import ProbeSample
from pengumon.settings import registry
from pengumon.settings.importer import read_settings

DAY = 86400
FOREVER = 100000  # days: a kind that must not lose anything in a test of another kind
TOKEN = "example-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
READ_TOKEN = "pgr_made-up-read-token-for-storage"
READER = {"Authorization": f"Bearer {READ_TOKEN}"}


def _has_dbstat() -> bool:
    """Whether this Python's SQLite has the dbstat table (Linux builds do; the Windows and macOS builds of python.org
    do not, and the storage answer is then an estimate that says so)."""
    try:
        sqlite3.connect(":memory:").execute("SELECT 1 FROM dbstat LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


HAS_DBSTAT = _has_dbstat()


# --- what each kind is pruned by --------------------------------------------------------------------------------------

# kind setting -> the tables that lose their old rows when only that setting is short
TABLES_OF = {
    "latency_retention_days": {"rollup_15m"},
    "router_retention_days": {"device_rollup_15m"},
    "mesh_retention_days": {"mesh_rollup_15m"},
    "line_retention_days": {"line_samples"},
    "traffic_retention_days": {"traffic_hourly", "traffic_apps"},
    "speedtest_retention_days": {"speed_tests", "device_events"},
    "incident_retention_days": {"incidents", "incident_evidence"},
}


def _day_of(window_end):
    return None


async def _seed(repo, age_days):
    """One row of every table that has a retention, `age_days` old (the incident resolved, with its evidence)."""
    ts = time.time() - age_days * DAY
    await repo.insert_samples([ProbeSample(target_id="a", timestamp=ts + i, success=True, latency_ms=1.0) for i in range(5)])
    await repo.insert_device_sample(DeviceSample("r1", ts, "healthy", cpu_pct=1.0))
    await repo.insert_mesh_counts([MeshCount("r1", ts, "n1", 2)])
    await repo.insert_line_sample(LineSample(device_id="m", ts=ts, technology="docsis", status="healthy",
                                             detail='{"v":1,"ch":[]}'))
    await repo.upsert_traffic_hours("r1", [TrafficHour("", int(ts // 3600) * 3600, 1, 2)], ts)
    await repo.store_traffic_apps("r1", [TrafficApps("", int(ts), (("Web", 1, 1),))], ts, _day_of)
    await repo.insert_speed_test({"ts": ts, "trigger": "scheduled", "provider": "cloudflare", "status": "ok",
                                  "duration_s": 12.0})
    await repo.insert_device_event("r1", ts, "restarted", {})
    incident = Incident(start_time=ts, end_time=ts + 60, duration_seconds=60.0, cause_code="unknown_outage", title="t",
                        description="d", severity="warning", resolved=True)
    incident.id = await repo.create_incident(incident)
    await repo.write_incident_evidence(incident.id)
    return incident


def _counts(repo):
    with sqlite3.connect(repo.db_path) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


@pytest.mark.parametrize("setting", sorted(TABLES_OF))
async def test_each_kind_prunes_only_its_tables_at_its_own_setting(repo, setting):
    await _seed(repo, 200)
    before = _counts(repo)
    assert all(before[t] > 0 for tables in TABLES_OF.values() for t in tables), before  # every table has a row
    policy = {name: FOREVER for name in RETENTION_SETTINGS}
    policy[setting] = 100  # only this one is shorter than the rows' age
    removed = await repo.cleanup_old_samples(**policy)
    after = _counts(repo)
    fixed_31_days = {"traffic_app_hours"}  # the hourly app lists go at 31 days whatever the kinds say (full detail)
    assert {t for t, n in removed.items() if n} - fixed_31_days == TABLES_OF[setting]
    evidence_children = {"incident_evidence_probes", "incident_evidence_readings"}  # go with their header
    changed = {t for t in after if after[t] != before[t]} - fixed_31_days
    assert TABLES_OF[setting] <= changed <= TABLES_OF[setting] | evidence_children
    assert changed - evidence_children == TABLES_OF[setting]
    assert all(after[t] == 0 for t in TABLES_OF[setting])


async def test_the_full_detail_settings_prune_the_fixed_tiers_whatever_the_kinds_say(repo):
    await _seed(repo, 200)
    policy = {name: FOREVER for name in KIND_SETTINGS}
    removed = await repo.cleanup_old_samples(retention_days=7, slot_retention_days=7, rollup_retention_days=30, **policy)
    assert {t for t, n in removed.items() if n} == {"samples", "rollup_10s", "rollup_1m", "device_samples",
                                                    "device_rollup_1m", "mesh_samples", "traffic_app_hours"}


async def test_the_kinds_default_to_180_days(repo):
    config = DatabaseConfig()
    assert [getattr(config, name) for name in KIND_SETTINGS] == [180] * len(KIND_SETTINGS)
    assert [getattr(repo, name) for name in KIND_SETTINGS] == [180] * len(KIND_SETTINGS)
    assert (config.retention_days, config.slot_retention_days, config.rollup_retention_days) == (7, 7, 30)
    await _seed(repo, 170)
    kept = _counts(repo)
    removed = await repo.cleanup_old_samples()
    assert not {t for t in TABLES_OF["latency_retention_days"] | TABLES_OF["incident_retention_days"] if removed[t]}
    assert _counts(repo)["rollup_15m"] == kept["rollup_15m"] and _counts(repo)["incidents"] == 1
    await _seed(repo, 190)  # past 180: every kind loses it, on a second run
    removed = await repo.cleanup_old_samples()
    assert all(removed[t] > 0 for tables in TABLES_OF.values() for t in tables)


async def test_speed_tests_are_never_kept_less_than_the_monthly_cap_needs(repo):
    ts = time.time() - 33 * DAY
    await repo.insert_speed_test({"ts": ts, "trigger": "scheduled", "provider": "cloudflare", "status": "ok"})
    await repo.insert_speed_test({"ts": time.time() - 31 * DAY, "trigger": "scheduled", "provider": "cloudflare",
                                  "status": "ok"})
    removed = await repo.cleanup_old_samples(speedtest_retention_days=1)
    assert SPEED_TEST_MIN_DAYS == 32 and removed["speed_tests"] == 1


async def test_incidents_zero_keeps_them_forever_and_n_deletes_the_resolved_ones_with_their_evidence(repo):
    old = await _seed(repo, 500)
    start = time.time() - 500 * DAY
    open_one = await repo.create_incident(Incident(start_time=start, cause_code="unknown_outage", title="open",
                                                   description="d", severity="warning", resolved=False))
    repo.incident_retention_days = 0
    removed = await repo.cleanup_old_samples(**{n: FOREVER for n in RETENTION_SETTINGS if n != "incident_retention_days"})
    assert "incidents" not in removed and removed["incident_evidence"] == 0
    assert _counts(repo)["incidents"] == 2 and _counts(repo)["incident_evidence"] == 1
    repo.incident_retention_days = 400
    removed = await repo.cleanup_old_samples(**{n: FOREVER for n in RETENTION_SETTINGS if n != "incident_retention_days"})
    assert removed["incidents"] == 1 and removed["incident_evidence"] > 0
    assert _counts(repo)["incidents"] == 1 and _counts(repo)["incident_evidence"] == 0
    assert [i.id for i in await repo.get_recent_incidents()] == [open_one] and old.id != open_one


async def test_an_unknown_retention_name_is_refused(repo):
    with pytest.raises(TypeError, match="long_rollup_retention_days"):
        await repo.cleanup_old_samples(long_rollup_retention_days=10)


async def test_the_retention_log_line_names_the_kinds(repo, caplog):
    caplog.set_level("INFO")
    await repo.cleanup_old_samples()
    text = " ".join(r.getMessage() for r in caplog.records)
    for name in ("latency", "router", "mesh", "line", "traffic", "speedtest", "incident"):
        assert f"{name} 180" in text


# --- the settings ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [n for n in KIND_SETTINGS if n != "incident_retention_days"])
def test_every_kind_is_at_least_30_days(name):
    assert getattr(DatabaseConfig(**{name: 30}), name) == 30
    for bad in (0, 1, 29):
        with pytest.raises(ValueError):
            DatabaseConfig(**{name: bad})


def test_incidents_are_zero_or_at_least_30_days():
    assert DatabaseConfig(incident_retention_days=0).incident_retention_days == 0
    assert DatabaseConfig(incident_retention_days=30).incident_retention_days == 30
    for bad in (1, 29):
        with pytest.raises(ValueError, match="incident_retention_days must be 0"):
            DatabaseConfig(incident_retention_days=bad)


def test_raw_is_at_least_a_day_and_the_tiers_are_ordered_raw_slot_minute():
    assert DatabaseConfig(retention_days=1).retention_days == 1
    for bad in (0, 91):
        with pytest.raises(ValueError):
            DatabaseConfig(retention_days=bad)
    with pytest.raises(ValueError, match="slot_retention_days must be at least retention_days"):
        DatabaseConfig(retention_days=14, slot_retention_days=10)
    with pytest.raises(ValueError, match="rollup_retention_days must be at least slot_retention_days"):
        DatabaseConfig(slot_retention_days=40, rollup_retention_days=30)
    # no tie between a kind and the tiers any more: a kind may be shorter than the minute tier
    assert DatabaseConfig(rollup_retention_days=400, latency_retention_days=30, incident_retention_days=30)


def test_the_retention_section_has_the_seven_kinds_and_the_fixed_tiers_are_advanced():
    retention = [s.key for s in registry.SETTINGS if s.section == "retention"]
    assert retention == [f"database.{n}" for n in KIND_SETTINGS if n != "incident_retention_days"] + [
        "database.incident_retention_days"]
    assert all(registry.get(k).unit == "days" and "hourly cleanup" in registry.get(k).effect for k in retention)
    for name in ("retention_days", "slot_retention_days", "rollup_retention_days"):
        setting = registry.get(f"database.{name}")
        assert setting.section == "advanced" and setting.advanced and setting.unit == "days"
    assert "7 days" in registry.get("database.retention_days").effect and "evidence" in registry.get("database.retention_days").effect
    assert registry.get("database.incident_retention_days").label == "Delete incidents older than"
    assert registry.get("database.rollup_retention_days").default() == 30
    assert registry.get("database.rollup_retention_days").constraints()["min"] == 30
    assert registry.get("database.retention_days").constraints()["min"] == 1
    assert "database.long_rollup_retention_days" not in registry.BY_KEY


# --- the earlier key -----------------------------------------------------------------------------------------------------

def test_config_yaml_still_accepts_the_old_key_and_reads_it_as_the_new_one():
    config = AppConfig(database={"long_rollup_retention_days": 250})
    assert config.database.latency_retention_days == 250
    assert AppConfig(database={"long_rollup_retention_days": 250, "latency_retention_days": 90}
                     ).database.latency_retention_days == 90  # the new key wins


def test_settings_import_of_an_old_yaml_maps_the_old_key():
    result = read_settings({"database": {"long_rollup_retention_days": 250, "traffic_retention_days": 400}})
    assert result.document.values == {"database.latency_retention_days": 250, "database.traffic_retention_days": 400}
    assert any("long_rollup_retention_days" in n for n in result.notes)
    both = read_settings({"database": {"long_rollup_retention_days": 250, "latency_retention_days": 90}})
    assert both.document.values == {"database.latency_retention_days": 90}


async def _v22_with(tmp_path, monkeypatch, stored, name="v22.db"):
    path = str(tmp_path / name)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:22])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 22)
    assert await migrations.migrate(path) == 22
    monkeypatch.undo()
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO settings_values (key, value, updated) VALUES (?, ?, 1)", list(stored.items()))
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('settings_revision', '3')")
    return path


async def test_the_store_renames_the_old_key_once_keeping_the_value(tmp_path, monkeypatch):
    path = await _v22_with(tmp_path, monkeypatch, {"database.long_rollup_retention_days": "250",
                                                   "database.rollup_retention_days": "400"})
    assert await migrations.migrate(path) == migrations.LATEST_VERSION >= 23
    with sqlite3.connect(path) as db:
        values = dict(db.execute("SELECT key, value FROM settings_values"))
        version = db.execute("SELECT revision, source FROM settings_versions ORDER BY revision DESC").fetchone()
    assert values == {"database.latency_retention_days": "250", "database.rollup_retention_days": "400"}
    assert version == (4, "upgrade")  # one version of its own; the minute tier's 400 is not touched
    assert await migrations.migrate(path) == migrations.LATEST_VERSION  # nothing more to do


async def test_the_new_key_already_stored_wins_and_unset_ones_stay_unset(tmp_path, monkeypatch):
    path = await _v22_with(tmp_path, monkeypatch, {"database.long_rollup_retention_days": "250",
                                                   "database.latency_retention_days": "90"})
    await migrations.migrate(path)
    with sqlite3.connect(path) as db:
        assert dict(db.execute("SELECT key, value FROM settings_values")) == {"database.latency_retention_days": "90"}
    none = await _v22_with(tmp_path, monkeypatch, {}, "none.db")
    await migrations.migrate(none)
    with sqlite3.connect(none) as db:
        assert db.execute("SELECT COUNT(*) FROM settings_values").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM settings_versions WHERE source = 'upgrade'").fetchone() == (0,)


# --- GET /api/storage --------------------------------------------------------------------------------------------------

def test_every_table_of_a_fresh_database_has_a_kind(tmp_path):
    path = str(tmp_path / "fresh.db")
    import asyncio
    assert asyncio.run(migrations.migrate(path)) == migrations.LATEST_VERSION
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables - set(storage_stats.KIND_OF_TABLE) == set(), "a new table needs a kind in db/storage_stats.py"
    assert set(storage_stats.KIND_OF_TABLE.values()) <= {k[0] for k in storage_stats.KINDS}
    assert not set(storage_stats.TIME_COLUMN) - set(storage_stats.KIND_OF_TABLE)


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)
    monkeypatch.setattr(security_config, "read_token_hash", "")
    security.reset_rate_limits()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _filled(repo):
    """Router readings every 10 minutes over 3 days: enough to measure."""
    now = time.time()
    for i in range(3 * 144):
        await repo.insert_device_sample(DeviceSample("r1", now - 3 * DAY + i * 600, "healthy", cpu_pct=1.0, client_count=3))


@pytest.mark.skipif(not HAS_DBSTAT, reason="this SQLite has no dbstat: the sizes are estimates (the next test)")
async def test_the_storage_answer_has_every_kind_and_adds_up_to_the_file(repo, app_state):
    app_state.repository = repo
    await _filled(repo)
    with sqlite3.connect(repo.db_path) as db:  # the file holds every page only after a checkpoint (WAL mode)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    async with _client() as ac:
        res = await ac.get("/api/storage")
    body = res.json()
    assert res.status_code == 200 and body["estimate"] is False
    kinds = {k["id"]: k for k in body["kinds"]}
    assert list(kinds) == ["full", "latency", "router", "mesh", "line", "traffic", "speedtest", "incident", "other"]
    assert kinds["full"]["setting"] is None and kinds["router"]["setting"] == "database.router_retention_days"
    assert kinds["router"]["tiered"] and not kinds["line"]["tiered"] and kinds["incident"]["setting"].endswith("incident_retention_days")
    for kind in kinds.values():
        assert set(kind) == {"id", "label", "setting", "tables", "bytes", "days_stored", "per_day", "fixed", "tiered",
                             "estimate"}
    assert kinds["router"]["tables"] == ["device_rollup_15m"] and "samples" in kinds["full"]["tables"]
    assert 2.5 < kinds["router"]["days_stored"] < 3.5 and kinds["router"]["per_day"] > 0
    assert kinds["router"]["fixed"] == pytest.approx(kinds["router"]["per_day"] * 30, abs=30)
    # the kinds hold every page of the tables and their indexes: what the file has more is free pages and bookkeeping
    # (the pointer map of an incremental-vacuum file, the header page)
    assert body["total_bytes"] == sum(k["bytes"] for k in body["kinds"])
    size = os.path.getsize(repo.db_path)
    assert body["file_bytes"] == size
    assert 0 <= size - body["total_bytes"] - body["free_bytes"] <= max(0.02 * size, 64 * 1024)
    assert body["wal_bytes"] >= 0 and body["disk"]["free"] > 0 and body["disk"]["total"] >= body["disk"]["used"]
    assert body["fixed_days"] == 30


async def test_without_dbstat_each_table_is_rows_times_a_sampled_row_and_says_estimate(repo, app_state, monkeypatch):
    app_state.repository = repo
    await _filled(repo)
    real = await storage_stats.storage_answer(repo, force_estimate=False)
    flagged = await storage_stats.storage_answer(repo, force_estimate=True)
    assert real["estimate"] is (not HAS_DBSTAT) and flagged["estimate"] is True
    assert all(k["estimate"] for k in flagged["kinds"] if k["tables"])
    by_kind = {k["id"]: k for k in flagged["kinds"]}
    assert 2.5 < by_kind["router"]["days_stored"] < 3.5 and by_kind["router"]["bytes"] > 0
    if HAS_DBSTAT:  # the estimate is in the neighbourhood of the measured size (it leaves the indexes out)
        measured = {k["id"]: k for k in real["kinds"]}
        assert 0.3 < by_kind["full"]["bytes"] / measured["full"]["bytes"] < 1.5
    # and the build without dbstat takes the same path by itself
    async def none(db):
        return False
    monkeypatch.setattr(storage_stats, "_has_dbstat", none)
    repo._storage_cache = None
    assert (await storage_stats.storage_answer(repo))["estimate"] is True


@pytest.mark.skipif(not HAS_DBSTAT, reason="this SQLite has no dbstat to hold the estimate against")
async def test_the_estimate_without_dbstat_is_close_to_dbstat_on_probe_history(repo):
    """Probe rows as stored since schema v25 (integer hundredths, some failures with an error, a few without jitter):
    the estimate of each large table, and of all of them, is near what dbstat measures. (Counting each value by its
    digits overstated the samples by about half.)"""
    import random
    rng = random.Random(7)
    now = time.time()
    con = sqlite3.connect(repo.db_path)
    con.executemany("INSERT INTO targets (id, key) VALUES (?, ?)", [(i, f"t{i}") for i in range(1, 15)])
    rows = []
    for r in range(15_000):  # 14 targets a round, a round a second, each probe a little after the round starts
        for t in range(1, 15):
            ok = rng.random() > 0.002
            rows.append((now - 15_000 + r + t * 0.0137 + rng.random() * 0.01, t, 1 if ok else 0,
                         int(rng.uniform(40, 3500)) if ok else None, int(rng.uniform(5, 900)) if ok and r % 50 else None,
                         None if ok else "timeout"))
    con.executemany("INSERT INTO samples (ts, target, ok, latency_cms, jitter_cms, error) VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute(
        "INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n) SELECT "
        "CAST(ts / 60 AS INTEGER), target, COUNT(*), SUM(ok), SUM(latency_cms) / 100.0, MIN(latency_cms) / 100.0, "
        "MAX(latency_cms) / 100.0, SUM(jitter_cms) / 100.0, COUNT(jitter_cms) FROM samples GROUP BY 1, 2")
    con.commit()
    con.close()
    async with aiosqlite.connect(repo.db_path) as db:
        real = {t.name: t.bytes for t in await storage_stats.table_stats(db, now)}
        estimated = {t.name: t.bytes for t in await storage_stats.table_stats(db, now, force_estimate=True)}
    for table in ("samples", "rollup_1m"):
        assert 0.85 < estimated[table] / real[table] < 1.15, (table, estimated[table], real[table])
    both = sum(real[t] for t in ("samples", "rollup_1m"))
    assert 0.9 < sum(estimated[t] for t in ("samples", "rollup_1m")) / both < 1.1


async def test_the_answer_is_kept_for_a_minute(repo, app_state):
    app_state.repository = repo
    first = await storage_stats.storage_answer(repo)
    assert await storage_stats.storage_answer(repo) is first
    repo._storage_cache = (time.time() - storage_stats.CACHE_SECONDS - 1, first)
    assert await storage_stats.storage_answer(repo) is not first


async def test_only_someone_who_may_edit_gets_the_storage(repo, app_state, monkeypatch):
    app_state.repository = repo
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "read_token_hash", security.read_token_digest(READ_TOKEN))
    monkeypatch.setattr(security_config, "view_without_token", True)
    async with _client() as ac:
        viewer = await ac.get("/api/storage")
        reader = await ac.get("/api/storage", headers=READER)
        owner = await ac.get("/api/storage", headers=BEARER)
    assert viewer.status_code == 401 and reader.status_code == 403
    assert owner.status_code == 200 and "kinds" in owner.json()
    policy = security.VIEWER_POLICY
    assert not policy.permits("GET", "/api/storage") and not policy.permits_reader("GET", "/api/storage")


# --- the readers clip to their kind --------------------------------------------------------------------------------------

async def test_the_history_helpers_say_how_far_back_each_kind_reaches(repo):
    repo.rollup_retention_days = 30
    repo.latency_retention_days, repo.router_retention_days, repo.mesh_retention_days = 90, 45, 60
    assert (repo.probe_history_days(), repo.router_history_days(), repo.mesh_history_days()) == (90, 45, 60)
    repo.router_retention_days = 30
    repo.rollup_retention_days = 400  # the minute tier raised in Advanced reaches further than the kind's number
    assert repo.router_history_days() == 400 and repo.probe_history_days() == 400
