"""Hops linked to their devices by address (engine.device_links) and "ignores ping" as an observation only: the one-time
import, the start of a path stored without links, an applied new hop and the editor's proposals (GET /api/path) link a
home hop to the one device at its address; an older file or row that says a hop ignores ping loads and changes nothing."""

import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.api.path import read_import
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.settings import DeviceConfig
from pengumon.engine.device_links import device_addresses, link_devices, matching_device
from pengumon.engine.path import PathManager
from pengumon.engine.prober import NetworkProber
from pengumon.main import app
from pengumon.models.probes import TargetConfig

from icmp_fakes import FakeBackend

ROUTER = DeviceConfig(id="router", driver="asuswrt", url="http://192.0.2.1", model="Example_Router")
MODEM = DeviceConfig(id="modem", driver="xfinity_xb", url="http://gw.example.com:8080")
LAN = TargetConfig(id="lan", name="Router", address="192.0.2.1", category="local")
LINE = TargetConfig(id="line", name="Modem", address="198.51.100.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop", address="198.51.100.9", category="isp")
PUB = TargetConfig(id="pub", name="Public", address="203.0.113.53", category="public")


async def resolve(host):
    return {"gw.example.com": ["198.51.100.1"]}.get(host, [])


async def _repo(tmp_path) -> DatabaseRepository:
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    return repo


def _hop(t: TargetConfig, position: int, **extra) -> dict:
    return {**t.model_dump(), "position": position, **extra}


# --- the rule ----------------------------------------------------------------------------------------------------------

async def test_a_home_hop_links_to_the_one_device_at_its_address_by_literal_or_resolved_name():
    linked = await link_devices([LAN, LINE, ISP, PUB], [ROUTER, MODEM], resolve=resolve)
    assert [t.device_id for t in linked] == ["router", "modem", None, None]
    assert LAN.device_id is None  # copies: the given targets are left as they are


async def test_no_link_for_another_role_an_owners_link_or_two_devices_at_one_address():
    nas = TargetConfig(id="nas", name="NAS", address="192.0.2.1", category="custom")
    mine = LAN.model_copy(update={"device_id": "modem"})
    assert [t.device_id for t in await link_devices([nas, mine], [ROUTER, MODEM], resolve=resolve)] == [None, "modem"]
    twin = DeviceConfig(id="ap", driver="asuswrt", url="https://192.0.2.1:8443")
    assert (await link_devices([LAN], [ROUTER, twin], resolve=resolve))[0].device_id is None


async def test_addresses_compare_by_value_and_names_by_name():
    v6 = DeviceConfig(id="v6", driver="snmp", url="snmp://[2001:DB8::1]")
    named = DeviceConfig(id="named", driver="asuswrt", url="http://Router.Example.COM")
    addresses = await device_addresses([v6, named], resolve=resolve)
    assert matching_device(TargetConfig(id="a", name="A", address="2001:db8::1", category="local"), addresses) == "v6"
    assert matching_device(TargetConfig(id="b", name="B", address="router.example.com", category="local"), addresses) == "named"


# --- where it happens --------------------------------------------------------------------------------------------------

async def test_the_one_time_import_links_the_home_hops(tmp_path):
    repo = await _repo(tmp_path)
    config = AppConfig(targets=[LAN, LINE, PUB], devices=[ROUTER, MODEM])
    await PathManager(config, repo, resolve=resolve).load()
    _, hops = await repo.load_path()
    assert [t.device_id for t in hops] == ["router", "modem", None]


async def test_a_path_stored_without_links_is_linked_once_at_the_start(tmp_path):
    repo = await _repo(tmp_path)
    assert await repo.save_path(0, [LAN, LINE, PUB]) == 1
    with sqlite3.connect(repo.db_path) as db:  # stored by 0.19, which had no such mark
        db.execute("DELETE FROM meta WHERE key = 'path_devices_linked'")
    config = AppConfig(devices=[ROUTER, MODEM])
    await PathManager(config, repo, resolve=resolve).load()
    assert [t.device_id for t in config.targets] == ["router", "modem", None]
    revision, hops = await repo.load_path()
    assert [t.device_id for t in hops] == ["router", "modem", None]
    # the owner unlinks the router; the next start leaves that alone
    assert await repo.save_path(revision, [LAN, hops[1], PUB]) == revision + 1
    config = AppConfig(devices=[ROUTER, MODEM])
    await PathManager(config, repo, resolve=resolve).load()
    assert [t.device_id for t in config.targets] == [None, "modem", None]


@pytest.fixture
async def links_app(tmp_path, app_state):
    repo = await _repo(tmp_path)
    config = AppConfig(targets=[PUB], devices=[ROUTER, MODEM])
    prober = NetworkProber(backend=FakeBackend())
    manager = PathManager(config, repo, prober, resolve=resolve)
    await manager.load()
    prober.set_targets(config.targets)
    app_state.config, app_state.repository, app_state.prober, app_state.path = config, repo, prober, manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, app_state


async def test_apply_links_a_new_home_hop_but_not_one_the_owner_left_without_a_device(links_app):
    ac, state = links_app
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(LAN, 0), _hop(PUB, 1)]})
    assert res.status_code == 200, res.text
    assert res.json()["hops"][0]["device_id"] == "router"
    assert state.config.targets[0].device_id == "router"
    # the owner sets "No device" on that (now existing) hop and adds the modem's hop
    res = await ac.put("/api/path", json={"base_revision": 2, "hops": [_hop(LAN, 0, device_id=None), _hop(LINE, 1),
                                                                       _hop(PUB, 2)]})
    assert [h["device_id"] for h in res.json()["hops"]] == [None, "modem", None]


