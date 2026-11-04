"""Path history and "Replace address" (db/path.py, api/path.py): every apply is kept as a version with a one-line
summary of what changed (the last 50; revision 1 is the import), a version reads back as a draft that restores the
path, and an apply that changes a hop's address keeps its history and records an "address_changed" event."""

import sqlite3
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.config import AppConfig
from pengumon.db import migrations
from pengumon.db.migrations.v014_path_versions import BASELINE_SUMMARY
from pengumon.db.path import describe_change
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.settings import DeviceConfig
from pengumon.engine.path import PathManager
from pengumon.engine.prober import NetworkProber
from pengumon.main import app
from pengumon.models.probes import ProbeSample, TargetConfig

from icmp_fakes import FakeBackend

GW = TargetConfig(id="gw", name="Gateway", address="192.0.2.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop 1", address="198.51.100.9", category="isp")
PUB = TargetConfig(id="pub", name="Public", address="203.0.113.53", category="public")


def _hop(t: TargetConfig, position: int, **extra) -> dict:
    return {**t.model_dump(), "position": position, **extra}


async def _repo(tmp_path) -> DatabaseRepository:
    repo = DatabaseRepository(db_path=str(tmp_path / "v.db"), sample_flush_seconds=0)
    await repo.initialize()
    return repo


@pytest.fixture
async def path_app(tmp_path, app_state):
    repo = await _repo(tmp_path)
    # the device is at no hop's address, so no hop is linked to it by itself (engine.device_links)
    config = AppConfig(targets=[GW, ISP, PUB],
                       devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.0.2.254")])
    prober = NetworkProber(backend=FakeBackend())
    manager = PathManager(config, repo, prober)
    await manager.load()
    prober.set_targets(config.targets)
    app_state.config, app_state.repository, app_state.prober, app_state.path = config, repo, prober, manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, app_state


# --- the summary -------------------------------------------------------------------------------------------------------

def test_the_summary_names_what_was_added_removed_renamed_and_what_changed_role_or_address():
    new_hop = TargetConfig(id="modem", name="Modem", address="192.0.2.254", category="gateway")
    after = [GW.model_copy(update={"name": "Home gateway"}), new_hop,
             ISP.model_copy(update={"address": "198.51.100.10", "category": "gateway"})]
    text, moves = describe_change([GW, ISP, PUB], after)
    assert text == ("Added Modem; removed Public; renamed Gateway to Home gateway; Provider hop 1: address "
                    "198.51.100.9 → 198.51.100.10; Provider hop 1: role Provider → Gateway (modem)")
    assert moves == [("isp_1", "198.51.100.9", "198.51.100.10")]
    assert describe_change([GW, ISP], [ISP, GW]) == ("Order changed", [])
    assert describe_change([GW], [GW.model_copy(update={"color": "#112233"})])[0] == "Settings of 1 hop changed"
    assert describe_change([GW], [GW]) == ("No changes", [])
    # the same address written differently is not a change; a removed hop that comes back is "restored"
    v6 = TargetConfig(id="v6", name="V6", address="2001:db8::1")
    assert describe_change([v6], [v6.model_copy(update={"address": "2001:DB8:0::1"})]) == ("No changes", [])
    assert describe_change([GW], [GW, PUB], removed=[PUB])[0] == "Restored Public"


def test_a_long_summary_is_cut_to_one_line():
    many = [TargetConfig(id=f"h{i}", name=f"A hop with a long name {i}", address="192.0.2.7") for i in range(30)]
    text, _ = describe_change([], many)
    assert len(text) <= 300 and text.endswith("…")


# --- versions ----------------------------------------------------------------------------------------------------------

async def test_every_apply_is_kept_as_a_version_newest_first_and_the_import_is_revision_1(path_app):
    ac, _ = path_app
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(PUB, 1, name="Cloud")]})
    assert res.status_code == 200, res.text
    versions = (await ac.get("/api/path/versions")).json()
    assert [(v["revision"], v["hop_count"]) for v in versions] == [(2, 2), (1, 3)]
    assert versions[0]["summary"] == "Removed Provider hop 1; renamed Public to Cloud"
    assert versions[1]["summary"].startswith("Imported 3 hops")
    assert all(abs(v["applied_at"] - time.time()) < 60 for v in versions)
    one = (await ac.get("/api/path/versions/1")).json()
    assert one["revision"] == 1 and [(h["id"], h["position"]) for h in one["hops"]] == [
        ("gw", 0), ("isp_1", 1), ("pub", 2)]
    assert (await ac.get("/api/path/versions/9")).status_code == 404


async def test_restoring_a_version_is_applying_its_draft_and_a_removed_hop_gets_its_history_back(path_app):
    ac, state = path_app
    now = time.time()
    await state.repository.insert_samples([ProbeSample(target_id="isp_1", timestamp=now - 30 + k, success=True,
                                                       latency_ms=5.0) for k in range(5)])
    await state.repository.flush_samples()
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(PUB, 1)]})
    assert {r["target_id"] for r in await state.repository.get_history(now - 60, now)} == set()  # hidden while removed
    draft = (await ac.get("/api/path/versions/1")).json()["hops"]
    res = await ac.put("/api/path", json={"base_revision": 2, "hops": draft})
    assert res.status_code == 200, res.text
    assert [t.id for t in state.config.targets] == ["gw", "isp_1", "pub"]
    assert {r["target_id"] for r in await state.repository.get_history(now - 60, now)} == {"isp_1"}
    assert (await ac.get("/api/path/versions")).json()[0]["summary"] == "Restored Provider hop 1"


