"""The discovery draft against the live path (ids, statuses, positions), the job store (single flight, kept for a
while, bounded) and POST /api/discovery, GET /api/discovery/{id}."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from discovery_fakes import discover, three_paths
from pengumon.state import STATE_ATTR, AppState
from pengumon.components import Context
from pengumon.engine.discovery import component, jobs as jobs_module
from pengumon.engine.discovery.draft import MAX_HOPS, diff_against, hop_id, trim
from pengumon.engine.discovery.engine import DiscoveryOptions, DiscoveryResult, live_path_from_targets
from pengumon.engine.discovery.jobs import DiscoveryJobs
from pengumon.engine.netinfo import default_targets
from pengumon.main import app, security_config
from pengumon.models.probes import TargetConfig

NO_ASN = DiscoveryOptions(asn_lookup=False)
HOME_PATH = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0), ("203.0.113.2", 10.0)],
                        to_1111=[("198.51.100.7", 12.0)])


def statuses(result):
    return [(h["id"], h["address"], h["status"]) for h in result.hops]


# --- the draft and its diff -----------------------------------------------------------------------------------------

async def test_first_run_against_the_default_targets_keeps_their_ids():
    live = live_path_from_targets(default_targets("192.168.1.1"))
    result = await discover(HOME_PATH, options=NO_ASN, live=live)
    assert statuses(result) == [
        ("local_router", "192.168.1.1", "same"), ("hop_203_0_113_1", "203.0.113.1", "new"),
        ("hop_203_0_113_2", "203.0.113.2", "new"), ("hop_198_51_100_7", "198.51.100.7", "new"),
        ("cloudflare_dns", "1.1.1.1", "same"), ("google_dns", "8.8.8.8", "same"), ("quad9_dns", "9.9.9.9", "new")]
    router = result.hops[0]
    assert router["name"] == "Router"  # the default name gives way to discovery's
    assert [h["position"] for h in result.hops] == list(range(7))
    assert {h["group"] for h in result.hops[4:]} == {"internet"}


async def test_rediscover_keeps_the_owners_choices_and_marks_what_changed_and_what_is_gone():
    live = [
        {"id": "my_router", "name": "Living room", "address": "192.168.1.1", "category": "local", "enabled": True,
         "position": 0, "group": None, "color": "#112233", "device_id": "router"},
        {"id": "isp_first", "name": "My ISP", "address": "203.0.113.1", "category": "gateway", "enabled": True,
         "position": 1, "group": None, "color": None, "device_id": None},
        {"id": "old_hop", "name": "Old provider hop", "address": "203.0.113.77", "category": "isp", "enabled": True,
         "position": 2, "group": None, "color": None, "device_id": None},
        {"id": "cloudflare_dns", "name": "Cloudflare", "address": "1.1.1.1", "category": "public", "enabled": False,
         "position": 3, "group": "internet", "color": None, "device_id": None},
        {"id": "nas", "name": "NAS", "address": "nas.home.arpa", "category": "custom", "enabled": True,
         "position": 4, "group": None, "color": None, "device_id": None},
        {"id": "resolver", "name": "Resolver", "address": "192.0.2.53", "category": "dns", "enabled": True,
         "position": 5, "group": None, "color": None, "device_id": None,
         "record_type": "A", "nameserver": "192.0.2.53"},
    ]
    result = await discover(HOME_PATH, options=NO_ASN, live=live)
    assert statuses(result) == [
        ("my_router", "192.168.1.1", "same"), ("isp_first", "203.0.113.1", "changed"),
        ("old_hop", "203.0.113.77", "gone"), ("hop_203_0_113_2", "203.0.113.2", "new"),
        ("hop_198_51_100_7", "198.51.100.7", "new"), ("cloudflare_dns", "1.1.1.1", "same"),
        ("google_dns", "8.8.8.8", "new"), ("quad9_dns", "9.9.9.9", "new"),
        ("nas", "nas.home.arpa", "same"), ("resolver", "192.0.2.53", "same")]
    hops = {h["id"]: h for h in result.hops}
    assert (hops["my_router"]["name"], hops["my_router"]["color"], hops["my_router"]["device_id"]) == \
        ("Living room", "#112233", "router")
    assert (hops["isp_first"]["name"], hops["isp_first"]["category"]) == \
        ("My ISP", "isp")  # the owner's name stays; the role is discovery's proposal
    assert hops["cloudflare_dns"]["enabled"] is False
    assert (hops["resolver"]["nameserver"], hops["resolver"]["record_type"]) == ("192.0.2.53", "A")
    assert [h["position"] for h in result.hops] == list(range(len(result.hops)))


async def test_a_home_hop_off_the_traced_path_is_kept_while_it_answers_ping():
    """A cable modem in bridge mode is never in a trace; while it answers ping it is not "gone"."""
    live = [
        {"id": "router", "name": "Router", "address": "192.168.1.1", "category": "local", "position": 0},
        {"id": "modem", "name": "Modem", "address": "192.168.100.1", "category": "gateway", "position": 1},
        {"id": "old_isp", "name": "Old", "address": "203.0.113.77", "category": "isp", "position": 2},
    ]
    kept = await discover(HOME_PATH, options=NO_ASN, live=live, answers_ping={"192.168.100.1", "203.0.113.77"})
    assert [(h["id"], h["status"]) for h in kept.hops[:4]] == [
        ("router", "same"), ("modem", "same"), ("old_isp", "gone"), ("hop_203_0_113_1", "new")]
    # the gateway addresses off the traced path get one ping each; the live modem is one of them, so it is found
    # there (and not pinged again as a missing home hop)
    assert kept.pinged == ["10.0.0.1", "192.168.0.1", "192.168.100.1"]
    gone = await discover(HOME_PATH, options=NO_ASN, live=live)
    assert [(h["id"], h["status"]) for h in gone.hops[:3]] == [("router", "same"), ("modem", "gone"), ("old_isp", "gone")]


async def test_a_live_home_hop_off_the_traced_path_is_pinged_before_it_is_called_gone():
    """A home hop at an address discovery does not ask by itself (not one of the gateway addresses)."""
    live = [
        {"id": "router", "name": "Router", "address": "192.168.1.1", "category": "local", "position": 0},
        {"id": "bridge", "name": "Bridge", "address": "192.168.88.2", "category": "gateway", "position": 1},
        {"id": "old_isp", "name": "Old", "address": "203.0.113.77", "category": "isp", "position": 2},
    ]
    kept = await discover(HOME_PATH, options=NO_ASN, live=live, answers_ping={"192.168.88.2", "203.0.113.77"})
    assert [(h["id"], h["status"]) for h in kept.hops[:3]] == [("router", "same"), ("bridge", "same"),
                                                               ("old_isp", "gone")]
    assert kept.pinged[-1:] == ["192.168.88.2"] and "203.0.113.77" not in kept.pinged  # only home hops
    assert any("answering ping" in n for n in kept.notes)
    gone = await discover(HOME_PATH, options=NO_ASN, live=live)
    assert [(h["id"], h["status"]) for h in gone.hops[:2]] == [("router", "same"), ("bridge", "gone")]


async def test_ids_are_stable_for_an_address_across_runs():
    first = await discover(HOME_PATH, options=NO_ASN)
    second = await discover(HOME_PATH, options=NO_ASN)
    assert [h["id"] for h in first.hops] == [h["id"] for h in second.hops]
    assert hop_id("2001:DB8::1") == hop_id("2001:db8:0::1") == "hop_2001_db8__1"
    assert all(len(h["id"]) <= 64 for h in first.hops)


def test_a_new_hops_id_never_takes_a_live_id():
    proposed = [{"id": "hop_203_0_113_1", "name": "x", "address": "203.0.113.1", "category": "isp",
                 "evidence": [], "confidence": "likely", "suggested_driver": None, "status": "new"}]
    live = [{"id": "hop_203_0_113_1", "name": "y", "address": "192.0.2.200", "category": "custom", "position": 0}]
    draft = diff_against(proposed, live)
    assert [(h["id"], h["status"]) for h in draft] == [("hop_203_0_113_1_2", "new"), ("hop_203_0_113_1", "same")]


def test_a_moved_hop_is_changed():
    def hop(address, category="isp"):
        return {"id": hop_id(address), "name": address, "address": address, "category": category, "evidence": [],
                "confidence": "likely", "suggested_driver": None, "status": "new"}

    live = [dict(hop("203.0.113.1"), position=0), dict(hop("203.0.113.2"), position=1)]
    draft = diff_against([hop("203.0.113.2"), hop("203.0.113.1")], live)
    assert [h["status"] for h in draft] == ["changed", "changed"]


def test_a_draft_longer_than_the_limit_drops_internet_hops_nearest_the_servers():
    hops = [{"id": f"h{n}", "address": f"198.51.100.{n}", "category": "public", "group": None, "status": "new"}
            for n in range(MAX_HOPS + 5)]
    hops.append({"id": "cloudflare_dns", "address": "1.1.1.1", "category": "public", "group": "internet",
                 "status": "new"})
    out = trim(hops)
    assert len(out) == MAX_HOPS and out[-1]["id"] == "cloudflare_dns" and out[-2]["id"] == f"h{MAX_HOPS - 2}"
    assert [h["position"] for h in out] == list(range(MAX_HOPS))


def test_the_live_path_from_the_probe_targets():
    targets = [TargetConfig(id="r", name="R", category="local", address="192.168.1.1"),
               TargetConfig(id="cloudflare_dns", name="C", category="public", address="1.1.1.1")]
    live = live_path_from_targets(targets)
    assert [(h["id"], h["position"], h["group"]) for h in live] == [("r", 0, None), ("cloudflare_dns", 1, "internet")]


# --- the job store --------------------------------------------------------------------------------------------------

class Gate:
    """A runner that waits until released, so a test sees a run in progress."""

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def __call__(self, options, progress):
        self.calls.append(options)
        progress("Tracing the path to 1.1.1.1 (1 of 3)")
        await self.release.wait()
        return DiscoveryResult(hops=[{"id": "x"}], notes=["n"], path=None, facts=None, inference=None)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_single_flight_and_results_kept_for_ten_minutes():
    now = [1000.0]
    gate = Gate()
    store = DiscoveryJobs(gate, clock=lambda: now[0])
    first = store.start(DiscoveryOptions())
    await _settle()
    assert store.start(DiscoveryOptions(snmp=True)) == first  # the running one
    assert len(gate.calls) == 1
    running = store.get(first)
    assert (running["state"], running["progress"], running["draft"]) == ("running", "Tracing the path to 1.1.1.1 (1 of 3)",
                                                                          {"hops": []})
    gate.release.set()
    await _settle()
    done = store.get(first)
    assert (done["state"], done["draft"], done["notes"]) == ("done", {"hops": [{"id": "x"}]}, ["n"])
    now[0] += 300
    second = store.start(DiscoveryOptions())
    assert second != first  # a finished run does not block a new one
    await _settle()
    now[0] = 1000.0 + jobs_module.KEEP_SECONDS + 1
    assert store.get(first) is None and store.get(second) is not None  # the second finished later


async def test_the_store_keeps_a_bounded_number_of_runs():
    gate = Gate()
    gate.release.set()
    store = DiscoveryJobs(gate)
    ids = []
    for _ in range(jobs_module.MAX_KEPT + 4):
        ids.append(store.start(DiscoveryOptions()))
        await _settle()
    kept = [i for i in ids if store.get(i) is not None]
    assert kept == ids[-jobs_module.MAX_KEPT:]


async def test_a_failed_run_says_so_and_a_new_one_can_start():
    async def broken(options, progress):
        raise RuntimeError("no")

    store = DiscoveryJobs(broken)
    job = store.start(DiscoveryOptions())
    await _settle()
    assert (store.get(job)["state"], store.get(job)["progress"]) == ("failed", "Discovery failed: RuntimeError")
    assert store.start(DiscoveryOptions()) != job


async def test_closing_the_store_stops_a_running_discovery():
    gate = Gate()
    store = DiscoveryJobs(gate)
    job = store.start(DiscoveryOptions())
    await _settle()
    await store.close()
    assert store.get(job)["state"] == "failed"


async def test_the_component_makes_the_store_and_stops_it():
    state = AppState()
    comp = await component.register(Context(app=app, config=None, state=state))
    assert isinstance(state.discovery, DiscoveryJobs) and comp.name == "discovery"
    await comp.stop()


# --- the API --------------------------------------------------------------------------------------------------------

@pytest.fixture
def api_state(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    state = AppState()
    monkeypatch.setattr(app.state, STATE_ATTR, state, raising=False)
    return state


def _api():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_api_starts_one_run_and_reports_it(api_state):
    gate = Gate()
    api_state.discovery = DiscoveryJobs(gate)
    async with _api() as ac:
        started = await ac.post("/api/discovery", json={"asn_lookup": False, "snmp": True})
        again = await ac.post("/api/discovery")
        assert started.status_code == 200 and again.json() == started.json()
        job_id = started.json()["id"]
        assert gate.calls == [DiscoveryOptions(asn_lookup=False, snmp=True, device_checks=True)]
        running = (await ac.get(f"/api/discovery/{job_id}")).json()
        assert running["state"] == "running" and running["draft"] == {"hops": []}
        gate.release.set()
        await _settle()
        done = (await ac.get(f"/api/discovery/{job_id}")).json()
        assert (done["state"], done["draft"]["hops"], done["notes"]) == ("done", [{"id": "x"}], ["n"])
        assert (await ac.get("/api/discovery/nope")).status_code == 404
        assert (await ac.get("/api/discovery/" + "x" * 65)).status_code == 422
        assert (await ac.post("/api/discovery", json={"asn_lookup": True, "apply": True})).status_code == 422


async def test_the_api_defaults_to_the_asn_lookup_and_device_checks_on_and_snmp_off(api_state):
    gate = Gate()
    gate.release.set()
    api_state.discovery = DiscoveryJobs(gate)
    async with _api() as ac:
        assert (await ac.post("/api/discovery")).status_code == 200
    await _settle()
    assert gate.calls == [DiscoveryOptions(asn_lookup=True, device_checks=True, snmp=False)]


async def test_the_api_is_rate_limited_and_answers_503_before_the_store_exists(api_state):
    async with _api() as ac:
        assert (await ac.post("/api/discovery")).status_code == 503
        assert (await ac.get("/api/discovery/x")).status_code == 503
        gate = Gate()
        gate.release.set()
        api_state.discovery = DiscoveryJobs(gate)
        codes = [(await ac.post("/api/discovery")).status_code for _ in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429  # 6 a minute, the 503 above counted


async def test_the_api_refuses_a_cross_site_start(api_state, monkeypatch):
    monkeypatch.setattr(security_config, "allowed_origins", ["http://test"])
    api_state.discovery = DiscoveryJobs(Gate())
    async with _api() as ac:
        response = await ac.post("/api/discovery", headers={"Origin": "http://evil.example.com"})
    assert response.status_code == 403


async def test_the_first_run_dashboard_starts_discovery_once_per_server_process(api_state):
    gate = Gate()
    gate.release.set()
    api_state.discovery = DiscoveryJobs(gate)
    async with _api() as ac:
        first = (await ac.post("/api/discovery", json={"asn_lookup": False, "first_run": True})).json()["id"]
        await _settle()
        again = (await ac.post("/api/discovery", json={"asn_lookup": False, "first_run": True})).json()["id"]
        assert first and again == first and len(gate.calls) == 1  # a later dashboard load shows the finished run
        assert (await ac.get(f"/api/discovery/{first}")).json()["state"] == "done"
        # the owner's own runs are not limited
        assert (await ac.post("/api/discovery")).json()["id"] != first
        await _settle()
        assert len(gate.calls) == 2
        # ...and a first-run call never starts another one, even once the stored run is gone
        api_state.discovery._jobs.clear()
        assert (await ac.post("/api/discovery", json={"first_run": True})).json() == {"id": None}
        assert len(gate.calls) == 2