async def test_get_path_proposes_the_device_of_each_unlinked_home_hop(links_app):
    ac, _ = links_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(LAN, 0), _hop(LINE, 1), _hop(ISP, 2)]})
    await ac.put("/api/path", json={"base_revision": 2, "hops": [_hop(LAN, 0, device_id=None),
                                                                 _hop(LINE, 1, device_id="modem"), _hop(ISP, 2)]})
    body = (await ac.get("/api/path")).json()
    assert [h["device_id"] for h in body["hops"]] == [None, "modem", None]
    assert body["device_matches"] == {"lan": "router"}  # the editor links it in its draft; Apply keeps it


# --- ignores ping: observed, never set ----------------------------------------------------------------------------------

def test_an_older_export_that_sets_ignores_ping_reads_without_it():
    text = ("targets:\n  - {id: isp_1, name: Provider hop, category: isp, address: 198.51.100.9, ignores_ping: true}\n"
            "  - {id: pub, name: Public, category: public, address: 203.0.113.53, ignores_ping: false}\n")
    targets = read_import(text)
    assert [t.id for t in targets] == ["isp_1", "pub"]
    assert all("ignores_ping" not in t.model_dump() for t in targets)


async def test_an_imported_or_applied_ignores_ping_has_no_effect_on_the_prober(links_app):
    ac, state = links_app
    text = "targets:\n  - {id: isp_1, name: Provider hop, category: isp, address: 198.51.100.9, ignores_ping: true}\n" \
           "  - {id: pub, name: Public, category: public, address: 203.0.113.53}\n"
    res = await ac.post("/api/path/import", content=text.encode())
    assert res.status_code == 200, res.text
    hops = res.json()["hops"]
    assert "ignores_ping" not in hops[0]
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [{**hops[0], "ignores_ping": True}, hops[1]]})
    assert res.status_code == 200, res.text
    prober = state.prober
    prober.stats["isp_1"].status, prober.stats["pub"].status = "down", "down"
    prober.update_path_flags()
    assert not prober.stats["isp_1"].ignores_ping  # nothing after it answers: a real outage, whatever the file said
    prober.stats["pub"].status = "up"
    prober.update_path_flags()
    assert prober.stats["isp_1"].ignores_ping  # the rule alone decides
    with sqlite3.connect(state.repository.db_path) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT config FROM path_targets")]
    assert all("ignores_ping" not in r for r in rows)


async def test_a_stored_ignores_ping_is_dropped_at_the_start(tmp_path):
    repo = await _repo(tmp_path)
    assert await repo.save_path(0, [ISP, PUB]) == 1
    with sqlite3.connect(repo.db_path) as db:  # rows as 0.19 wrote them, the owner's answer included
        for tid, answer in (("isp_1", True), ("pub", False)):
            row = json.loads(db.execute("SELECT config FROM path_targets WHERE id = ?", (tid,)).fetchone()[0])
            db.execute("UPDATE path_targets SET config = ? WHERE id = ?", (json.dumps({**row, "ignores_ping": answer}), tid))
    config = AppConfig()
    await PathManager(config, repo).load()
    assert [t.id for t in config.targets] == ["isp_1", "pub"]
    with sqlite3.connect(repo.db_path) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT config FROM path_targets")]
    assert all("ignores_ping" not in r for r in rows)
    assert (await repo.load_path())[0] == 1  # a data fix, not an edit: the revision stays


# --- an explicit "No device" sticks ------------------------------------------------------------------------------------

async def test_a_hop_the_owner_set_to_no_device_is_never_linked_again(links_app, tmp_path):
    ac, state = links_app
    unlinked = _hop(LAN, 0, device_unlinked=True)
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [unlinked, _hop(PUB, 1)]})
    assert res.status_code == 200, res.text
    assert res.json()["hops"][0]["device_id"] is None and res.json()["hops"][0]["device_unlinked"] is True
    assert (await ac.get("/api/path")).json()["device_matches"] == {}  # the editor proposes nothing for it
    text = (await ac.get("/api/path/export")).text
    assert "device_unlinked: true" in text  # an export keeps the choice
    imported = (await ac.post("/api/path/import", content=text.encode())).json()
    assert imported["device_matches"] == {} and imported["hops"][0]["device_unlinked"] is True
    # and a start that links the home hops of an older path leaves it alone too
    assert [t.device_id for t in await link_devices([LAN.model_copy(update={"device_unlinked": True})], [ROUTER], resolve=resolve)] == [None]


async def test_a_viewer_gets_no_device_proposals_from_get_path(links_app, monkeypatch):
    """The proposals may look up host names; only the path editor (an editor's) uses them."""
    from pengumon.main import security_config
    ac, _ = links_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(LAN, 0), _hop(LINE, 1), _hop(ISP, 2)]})
    await ac.put("/api/path", json={"base_revision": 2, "hops": [_hop(LAN, 0, device_id=None),
                                                                 _hop(LINE, 1, device_id="modem"), _hop(ISP, 2)]})
    monkeypatch.setattr(security_config, "access_token", "example-token")
    monkeypatch.setattr(security_config, "view_without_token", True)
    viewer = (await ac.get("/api/path")).json()
    editor = (await ac.get("/api/path", headers={"Authorization": "Bearer example-token"})).json()
    assert viewer["device_matches"] == {} and [h["id"] for h in viewer["hops"]] == [h["id"] for h in editor["hops"]]
    assert editor["device_matches"] == {"lan": "router"}
