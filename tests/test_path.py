"""The probe path in the database (db/path.py, engine/path.py) and its API (api/path.py): the one-time import keeps the
ids and never runs twice, an edit is checked against its revision and applied atomically, removed hops keep their
history hidden, and an applied path is probed without a restart."""

import asyncio
import logging
import sqlite3
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import main
from pengumon.api.path import export_yaml, read_import
from pengumon.state import STATE_ATTR
from pengumon.config import AppConfig, load_config
from pengumon.db.path import PathConflict
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.settings import DeviceConfig
from pengumon.engine.icmp_base import ProbeResult
from pengumon.engine.path import IGNORED_TARGETS_LINE, MAX_PATH_HOPS, PathError, PathManager
from pengumon.engine.prober import NetworkProber
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.main import app
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.reports.queries import daily_availability

from icmp_fakes import FakeBackend

GW = TargetConfig(id="gw", name="Gateway", address="192.0.2.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop 1", address="198.51.100.9", category="isp")
PUB = TargetConfig(id="pub", name="Public", address="203.0.113.53", category="public")


def _custom_row(db_path: str, target: TargetConfig, created: float) -> None:
    """A custom_targets row as an older build wrote it (POST /api/targets before the path moved to path_targets)."""
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO custom_targets (id, config, created) VALUES (?, ?, ?)",
                   (target.id, target.model_dump_json(), created))


async def _repo(tmp_path, name="p.db") -> DatabaseRepository:
    repo = DatabaseRepository(db_path=str(tmp_path / name), sample_flush_seconds=0)
    await repo.initialize()
    return repo


def _hop(t: TargetConfig, position: int, **extra) -> dict:
    return {**t.model_dump(), "position": position, **extra}


# --- the store: import, revisions, soft delete ---------------------------------------------------------------------

async def test_first_start_imports_config_and_custom_targets_with_their_ids_in_order(tmp_path):
    repo = await _repo(tmp_path)
    mine = TargetConfig(id="nas", name="NAS", address="192.0.2.50", category="custom")
    clash = TargetConfig(id="gw", name="Shadowed", address="192.0.2.99", category="custom")
    _custom_row(repo.db_path, mine, 2.0)
    _custom_row(repo.db_path, clash, 1.0)  # same id as a config.yaml target: config.yaml's wins, as it always did
    config = AppConfig(targets=[GW, ISP, PUB])

    await PathManager(config, repo).load()

    revision, hops = await repo.load_path()
    assert revision == 1 and await repo.path_initialized()
    assert [t.id for t in hops] == ["gw", "isp_1", "pub", "nas"]
    assert hops[0] == GW and hops[3] == mine
    assert [t.id for t in config.targets] == ["gw", "isp_1", "pub", "nas"]


async def test_the_import_runs_once_even_after_every_hop_was_removed(tmp_path, caplog):
    repo = await _repo(tmp_path)
    await PathManager(AppConfig(targets=[GW, PUB]), repo).load()
    assert await repo.save_path(1, []) == 2  # the owner removed every hop

    config = AppConfig(targets=[GW, PUB, ISP])  # config.yaml still lists targets (and one more)
    with caplog.at_level(logging.INFO, logger="pengumon.path"):
        await PathManager(config, repo).load()
    assert config.targets == []
    assert (await repo.load_path()) == (2, [])
    assert not await repo.import_path([ISP])  # the store refuses a second import too
    assert any(IGNORED_TARGETS_LINE in r.getMessage() for r in caplog.records)


async def test_without_targets_in_config_yaml_the_defaults_are_probed_unstored_until_a_path_is_applied(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {db_path: " + repr(str(tmp_path / "p.db")) + "}\n", encoding="utf-8")
    monkeypatch.setattr("pengumon.config.get_default_gateway_ip", lambda: "192.0.2.254")
    config = load_config(str(cfg))
    assert not config.targets_from_file
    repo = await _repo(tmp_path)
    await PathManager(config, repo).load()
    assert [t.id for t in config.targets] == ["local_router", "cloudflare_dns", "google_dns"]
    assert await repo.path_revision() == 0 and not await repo.path_initialized()
    assert await repo.load_path() == (0, [])


async def test_custom_targets_alone_import_together_with_the_defaults_they_were_probed_with(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {db_path: " + repr(str(tmp_path / "p.db")) + "}\n", encoding="utf-8")
    monkeypatch.setattr("pengumon.config.get_default_gateway_ip", lambda: None)
    config = load_config(str(cfg))
    repo = await _repo(tmp_path)
    _custom_row(repo.db_path, TargetConfig(id="nas", name="NAS", address="192.0.2.50"), 1.0)
    await PathManager(config, repo).load()
    assert [t.id for t in (await repo.load_path())[1]] == ["cloudflare_dns", "google_dns", "nas"]


async def test_a_stale_revision_is_refused_and_changes_nothing(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, PUB])
    assert await repo.save_path(1, [GW, ISP, PUB]) == 2
    with pytest.raises(PathConflict) as refused:
        await repo.save_path(1, [PUB])
    assert refused.value.current == 2
    assert (await repo.load_path()) == (2, [GW, ISP, PUB])
    assert await repo.removed_path_ids() == set()


async def test_a_removed_hop_keeps_its_history_hidden_until_it_comes_back(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP, PUB])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=now - 120 + k, success=True, latency_ms=1.0)
                               for t in (GW, ISP, PUB) for k in range(5)])
    await repo.flush_samples()

    def ids(rows):
        return {r["target_id"] for r in rows}

    assert ids(await repo.get_history(now - 600, now)) == {"gw", "isp_1", "pub"}
    await repo.save_path(1, [GW, PUB])
    assert await repo.removed_path_ids() == {"isp_1"}
    assert ids(await repo.get_history(now - 600, now)) == {"gw", "pub"}
    assert ids(await repo.get_history(now - 600, now, target_ids=["isp_1"])) == set()
    assert ids(await repo.get_hop_stats(now - 600)) == {"gw", "pub"}
    assert ids(await daily_availability(repo, now - 600)) == {"gw", "pub"}

    again = DatabaseRepository(db_path=repo.db_path)  # hidden after a restart too
    await again.initialize()
    assert ids(await again.get_history(now - 600, now)) == {"gw", "pub"}
    await again.close()

    await repo.save_path(2, [GW, ISP, PUB])  # added back with the same id: its history is there again
    assert ids(await repo.get_history(now - 600, now)) == {"gw", "isp_1", "pub"}


