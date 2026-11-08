"""Schema v16: the names stored with British spelling take the US one (incident kind and titles, meta markers, the
dashboard's card colors setting and its saved versions)."""

import json
import sqlite3

from pengumon.db import migrations
from pengumon.db.settings import SETTINGS_INITIALIZED_KEY
from pengumon.db.path import PATH_INITIALIZED_KEY
from pengumon.engine.load import PING_BACKFILL_META_KEY
from pengumon.incidents.taxonomy import KINDS


async def _v15_database(path, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:15])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 15)
    assert await migrations.migrate(path) == 15
    monkeypatch.undo()
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO incidents (start_time, cause_code, title, description, severity, affected_targets, resolved, kind) "
        "VALUES (?, ?, ?, 'd', 'warning', '[]', 1, ?)",
        [(1000.0, "nominal", "ISP Hop Deprioritises Ping", "hop_deprioritised"),
         (1001.0, "load_induced_latency", "Line Busy: ISP Hops Deprioritise Ping", "busy_isp_hops"),
         (1002.0, "dns_resolver_failure", "Slow DNS Queries", "dns_slow")])
    con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                    [("settings_initialised", "1"), ("path_initialised", "1"),
                     ("incident_ping_deprioritisation_backfill", '{"checked": 4}')])
    colors = {"latency": {"theme": "nord"}}
    con.execute("INSERT INTO settings_values (key, value, updated) VALUES (?, ?, 1.0), ('line.up_mbps', '40', 1.0)",
                ("dashboard.colours", json.dumps(colors)))
    con.executemany("INSERT INTO settings_versions (revision, applied_at, source, summary, document) VALUES (?, 1.0, 'page', 's', ?)",
                    [(1, json.dumps({"values": {"line.up_mbps": 40}, "lists": {}})),
                     (2, json.dumps({"values": {"line.up_mbps": 40, "dashboard.colours": colors}, "lists": {}}))])
    con.commit()
    con.close()
    return colors


def _query(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


async def test_v16_gives_the_stored_names_their_us_spelling_and_keeps_their_values(tmp_path, monkeypatch):
    path = str(tmp_path / "v15.db")
    colors = await _v15_database(path, monkeypatch)
    assert await migrations.migrate(path) == migrations.LATEST_VERSION == 17
    assert _query(path, "SELECT title, kind FROM incidents ORDER BY id") == [
        ("ISP Hop Deprioritizes Ping", "hop_deprioritized"), ("Line Busy: ISP Hops Deprioritize Ping", "busy_isp_hops"),
        ("Slow DNS Queries", "dns_slow")]
    assert {k for k, in _query(path, "SELECT kind FROM incidents")} <= set(KINDS)
    assert dict(_query(path, "SELECT key, value FROM meta WHERE key NOT LIKE '%revision%'")) == {
        SETTINGS_INITIALIZED_KEY: "1", PATH_INITIALIZED_KEY: "1", PING_BACKFILL_META_KEY: '{"checked": 4}'}
    assert dict(_query(path, "SELECT key, value FROM settings_values")) == {
        "dashboard.colors": json.dumps(colors), "line.up_mbps": "40"}
    documents = [json.loads(d) for d, in _query(path, "SELECT document FROM settings_versions ORDER BY revision")]
    assert documents[0] == {"values": {"line.up_mbps": 40}, "lists": {}}  # nothing to rename: left as it was
    assert documents[1]["values"] == {"line.up_mbps": 40, "dashboard.colors": colors}
    assert await migrations.migrate(path) == 17  # runs once


async def test_v16_keeps_a_value_already_under_the_new_name(tmp_path, monkeypatch):
    path = str(tmp_path / "both.db")
    await _v15_database(path, monkeypatch)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta (key, value) VALUES ('settings_initialized', 'new')")
    con.commit()
    con.close()
    await migrations.migrate(path)
    assert _query(path, "SELECT key, value FROM meta WHERE key LIKE 'settings_initiali%'") == [
        ("settings_initialized", "new")]