async def test_a_version_drops_a_device_link_that_is_no_longer_configured(path_app):
    ac, state = path_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0, device_id="router"), _hop(PUB, 1)]})
    assert (await ac.get("/api/path/versions/2")).json()["hops"][0]["device_id"] == "router"
    state.config.devices = []
    assert (await ac.get("/api/path/versions/2")).json()["hops"][0]["device_id"] is None


async def test_only_the_last_fifty_versions_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr("pengumon.db.path.MAX_VERSIONS", 3)
    repo = await _repo(tmp_path)
    await repo.import_path([GW])
    for revision in range(1, 6):
        await repo.save_path(revision, [GW.model_copy(update={"name": f"Gateway {revision}"})])
    assert [v["revision"] for v in await repo.path_versions()] == [6, 5, 4]
    assert await repo.path_version(3) is None


async def test_a_refused_apply_stores_no_version(path_app):
    ac, state = path_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0)]})
    assert (await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(PUB, 0)]})).status_code == 409
    assert [v["revision"] for v in await state.repository.path_versions()] == [2, 1]


async def test_the_migration_keeps_the_path_of_an_older_build_as_its_first_version(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    await repo.save_path(1, [GW, ISP, PUB])
    await repo.close()
    with sqlite3.connect(repo.db_path) as db:  # as the previous build left it: no versions, schema v13
        db.execute("DROP TABLE path_versions")
        db.execute("DROP TABLE path_events")
        db.execute("PRAGMA user_version=13")
    assert await migrations.migrate(repo.db_path) == migrations.LATEST_VERSION
    again = DatabaseRepository(db_path=repo.db_path)
    await again.initialize()
    assert [(v["revision"], v["hop_count"], v["summary"]) for v in await again.path_versions()] == [
        (2, 3, BASELINE_SUMMARY)]
    assert [h["id"] for h in await again.path_version(2)] == ["gw", "isp_1", "pub"]
    await again.close()


async def test_the_migration_adds_no_version_when_no_path_was_ever_applied(tmp_path):
    repo = await _repo(tmp_path)
    assert await repo.path_versions() == []


# --- replace address -----------------------------------------------------------------------------------------------------

async def test_replacing_a_hops_address_keeps_its_history_records_an_event_and_restarts_its_live_stats(path_app):
    ac, state = path_app
    now = time.time()
    await state.repository.insert_samples([ProbeSample(target_id="isp_1", timestamp=now - 30 + k, success=True,
                                                       latency_ms=5.0) for k in range(5)])
    await state.repository.flush_samples()
    state.prober.stats["isp_1"].sent = 40
    state.prober.stats["gw"].sent = 40
    hops = [_hop(GW, 0), _hop(ISP, 1, address="198.51.100.10"), _hop(PUB, 2)]
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": hops})
    assert res.status_code == 200, res.text
    assert state.prober.stats["isp_1"].sent == 0 and state.prober.stats["isp_1"].address == "198.51.100.10"
    assert state.prober.stats["gw"].sent == 40
    assert len([r for r in await state.repository.get_history(now - 60, now) if r["target_id"] == "isp_1"]) == 5
    events = (await ac.get("/api/path/events", params={"target_id": "isp_1"})).json()
    assert [(e["kind"], e["target_id"], e["old"], e["new"], e["revision"]) for e in events] == [
        ("address_changed", "isp_1", "198.51.100.9", "198.51.100.10", 2)]
    assert abs(events[0]["at"] - time.time()) < 60
    assert (await ac.get("/api/path/events", params={"target_id": "gw"})).json() == []
    assert "Provider hop 1: address 198.51.100.9 → 198.51.100.10" in (
        await ac.get("/api/path/versions")).json()[0]["summary"]


async def test_an_apply_without_an_address_change_records_no_event(path_app):
    ac, _ = path_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0, name="Renamed"), _hop(ISP, 1)]})
    assert (await ac.get("/api/path/events")).json() == []


async def test_a_removed_hop_restored_at_another_address_records_the_change(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    await repo.save_path(1, [GW])
    await repo.save_path(2, [GW, ISP.model_copy(update={"address": "198.51.100.11"})])
    assert [(e["old"], e["new"], e["revision"]) for e in await repo.path_events("isp_1")] == [
        ("198.51.100.9", "198.51.100.11", 3)]


async def test_deleting_a_removed_hops_history_deletes_its_events(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    await repo.save_path(1, [GW, ISP.model_copy(update={"address": "198.51.100.11"})])
    await repo.save_path(2, [GW])
    assert len(await repo.path_events("isp_1")) == 1
    assert await repo.delete_path_history("isp_1")
    assert await repo.path_events("isp_1") == []