async def test_deleting_history_only_works_for_removed_hops(tmp_path):
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=now - 10 + k, success=True, latency_ms=1.0)
                               for t in (GW, ISP) for k in range(3)])
    await repo.flush_samples()
    assert not await repo.delete_path_history("isp_1")  # on the path: refused
    await repo.save_path(1, [GW])
    assert await repo.delete_path_history("isp_1")
    assert await repo.removed_path_ids() == set()
    repo.hidden_targets = set()
    assert {r["target_id"] for r in await repo.get_history(now - 60, now + 60)} == {"gw"}
    isp = repo._target_ids["isp_1"]  # every table that holds its probes, rollups of all three widths included
    with sqlite3.connect(repo.db_path) as db:
        assert [(t, db.execute(f"SELECT COUNT(*) FROM {t} WHERE target = ?", (isp,)).fetchone()[0],
                 db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0)
                for t in ("samples", "rollup_10s", "rollup_1m", "rollup_15m")] == [
            ("samples", 0, True), ("rollup_10s", 0, True), ("rollup_1m", 0, True), ("rollup_15m", 0, True)]
    assert not await repo.delete_path_history("isp_1")


# --- the prober follows an applied path ----------------------------------------------------------------------------

def test_set_targets_keeps_stats_of_unchanged_hops_resets_moved_ones_and_keeps_dns_last():
    prober = NetworkProber()
    for t in (GW, ISP):
        prober.register_target(t)
    prober.register_dns_target(DnsProbeConfig(id="dns_a", name="DNS"))
    prober.stats["gw"].sent = 7
    prober.stats["isp_1"].sent = 5
    moved = ISP.model_copy(update={"address": "198.51.100.10"})
    renamed = GW.model_copy(update={"name": "Modem", "group": "line"})
    prober.set_targets([PUB, renamed, moved], dns_ids=["dns_a"])
    assert list(prober.stats) == ["pub", "gw", "isp_1", "dns_a"]
    assert prober.stats["gw"].sent == 7 and prober.stats["gw"].name == "Modem" and prober.stats["gw"].group == "line"
    assert prober.stats["isp_1"].sent == 0  # another address: another hop's statistics
    prober.set_targets([PUB], dns_ids=["dns_a"])
    assert list(prober.stats) == ["pub", "dns_a"]


# --- the API ---------------------------------------------------------------------------------------------------------

@pytest.fixture
async def path_app(tmp_path, app_state):
    repo = await _repo(tmp_path)
    config = AppConfig(targets=[GW, PUB], dns_probes=[DnsProbeConfig(id="dns_a", name="DNS")],
                       devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.0.2.1")])
    prober = NetworkProber(backend=FakeBackend())
    manager = PathManager(config, repo, prober)
    await manager.load()
    prober.set_targets(config.targets)
    prober.register_dns_target(config.dns_probes[0])
    app_state.config, app_state.repository, app_state.prober, app_state.path = config, repo, prober, manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, app_state


async def test_get_targets_names_each_hop_by_its_role_words_and_follows_a_path_edit(path_app):
    ac, state = path_app

    async def labels():
        return {t["target_id"]: t["short_label"] for t in (await ac.get("/api/targets")).json()}

    assert await labels() == {"gw": "Gateway", "pub": "Internet", "dns_a": "DNS lookup"}
    second = ISP.model_copy(update={"id": "isp_2", "address": "198.51.100.20"})
    state.prober.set_targets([GW, ISP, second, PUB.model_copy(update={"enabled": False})], dns_ids=["dns_a"])
    assert await labels() == {"gw": "Gateway", "isp_1": "ISP hop 1", "isp_2": "ISP hop 2", "pub": None,
                              "dns_a": "DNS lookup"}


async def test_get_path_lists_the_hops_in_order_with_their_positions(path_app):
    ac, _ = path_app
    body = (await ac.get("/api/path")).json()
    assert body["revision"] == 1 and body["managed_by"] == "dashboard"
    assert [(h["id"], h["position"]) for h in body["hops"]] == [("gw", 0), ("pub", 1)]
    assert {"group", "device_id", "color", "record_type", "nameserver"} <= set(body["hops"][0])
    assert "ignores_ping" not in body["hops"][0]  # observed (the live targets), never a setting of the hop


async def test_put_applies_the_path_live_and_ignores_the_draft_only_fields(path_app):
    ac, state = path_app
    hops = [_hop(PUB, 9), _hop(GW, 0, device_id="router", ignores_ping=False),
            _hop(ISP, 4, evidence=[{"kind": "rdns", "text": "provider name"}], confidence="likely", status="new",
                 suggested_driver=None)]
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": hops})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["revision"] == 2 and [h["id"] for h in body["hops"]] == ["gw", "isp_1", "pub"]
    assert [h["position"] for h in body["hops"]] == [0, 1, 2]
    assert "evidence" not in body["hops"][1]
    assert [t.id for t in state.config.targets] == ["gw", "isp_1", "pub"]
    assert list(state.prober.stats) == ["gw", "isp_1", "pub", "dns_a"]
    assert state.config.targets[0].device_id == "router"
    assert (await ac.get("/api/path")).json()["revision"] == 2


async def test_put_with_a_stale_revision_is_a_409_and_nothing_changes(path_app):
    ac, state = path_app
    assert (await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0)]})).status_code == 200
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(PUB, 0)]})
    assert res.status_code == 409 and res.json()["revision"] == 2
    assert [t.id for t in state.config.targets] == ["gw"]


@pytest.mark.parametrize("hops, status, says", [
    ([_hop(GW, 0, address="192.0.2.1; reboot")], 422, "Host"),
    ([_hop(GW, 0, address="-f")], 422, "Host"),
    ([_hop(GW, 0, id="bad id")], 422, "Target ID"),
    ([_hop(GW, 0), _hop(GW, 1)], 422, "unique"),
    ([_hop(GW, 0), _hop(PUB, 0)], 422, "same position"),
    ([_hop(GW, 0, category="router")], 422, "category"),
    ([_hop(GW, 0, group="not a group")], 422, "group"),
    ([_hop(TargetConfig(id=f"h{i}", name="H", address="192.0.2.7"), i) for i in range(MAX_PATH_HOPS + 1)], 422,
     "at most 64"),
    ([_hop(GW, 0, id="dns_a")], 400, "DNS lookup"),
    ([_hop(GW, 0, device_id="nope")], 400, "(device nope) is linked to a device that is not set up here"),
])
async def test_put_refuses_an_invalid_path_and_keeps_the_live_one(path_app, hops, status, says):
    ac, state = path_app
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": hops})
    assert res.status_code == status, res.text
    assert says.lower() in res.text.lower()
    assert [t.id for t in state.config.targets] == ["gw", "pub"]
    assert (await ac.get("/api/path")).json()["revision"] == 1


async def test_sixty_four_hops_are_allowed(path_app):
    ac, state = path_app
    hops = [_hop(TargetConfig(id=f"h{i}", name="H", address="192.0.2.7"), i) for i in range(MAX_PATH_HOPS)]
    assert (await ac.put("/api/path", json={"base_revision": 1, "hops": hops})).status_code == 200
    assert len(state.config.targets) == MAX_PATH_HOPS


async def test_history_delete_needs_a_removed_hop(path_app):
    ac, _ = path_app
    assert (await ac.delete("/api/path/history/gw")).status_code == 400  # on the path
    assert (await ac.delete("/api/path/history/nope")).status_code == 404
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(PUB, 0)]})
    assert (await ac.delete("/api/path/history/gw")).status_code == 200
    assert (await ac.delete("/api/path/history/gw")).status_code == 404


async def test_test_probe_sends_one_echo_request_through_the_backend(path_app):
    ac, state = path_app
    backend = state.prober._backend
    res = await ac.post("/api/path/test", json={"address": "198.51.100.9"})
    assert res.json() == {"kind": "reply", "rtt_ms": 1.0, "responder": "198.51.100.9", "address": "198.51.100.9",
                          "error": None}
    assert [s[0] for s in backend.sent] == ["198.51.100.9"]
    backend._answer = lambda address, ttl: ProbeResult("timeout")
    assert (await ac.post("/api/path/test", json={"address": "198.51.100.9"})).json()["kind"] == "timeout"
    assert (await ac.post("/api/path/test", json={"address": "1.1.1.1 && id"})).status_code == 422
    assert len(backend.sent) == 2


async def test_export_and_import_round_trip_as_a_draft_that_applies_nothing(path_app):
    ac, state = path_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [
        _hop(GW, 0, device_id="router"), _hop(ISP, 1), _hop(PUB, 2, group="internet",
                                                                               color="#112233")]})
    exported = await ac.get("/api/path/export")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    assert exported.text.splitlines()[1] == "targets:"
    state.config.devices = []  # another install, without that device: the link is dropped, the hop kept
    res = await ac.post("/api/path/import", content=exported.text.encode())
    assert res.status_code == 200, res.text
    hops = res.json()["hops"]
    assert [(h["id"], h["position"]) for h in hops] == [("gw", 0), ("isp_1", 1), ("pub", 2)]
    assert hops[0]["device_id"] is None
    assert hops[2]["group"] == "internet" and hops[2]["color"] == "#112233"
    assert (await ac.get("/api/path")).json()["revision"] == 2  # nothing applied


@pytest.mark.parametrize("text, says", [
    ("targets: [", "not valid YAML at line"),
    ("devices: []\n", "No targets"),
    ("targets:\n  - just a string\n", "mapping"),
    ("targets:\n  - {id: a, name: A, address: '1.1.1.1;x'}\n", "Host"),
    ("targets:\n  - {id: a, name: A, address: 192.0.2.1}\n  - {id: a, name: B, address: 192.0.2.2}\n", "unique"),
])
async def test_import_refuses_a_bad_file(path_app, text, says):
    ac, _ = path_app
    res = await ac.post("/api/path/import", content=text.encode())
    assert res.status_code == 422 and says.lower() in res.text.lower(), res.text


def test_export_leaves_defaults_out_and_reads_back_the_same():
    targets = [GW, ISP.model_copy(update={"enabled": False})]
    text = export_yaml(targets)
    assert "record_type" not in text and "enabled: false" in text
    assert read_import(text) == targets


# --- live reload in the running app -----------------------------------------------------------------------------------

async def test_a_hop_added_by_put_is_probed_without_a_restart_and_a_removed_one_stops(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("database: {db_path: " + repr(str(tmp_path / "p.db")) + "}\nprober: {interval_seconds: 0.05}\n"
                   "dns_probes: []\ntargets:\n  - {id: gw, name: Gateway, address: 192.0.2.1, category: gateway}\n"
                   "  - {id: pub, name: Public, address: 203.0.113.53, category: public}\n", encoding="utf-8")
    monkeypatch.setenv("PENGUMON_CONFIG", str(cfg))
    probed = []

    async def fake_icmp(self, target):
        probed.append(target.id)
        return ProbeSample(target_id=target.id, timestamp=time.time(), success=True, latency_ms=1.0)

    monkeypatch.setattr(NetworkProber, "probe_icmp", fake_icmp)
    monkeypatch.setattr(NetworkProber, "backend", lambda self: FakeBackend())

    async def wait_for(cond):
        for _ in range(200):
            if cond():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("timed out")

    async with main.lifespan(main.app):
        state = getattr(main.app.state, STATE_ATTR)
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
            await wait_for(lambda: {"gw", "pub"} <= set(probed))
            res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(ISP, 1)]})
            assert res.status_code == 200, res.text
            probed.clear()
            await wait_for(lambda: probed.count("isp_1") >= 3)
            assert "pub" not in probed
            assert state.classifier.known_targets is state.config.targets
            assert [t.id for t in state.classifier.known_targets] == ["gw", "isp_1"]
            listed = [t["target_id"] for t in (await ac.get("/api/targets")).json()]
            assert listed == ["gw", "isp_1"]
    # and after a restart the applied path is what is probed, not config.yaml's
    probed.clear()
    async with main.lifespan(main.app):
        await wait_for(lambda: "isp_1" in probed)
        assert "pub" not in probed


async def test_manager_check_refuses_more_than_the_limit():
    manager = PathManager(AppConfig(), repository=None)
    with pytest.raises(PathError):
        manager.check([TargetConfig(id=f"h{i}", name="H", address="192.0.2.7") for i in range(MAX_PATH_HOPS + 1)])


# --- IPv6 ------------------------------------------------------------------------------------------------------------

def _no_ipv6():
    from pengumon.engine.netinfo import Ipv6Availability
    return Ipv6Availability(check=lambda: "no IPv6 default route")


def _with_ipv6():
    from pengumon.engine.netinfo import Ipv6Availability
    return Ipv6Availability(check=lambda: None)  # the host's real routes must not decide the test


async def test_family_is_carried_through_put_export_and_import(path_app):
    ac, state = path_app
    v6name = TargetConfig(id="v6_name", name="V6 by name", address="example.com", category="public", family=6)
    v6lit = TargetConfig(id="v6_lit", name="V6 literal", address="2001:db8::1", category="public")
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(v6name, 1), _hop(v6lit, 2)]})
    assert res.status_code == 200, res.text
    assert [h["family"] for h in res.json()["hops"]] == [4, 6, 4]
    assert state.config.targets[1].family == 6
    text = (await ac.get("/api/path/export")).text
    assert text.count("family: 6") == 1
    hops = (await ac.post("/api/path/import", content=text.encode())).json()["hops"]
    assert [(h["id"], h["family"]) for h in hops] == [("gw", 4), ("v6_name", 6), ("v6_lit", 4)]
    assert hops[2]["address"] == "2001:db8::1"


async def test_a_hops_warn_level_is_stored_applied_live_and_exported(path_app):
    ac, state = path_app
    far = TargetConfig(id="far", name="Far server", address="203.0.113.80", category="public", warn_latency_ms=150)
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(far, 1)]})
    assert res.status_code == 200, res.text
    assert [h["warn_latency_ms"] for h in res.json()["hops"]] == [None, 150.0]
    assert state.config.targets[1].warn_latency_ms == 150
    _, stored = await state.repository.load_path()
    assert [t.warn_latency_ms for t in stored] == [None, 150.0]
    for _ in range(3):  # 120 ms: amber for a public hop (60 ms), not for this one
        state.prober._update_target_stats(ProbeSample(target_id="far", success=True, latency_ms=120.0))
    assert state.prober.stats["far"].status == "up"
    text = (await ac.get("/api/path/export")).text
    assert text.count("warn_latency_ms: 150") == 1  # left out where unset
    hops = (await ac.post("/api/path/import", content=text.encode())).json()["hops"]
    assert [h["warn_latency_ms"] for h in hops] == [None, 150.0]


async def test_test_probe_uses_the_family_and_says_when_this_host_has_no_ipv6(path_app, monkeypatch):
    ac, state = path_app
    backend = state.prober._backend
    looked_up = []

    async def resolve(host, timeout, family=4):
        looked_up.append((host, family))
        return ("2001:db8::7", 6) if family == 6 else ("192.0.2.7", 4)

    monkeypatch.setattr("pengumon.api.path.resolve_address", resolve)
    state.prober.ipv6 = _with_ipv6()
    assert (await ac.post("/api/path/test", json={"address": "example.com", "family": 6})).json()["address"] == "2001:db8::7"
    assert looked_up == [("example.com", 6)] and backend.sent[-1][0] == "2001:db8::7"

    state.prober.ipv6 = _no_ipv6()
    sent = len(backend.sent)
    for body in ({"address": "2001:db8::1"}, {"address": "example.com", "family": 6}):
        r = (await ac.post("/api/path/test", json=body)).json()
        assert r["kind"] == "ipv6_unavailable" and r["error"] == "IPv6 unavailable on this host", body
    assert len(backend.sent) == sent  # nothing was sent
    assert (await ac.post("/api/path/test", json={"address": "192.0.2.7"})).json()["kind"] == "reply"
    assert (await ac.post("/api/path/test", json={"address": "192.0.2.7", "family": 5})).status_code == 422


async def test_a_send_that_fails_for_lack_of_ipv6_is_the_neutral_state_too(path_app):
    import errno
    ac, state = path_app
    code = errno.ENETUNREACH

    def refuse(address, ttl):
        raise OSError(code, "no route")

    state.prober._backend._answer = refuse
    r = (await ac.post("/api/path/test", json={"address": "::1"})).json()
    assert r["kind"] == "ipv6_unavailable"
    assert state.prober.ipv6.reason  # recorded (Ipv6Availability.no_route), as the prober does


def test_a_hop_whose_family_changed_starts_over():
    prober = NetworkProber()
    name = TargetConfig(id="v", name="V", address="example.com", category="public")
    prober.set_targets([name])
    prober.stats["v"].sent = 9
    prober.set_targets([name.model_copy(update={"name": "Renamed"})])
    assert prober.stats["v"].sent == 9
    prober.set_targets([name.model_copy(update={"family": 6})])
    assert prober.stats["v"].sent == 0


# --- rediscover against the stored path --------------------------------------------------------------------------------

def _proposed(address: str, category: str = "isp") -> dict:
    from pengumon.engine.discovery.draft import hop_id
    return {"id": hop_id(address), "name": f"Provider hop ({address})", "address": address, "category": category,
            "enabled": True, "group": None, "color": None, "device_id": None,
            "evidence": [{"kind": "rdns", "text": "a reason"}], "confidence": "likely", "suggested_driver": None,
            "status": "new"}


def test_a_removed_hop_found_again_comes_back_with_its_id_and_one_not_found_stays_away():
    from pengumon.engine.discovery.draft import diff_against, hop_id
    from pengumon.engine.discovery.engine import live_path_from_targets
    kept = TargetConfig(id="mine_isp", name="My provider router", address="198.51.100.9", category="isp")
    away = TargetConfig(id="old_isp", name="Old", address="198.51.100.77", category="isp")
    squatter = TargetConfig(id=hop_id("198.51.100.20"), name="Removed, same id as a new hop", address="192.0.2.99",
                            category="custom")
    live = live_path_from_targets([GW, PUB])
    removed = live_path_from_targets([kept, away, squatter])
    draft = diff_against([_proposed("198.51.100.9"), _proposed("198.51.100.20")], live, removed=removed)
    by_address = {h["address"]: h for h in draft}
    back = by_address["198.51.100.9"]
    assert back["id"] == "mine_isp" and back["name"] == "My provider router" and back["status"] == "new"
    assert back["evidence"] and back["confidence"] == "likely"
    assert "198.51.100.77" not in by_address and "192.0.2.99" not in by_address  # not found: not proposed
    assert by_address["198.51.100.20"]["id"] != squatter.id  # a new hop never takes a removed hop's id
    assert [h["status"] for h in draft if h["address"] in ("192.0.2.1", "203.0.113.53")] == ["gone", "gone"]


async def test_rediscover_compares_with_the_applied_path_and_its_removed_hops(path_app):
    from pengumon.engine.discovery.component import applied_paths
    ac, state = path_app
    await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), _hop(ISP, 1)]})
    live, removed = await applied_paths(state)
    assert [h["id"] for h in live] == ["gw", "isp_1"] and [h["position"] for h in live] == [0, 1]
    assert [h["id"] for h in removed] == ["pub"]


def test_configured_devices_get_one_front_page_request_from_discovery():
    from pengumon.engine.discovery.component import device_hosts
    config = AppConfig(devices=[DeviceConfig(id="router", driver="asuswrt", url="http://192.0.2.1"),
                                DeviceConfig(id="modem", driver="xfinity_xb", url="https://192.0.2.254:8443/")])
    assert device_hosts(config) == frozenset({"192.0.2.1", "192.0.2.254"})


async def test_discoverys_ping_resolves_with_the_ipv6_aware_resolver(monkeypatch):
    from pengumon.engine.discovery import engine
    backend = FakeBackend()
    monkeypatch.setattr("pengumon.engine.icmp_select.shared_backend", lambda: backend)
    assert await engine._default_ping("192.0.2.1")
    assert backend.sent[0][0] == "192.0.2.1"


# --- hardening (Stage 2 review) ------------------------------------------------------------------------------------------

ALIAS_BOMB = "a: &a [x,x,x,x,x,x,x,x,x]\n" + "".join(
    f"{chr(98 + i)}: &{chr(98 + i)} [" + ",".join([f"*{chr(97 + i)}"] * 9) + "]\n" for i in range(8))
MERGE_BOMB = "l0: &l0 {k: 1}\n" + "".join(
    f"l{i}: &l{i} {{<<: [" + ",".join([f"*l{i - 1}"] * 9) + "]}\n" for i in range(1, 9)) + \
    "targets: [{<<: *l8, id: a, name: n, address: 192.0.2.1}]\n"


@pytest.mark.parametrize("text, says", [
    pytest.param(ALIAS_BOMB + "targets:\n- {id: a, name: n, address: 192.0.2.1, record_type: *i}\n", "anchors and aliases",
                 id="alias bomb"),
    pytest.param(MERGE_BOMB, "anchors and aliases", id="merge bomb"),
    pytest.param("targets:\n- &x {id: a, name: n, address: 192.0.2.1}\n", "anchors and aliases", id="anchor"),
    pytest.param("targets:\n- {<<: {id: a}, name: n, address: 192.0.2.1}\n", "merge keys", id="merge key"),
    pytest.param("targets: " + "[" * 20000, "nested too deeply", id="20000 nested lists"),  # the id was 20 KB of this text
])
async def test_import_refuses_alias_bombs_merge_keys_and_deep_nesting_at_once(path_app, text, says):
    ac, _ = path_app
    started = time.monotonic()
    res = await ac.post("/api/path/import", content=text.encode())
    assert res.status_code == 422 and says in res.text.lower(), res.text
    assert time.monotonic() - started < 2


def test_read_import_refuses_a_file_too_big_to_be_a_path():
    with pytest.raises(ValueError, match="too big"):
        read_import("targets:\n- {id: a, name: n, address: 192.0.2.1}\n" + "# x\n" * 100_000)


def test_a_recursion_error_while_loading_is_a_refusal_not_a_crash(monkeypatch):
    def deep(_text):
        raise RecursionError

    monkeypatch.setattr("pengumon.api.path.yaml.safe_load", deep)
    with pytest.raises(ValueError, match="nested too deeply"):
        read_import("targets: [a]")


async def test_history_of_a_dns_probe_or_a_live_hop_is_never_deleted(path_app):
    ac, state = path_app
    assert (await ac.delete("/api/path/history/dns_a")).status_code == 400
    assert (await ac.delete("/api/path/history/gw")).status_code == 400
    assert (await ac.delete("/api/path/history/nobody")).status_code == 404
    # a stale removed row that shares the id of a DNS probe (written by an older build) is still refused
    await state.repository.save_path(1, [GW])
    with sqlite3.connect(state.repository.db_path) as db:
        db.execute("INSERT INTO path_targets (id, position, config, deleted_at, updated) VALUES ('dns_a', 9, ?, 1, 1)",
                   (TargetConfig(id="dns_a", name="x", address="192.0.2.9").model_dump_json(),))
    assert (await ac.delete("/api/path/history/dns_a")).status_code == 400


async def test_the_one_time_import_keeps_every_target_those_past_the_limit_as_removed_hops(tmp_path, caplog):
    many = [TargetConfig(id=f"t{i}", name=f"T{i}", address=f"192.0.2.{i % 250 + 1}", category="custom")
            for i in range(MAX_PATH_HOPS + 3)]
    repo = await _repo(tmp_path)
    config = AppConfig(targets=many)
    with caplog.at_level(logging.INFO, logger="pengumon.path"):
        await PathManager(config, repo).load()
    assert [t.id for t in config.targets] == [t.id for t in many[:MAX_PATH_HOPS]]
    assert [t.id for t in await repo.removed_path_hops()] == ["t64", "t65", "t66"]
    assert repo.hidden_targets == {"t64", "t65", "t66"}
    assert any("imported as removed hops" in r.getMessage() for r in caplog.records)
    # the owner can bring one back: it returns with its id
    revision, hops = await repo.load_path()
    await repo.save_path(revision, [*hops[:-1], many[-1]])
    assert (await repo.load_path())[1][-1].id == "t66"


async def test_when_going_live_fails_after_the_commit_the_live_path_is_reloaded_from_the_database(path_app, monkeypatch):
    ac, state = path_app
    manager = state.path
    real = manager._go_live
    calls = []

    def flaky(targets):
        calls.append([t.id for t in targets])
        if len(calls) == 1:
            raise RuntimeError("prober refused")
        real(targets)

    monkeypatch.setattr(manager, "_go_live", flaky)
    with pytest.raises(RuntimeError):
        await manager.apply(1, [GW, ISP, PUB])
    assert calls == [["gw", "isp_1", "pub"], ["gw", "isp_1", "pub"]]  # the second try is what the database holds
    assert [t.id for t in state.config.targets] == ["gw", "isp_1", "pub"]
    assert (await manager.current())[0] == 2


async def test_configured_device_names_are_resolved_so_discovery_meets_them_by_address(monkeypatch):
    from pengumon.engine.discovery import component

    config = AppConfig(devices=[DeviceConfig(id="router", driver="asuswrt", url="http://router.home.arpa/"),
                                DeviceConfig(id="gone", driver="asuswrt", url="http://nowhere.invalid"),
                                DeviceConfig(id="modem", driver="xfinity_xb", url="https://192.0.2.254:8443/")])

    async def resolver(self, host, port, **kw):
        if host == "nowhere.invalid":
            raise OSError("no such host")
        assert host == "router.home.arpa"  # a literal address is not looked up
        return [(2, 1, 6, "", ("192.0.2.1", 0)), (10, 1, 6, "", ("2001:db8::1", 0, 0, 0)),
                (10, 1, 6, "", ("fe80::1%eth0", 0, 0, 2))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", resolver)
    hosts = await component.limited_hosts(config)
    assert hosts == frozenset({"router.home.arpa", "192.0.2.1", "2001:db8::1", "fe80::1", "nowhere.invalid",
                               "192.0.2.254"})


async def test_a_fresh_start_with_the_example_config_has_no_path_and_is_at_revision_0(tmp_path, monkeypatch):
    """The image ships config.example.yaml as /app/config.yaml: its placeholder targets must not become the path."""
    from pathlib import Path

    monkeypatch.setattr("pengumon.config.get_default_gateway_ip", lambda: "192.0.2.254")
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = load_config(str(example))
    assert not config.targets_from_file
    repo = await _repo(tmp_path)
    await PathManager(config, repo).load()
    assert await repo.path_revision() == 0 and not await repo.path_initialized()  # first run: discovery
    assert [t.id for t in config.targets][0] == "local_router" and "isp_hop_1" not in {t.id for t in config.targets}


async def test_deleting_history_goes_in_batches_so_the_sample_flush_is_not_held_for_all_of_it(tmp_path, monkeypatch):
    from pengumon.db import path as path_module
    monkeypatch.setattr(path_module, "HISTORY_BATCHES", (("samples", "ts", 10), ("rollup_10s", "slot", 1),
                                                         ("rollup_1m", "minute", 1), ("rollup_15m", "quarter", 1)))
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=now - 100 + k, success=True, latency_ms=1.0)
                               for t in (GW, ISP) for k in range(60)])
    await repo.flush_samples()
    await repo.save_path(1, [GW])
    transactions = 0
    original = repo._db.write

    def counting():
        nonlocal transactions
        transactions += 1
        return original()

    monkeypatch.setattr(repo._db, "write", counting)
    assert await repo.delete_path_history("isp_1")
    assert transactions >= 6  # 60 s of samples in 10 s batches, not one transaction for the lot
    isp = repo._target_ids["isp_1"]
    gw = repo._target_ids["gw"]
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM samples WHERE target = ?", (isp,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM samples WHERE target = ?", (gw,)).fetchone()[0] == 60  # the others stay
        assert db.execute("SELECT COUNT(*) FROM rollup_1m WHERE target = ?", (gw,)).fetchone()[0] > 0


async def test_a_hop_put_back_on_the_path_while_its_history_is_deleted_stops_the_delete(tmp_path, monkeypatch):
    from pengumon.db import path as path_module
    monkeypatch.setattr(path_module, "HISTORY_BATCHES", (("samples", "ts", 10),))
    repo = await _repo(tmp_path)
    await repo.import_path([GW, ISP])
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id="isp_1", timestamp=now - 100 + k, success=True, latency_ms=1.0)
                               for k in range(60)])
    await repo.flush_samples()
    await repo.save_path(1, [GW])
    original = repo._db.write
    calls = 0

    def putting_back():
        nonlocal calls
        calls += 1
        if calls == 3:  # between two batches the owner applies a path with the hop again
            with sqlite3.connect(repo.db_path) as db:
                db.execute("UPDATE path_targets SET deleted_at = NULL WHERE id = 'isp_1'")
        return original()

    monkeypatch.setattr(repo._db, "write", putting_back)
    assert not await repo.delete_path_history("isp_1")
    with sqlite3.connect(repo.db_path) as db:
        left = db.execute("SELECT COUNT(*) FROM samples WHERE target = ?", (repo._target_ids["isp_1"],)).fetchone()[0]
    assert 0 < left < 60  # stopped after the first batches; the rest of the history is kept
    assert "isp_1" not in await repo.removed_path_ids()


# --- TCP and HTTP hops: the policy, the cap, the round trip ----------------------------------------------------------

TCP = TargetConfig(id="cf_tcp", name="Cloudflare (TCP)", category="public", address="198.51.100.77", probe="tcp",
                   port=443)
SITE = TargetConfig(id="site", name="My site", category="custom", address="example.com", probe="http",
                    http_path="/health")


async def test_a_tcp_and_an_http_hop_go_through_put_live_and_stored_and_round_trip_through_export(path_app):
    ac, state = path_app
    hops = [_hop(GW, 0), _hop(TCP, 1), _hop(SITE, 2), _hop(PUB, 3)]
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": hops})
    assert res.status_code == 200, res.text
    by_id = {h["id"]: h for h in res.json()["hops"]}
    assert (by_id["cf_tcp"]["probe"], by_id["cf_tcp"]["port"]) == ("tcp", 443)
    assert (by_id["site"]["probe"], by_id["site"]["http_path"], by_id["site"]["http_verb"]) == ("http", "/health", "HEAD")
    assert [(t.id, t.probe) for t in state.config.targets] == [("gw", "icmp"), ("cf_tcp", "tcp"), ("site", "http"),
                                                               ("pub", "icmp")]
    _, stored = await state.repository.load_path()
    assert [t for t in stored if t.id in ("cf_tcp", "site")] == [TCP, SITE]
    exported = (await ac.get("/api/path/export")).text
    assert "probe: tcp" in exported and "port: 443" in exported and "http_path: /health" in exported
    plain = export_yaml([GW, ISP, PUB])  # a ping hop exports as before: none of the new keys
    assert not any(key in plain for key in ("probe:", "port:", "http_"))
    again = await ac.post("/api/path/import", content=exported.encode())
    assert again.status_code == 200, again.text
    assert [(h["id"], h["probe"], h["port"]) for h in again.json()["hops"]] == [
        ("gw", "icmp", None), ("cf_tcp", "tcp", 443), ("site", "http", None), ("pub", "icmp", None)]


@pytest.mark.parametrize("hop, status, says", [
    (dict(TCP.model_dump(), port=None), 422, "needs a port"),
    (dict(TCP.model_dump(), port=25), 422, "port 25 is refused"),
    (dict(SITE.model_dump(), category="isp"), 422, "role custom"),
    (dict(SITE.model_dump(), http_path="/health?x=1"), 422, "query string"),
    (dict(TCP.model_dump(), address="127.0.0.1"), 400, "loopback address"),
    (dict(TCP.model_dump(), address="::ffff:127.0.0.1"), 400, "special-purpose"),
    (dict(TCP.model_dump(), address="169.254.169.254"), 400, "link-local"),
    (dict(SITE.model_dump(), address="localhost"), 400, "loopback name"),
])
async def test_put_refuses_a_bad_socket_hop_and_keeps_the_live_path(path_app, hop, status, says):
    ac, state = path_app
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": [_hop(GW, 0), {**hop, "position": 1}]})
    assert res.status_code == status and says in res.text, res.text
    assert [t.id for t in state.config.targets] == ["gw", "pub"]


async def test_nine_socket_hops_are_refused_and_eight_are_not(path_app):
    ac, state = path_app

    def many(n):
        return [_hop(TCP.model_copy(update={"id": f"t{i}", "address": f"198.51.100.{i + 10}"}), i) for i in range(n)]
    res = await ac.put("/api/path", json={"base_revision": 1, "hops": many(9)})
    assert res.status_code == 400 and "At most 8" in res.text
    assert (await ac.put("/api/path", json={"base_revision": 1, "hops": many(8)})).status_code == 200


async def test_the_other_ways_in_refuse_a_loopback_socket_hop_and_a_query_too(path_app):
    ac, state = path_app
    assert (await ac.post("/api/targets", json={**TCP.model_dump(), "address": "127.0.0.1"})).status_code == 400
    assert (await ac.post("/api/targets", json=TCP.model_dump())).status_code == 200
    loop = export_yaml([TCP.model_copy(update={"address": "127.0.0.1"})])
    assert (await ac.post("/api/path/import", content=loop.encode())).status_code == 422
    query = export_yaml([SITE]).replace("http_path: /health", "http_path: /health?x=1")
    res = await ac.post("/api/path/import", content=query.encode())
    assert res.status_code == 422 and "query string" in res.text
    assert [t.id for t in state.config.targets if t.id == "cf_tcp"] == ["cf_tcp"]  # the POST added it


def test_the_socket_policy_words():
    from pengumon.engine.probe_rules import refused_address, socket_hop_problem
    assert refused_address("127.0.0.1") == "a loopback address" and refused_address("::1") == "a loopback address"
    assert refused_address("LocalHost") == "a loopback name" and refused_address("a.localhost") == "a loopback name"
    assert refused_address("0.0.0.0") and refused_address("224.0.0.1") and refused_address("fe80::1%eth0")
    for fine in ("198.51.100.1", "192.168.88.1", "10.0.0.1", "100.64.0.1", "example.com", "fd00::1", "localhost.example"):
        assert refused_address(fine) is None, fine
    assert socket_hop_problem([GW.model_copy(update={"address": "127.0.0.1"})]) is None  # a ping hop is not the policy's


@pytest.mark.parametrize("hidden", ["64:ff9b::a9fe:a9fe", "::127.0.0.1", "::ffff:0:7f00:1", "64:ff9b::7f00:1"])
def test_ipv6_ranges_that_carry_an_ipv4_address_are_special_and_refused(hidden):
    """NAT64, IPv4-compatible and IPv4-translated addresses are not the host they look like."""
    from pengumon.engine.discovery.addresses import SPECIAL, address_class
    from pengumon.engine.probe_rules import refused_address
    assert address_class(hidden) == SPECIAL
    assert refused_address(hidden) == "a special-purpose address"


async def test_the_test_route_refuses_a_loopback_tcp_hop_and_a_bad_probe_and_keeps_the_echo_answer(path_app):
    ac, state = path_app
    res = await ac.post("/api/path/test", json={"address": "127.0.0.1", "probe": "tcp", "port": 443})
    assert res.status_code == 400 and "loopback address" in res.text
    assert (await ac.post("/api/path/test", json={"address": "169.254.169.254", "probe": "http"})).status_code == 400
    assert (await ac.post("/api/path/test", json={"address": "198.51.100.9", "probe": "tcp"})).status_code == 422  # no port
    assert (await ac.post("/api/path/test", json={"address": "198.51.100.9", "probe": "tcp", "port": 25})).status_code == 422
    assert (await ac.post("/api/path/test", json={"address": "198.51.100.9", "probe": "nope"})).status_code == 422
    assert state.prober._backend.sent == []
    res = await ac.post("/api/path/test", json={"address": "198.51.100.9"})
    assert res.json() == {"kind": "reply", "rtt_ms": 1.0, "responder": "198.51.100.9", "address": "198.51.100.9",
                          "error": None}  # unchanged: no status, no connect_ms


async def test_the_test_route_probes_a_loopback_listener_when_the_policy_is_off(path_app, monkeypatch):
    ac, state = path_app
    requests = []

    async def handle(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr("pengumon.api.path.refused_address", lambda address: None)
    state.prober.socket_policy = lambda address: None
    try:
        tcp = (await ac.post("/api/path/test", json={"address": "127.0.0.1", "probe": "tcp", "port": port})).json()
        http = (await ac.post("/api/path/test", json={"address": "127.0.0.1", "probe": "http", "port": port,
                                                        "http_scheme": "http", "http_path": "/x"})).json()
    finally:
        server.close()
        await server.wait_closed()
    assert tcp["kind"] == "reply" and tcp["rtt_ms"] > 0 and tcp["status"] is None and tcp["connect_ms"] is None
    assert http["kind"] == "reply" and http["status"] == 204 and 0 < http["connect_ms"] <= http["rtt_ms"]
    assert set(http) == {"kind", "rtt_ms", "responder", "address", "error", "status", "connect_ms"}
    assert requests[0].startswith(b"HEAD /x HTTP/1.1\r\n")
    assert state.prober._backend.sent == []  # no echo request went out for either


async def test_the_test_route_names_a_name_that_resolves_to_the_host_itself(path_app, monkeypatch):
    ac, _ = path_app

    async def resolve(host, timeout, family=4):
        return "127.0.0.1", 4
    monkeypatch.setattr("pengumon.engine.prober.resolve_address", resolve)
    res = await ac.post("/api/path/test", json={"address": "rebind.example.com", "probe": "tcp", "port": 443})
    assert res.status_code == 200 and res.json()["kind"] == "not_allowed"
    assert "resolves to 127.0.0.1, a loopback address" in res.json()["error"]
