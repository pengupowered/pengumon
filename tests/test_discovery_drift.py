"""Route-change checks (engine/discovery/drift.py, GET /api/path/drift): discovery rerun in the background against
the applied path, the changes that count (new, gone and moved home, line and provider hops), confirmation by a second
run, one notification per set of changes, dismissing, the stored result and its draft, and when checks run."""

import asyncio
import json
import random

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from discovery_fakes import FakeDns, FakeLan, three_paths
from pengumon.api.state import AppState
from pengumon.components import Context
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.discovery import component, drift as drift_module
from pengumon.engine.discovery.draft import diff_against
from pengumon.engine.discovery.drift import DriftChecker, notice_line, route_changes
from pengumon.engine.discovery.engine import Deps, live_path_from_targets, run_discovery
from pengumon.engine.discovery.jobs import KEEP_SECONDS, DiscoveryJobs
from pengumon.engine.discovery.settings import DiscoverySettings
from pengumon.main import app
from pengumon.models.probes import TargetConfig
from pengumon.notify import Notifier
from pengumon.notify_settings import WebhookConfig
from pengumon.webhook import WebhookSender

ROUTER = TargetConfig(id="router", name="Router", address="192.168.1.1", category="local")
ISP_1 = TargetConfig(id="isp_1", name="Provider hop 1", address="203.0.113.1", category="isp")
ISP_2 = TargetConfig(id="isp_2", name="Provider hop 2", address="203.0.113.2", category="isp")
SERVERS = [TargetConfig(id="cloudflare_dns", name="Cloudflare", address="1.1.1.1", category="public", group="internet"),
           TargetConfig(id="google_dns", name="Google", address="8.8.8.8", category="public", group="internet"),
           TargetConfig(id="quad9_dns", name="Quad9", address="9.9.9.9", category="public", group="internet")]

ROUTE_A = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0), ("203.0.113.2", 10.0)])
# the provider renumbered (or rerouted): its second hop is another router now
ROUTE_B = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0), ("203.0.113.3", 10.0)])
# internet hops past the provider differ from one trace to the next: never a route change
ROUTE_A_OTHER_INTERNET = three_paths(shared=[("192.168.1.1", 0.6), ("203.0.113.1", 9.0), ("203.0.113.2", 10.0)],
                                     to_1111=[("198.51.100.200", 12.0)])
DOWN = {}


class Network:
    """The route discovery traces, which can change between runs; and the applied path."""

    def __init__(self, traces, targets, removed=(), revision=2):
        self.traces = traces
        self.targets = list(targets)
        self.removed = list(removed)
        self.revision = revision
        self.runs = []

    async def paths(self):
        return self.revision, live_path_from_targets(self.targets), live_path_from_targets(self.removed)

    async def run(self, options):
        self.runs.append(options)
        traces = self.traces

        async def trace(host):
            return list(traces.get(host, []))

        async def ssdp():
            return []

        async def ping(address):
            return False

        async def snmp(address):
            return None

        deps = Deps(trace=trace, query=FakeDns(), http_client=FakeLan().client, ssdp=ssdp, snmp_get=snmp,
                    gateway=lambda: None, ping=ping, ipv6=lambda: "no IPv6 route")

        async def live():
            _, hops, removed = await self.paths()
            return hops, removed

        return await run_discovery(options, live, deps)


class MemoryRepo:
    def __init__(self):
        self.saved = None

    async def load_path_drift(self):
        return json.loads(self.saved) if self.saved else None

    async def save_path_drift(self, drift):
        self.saved = json.dumps(drift)


def checker(net, repo=None, jobs=None, sleep=None, **kw):
    sent = []

    async def no_wait(seconds):
        return None

    drift = DriftChecker(run=net.run, paths=net.paths, repository=repo or MemoryRepo(), jobs=jobs,
                         notify=sent.append, sleep=sleep or no_wait, **kw)
    return drift, sent


def changes(public):
    return [(c["kind"], c["hop"]["address"]) for c in public["changes"]]


APPLIED = [ROUTER, ISP_1, ISP_2, *SERVERS]


# --- what counts as a change --------------------------------------------------------------------------------------------

async def test_the_same_route_is_no_change_and_internet_hops_past_the_provider_never_are():
    for traces in (ROUTE_A, ROUTE_A_OTHER_INTERNET):
        drift, sent = checker(Network(traces, APPLIED))
        public = await drift.check()
        assert (public["state"], public["changes"]) == ("same", []) and sent == []


async def test_a_new_provider_hop_and_a_gone_one_are_reported_in_plain_words():
    net = Network(ROUTE_B, APPLIED)
    drift, sent = checker(net)
    public = await drift.check()
    assert public["state"] == "changed" and public["discovery_id"]
    assert sorted(changes(public)) == [("gone", "203.0.113.2"), ("new", "203.0.113.3")]
    texts = {c["kind"]: c["text"] for c in public["changes"]}
    assert texts["new"].startswith("Hop 3 is new: ") and "(203.0.113.3)" in texts["new"]
    assert texts["gone"] == "Provider hop 2 (203.0.113.2) is no longer on the route."
    assert {c["hop"]["category"] for c in public["changes"]} == {"isp"}
    assert len(net.runs) == 2  # confirmed by a second run
    assert all(not o.device_checks and o.off_path_gateways and not o.snmp and not o.asn_lookup for o in net.runs)


def test_a_hop_the_owner_removed_is_no_news_and_the_role_alone_is_not_a_change():
    live = live_path_from_targets([ROUTER, ISP_1])
    removed = live_path_from_targets([ISP_2])
    proposed = [
        {"id": "hop_192_168_1_1", "name": "Router", "address": "192.168.1.1", "category": "local", "evidence": [],
         "confidence": "likely", "suggested_driver": None, "status": "new"},
        {"id": "hop_203_0_113_1", "name": "Gateway", "address": "203.0.113.1", "category": "gateway",
         "evidence": [], "confidence": "likely", "suggested_driver": None, "status": "new"},
        {"id": "hop_203_0_113_2", "name": "P", "address": "203.0.113.2", "category": "isp", "evidence": [],
         "confidence": "likely", "suggested_driver": None, "status": "new"},
    ]
    draft = diff_against(proposed, live, removed=removed)
    assert [(h["id"], h["status"]) for h in draft] == [("router", "same"), ("isp_1", "changed"), ("isp_2", "new")]
    assert route_changes(draft, live, removed) == []


def test_a_hop_that_moved_among_the_others_is_changed():
    live = live_path_from_targets([ROUTER, ISP_1, ISP_2])

    def proposed(address, category):
        return {"id": address, "name": address, "address": address, "category": category, "evidence": [],
                "confidence": "likely", "suggested_driver": None, "status": "new"}

    draft = diff_against([proposed("192.168.1.1", "local"), proposed("203.0.113.2", "isp"),
                          proposed("203.0.113.1", "isp")], live)
    found = route_changes(draft, live)
    assert [(c["kind"], c["hop"]["id"]) for c in found] == [("changed", "isp_1")]
    assert found[0]["text"] == "Provider hop 1 (203.0.113.1) moved: it is hop 3 now."


def test_notification_lines_carry_no_addresses():
    lines = [notice_line({"kind": k, "hop": {"name": "Provider hop 2", "address": "203.0.113.3"}})
             for k in ("new", "gone", "changed")]
    assert lines == ["A new hop on the route: Provider hop 2.", "Provider hop 2 is no longer on the route.",
                     "Provider hop 2 moved to another place on the route."]


# --- confirmation, failures ---------------------------------------------------------------------------------------------

async def test_a_change_the_second_run_does_not_see_is_not_reported():
    net = Network(ROUTE_B, APPLIED)

    async def route_comes_back(seconds):
        net.traces = ROUTE_A

    drift, sent = checker(net, sleep=route_comes_back)
    public = await drift.check()
    assert (public["state"], public["changes"], sent) == ("same", [], [])
    assert len(net.runs) == 2


async def test_a_change_whose_confirming_run_cannot_reach_the_internet_is_not_reported():
    net = Network(ROUTE_B, APPLIED)

    async def connection_drops(seconds):
        net.traces = DOWN

    drift, sent = checker(net, sleep=connection_drops)
    public = await drift.check()
    assert (public["state"], public["changes"], sent) == ("failed", [], [])


async def test_a_check_while_the_connection_is_down_compares_nothing():
    drift, sent = checker(Network(DOWN, APPLIED))
    public = await drift.check()
    assert public["state"] == "failed" and public["changes"] == [] and "not compared" in public["error"]
    assert public["discovery_id"] is None and sent == []


async def test_a_failing_discovery_is_a_failed_check(caplog):
    net = Network(ROUTE_B, APPLIED)

    async def broken(options):
        raise RuntimeError("no")

    drift, _ = checker(net)
    drift._run = broken
    public = await drift.check()
    assert (public["state"], public["error"]) == ("failed", "Discovery failed: RuntimeError")


async def test_no_check_runs_before_a_path_is_applied():
    net = Network(ROUTE_B, [], revision=0)
    drift, _ = checker(net)
    assert (await drift.check())["state"] == "never" and net.runs == []


# --- announcing, dismissing, applying -----------------------------------------------------------------------------------

async def test_a_set_of_changes_is_announced_once_and_again_only_when_it_changes():
    net = Network(ROUTE_B, APPLIED)
    drift, sent = checker(net)
    await drift.check()
    assert len(sent) == 1 and len(sent[0]) == 2 and "203.0.113" not in " ".join(sent[0])
    await drift.check()
    assert len(sent) == 1  # the same set: not again
    net.traces = ROUTE_A
    assert (await drift.check())["state"] == "same"
    net.traces = ROUTE_B
    await drift.check()
    assert len(sent) == 2  # it came back: news again


async def test_dismissed_changes_are_not_shown_or_announced_again_until_they_go_away():
    net = Network(ROUTE_B, APPLIED)
    drift, sent = checker(net)
    await drift.check()
    dismissed = await drift.dismiss()
    assert (dismissed["state"], dismissed["changes"]) == ("same", [])
    await drift.check()
    assert (await drift.public())["state"] == "same" and len(sent) == 1
    net.traces = ROUTE_A
    await drift.check()  # gone: the dismissal is forgotten
    net.traces = ROUTE_B
    assert (await drift.check())["state"] == "changed" and len(sent) == 2


async def test_applying_the_new_route_settles_the_changes_without_a_new_check():
    net = Network(ROUTE_B, APPLIED)
    drift, _ = checker(net)
    public = await drift.check()
    new = next(c for c in public["changes"] if c["kind"] == "new")
    runs = len(net.runs)
    net.targets = [ROUTER, ISP_1, TargetConfig(id=new["hop"]["id"], name="Provider hop 2", address="203.0.113.3",
                                               category="isp"), *SERVERS]
    net.revision += 1
    assert (await drift.public())["state"] == "same"
    assert len(net.runs) == runs


async def test_the_review_draft_is_kept_under_its_id_and_survives_a_restart(tmp_path):
    now = [1000.0]
    jobs = DiscoveryJobs(None, clock=lambda: now[0])
    repo = DatabaseRepository(db_path=str(tmp_path / "d.db"), sample_flush_seconds=0)
    await repo.initialize()
    net = Network(ROUTE_B, APPLIED)
    drift, _ = checker(net, repo=repo, jobs=jobs)
    public = await drift.check()
    now[0] += KEEP_SECONDS * 10
    job = jobs.get(public["discovery_id"])
    assert job["state"] == "done" and job["background"] is True
    assert {h["address"] for h in job["draft"]["hops"]} >= {"203.0.113.3", "203.0.113.2"}
    # after a restart: the same result, its draft under the same id
    fresh_jobs = DiscoveryJobs(None)
    again, sent = checker(net, repo=repo, jobs=fresh_jobs)
    await again.load()
    assert (await again.public())["state"] == "changed"
    assert fresh_jobs.get(public["discovery_id"])["draft"]["hops"] == job["draft"]["hops"]
    await again.check()
    assert sent == []  # announced before the restart already
    await repo.close()


# --- when ----------------------------------------------------------------------------------------------------------

def test_the_daily_check_runs_at_one_minute_of_the_day():
    drift, _ = checker(Network(ROUTE_A, APPLIED), rng=random.Random(7))
    day = 86400 * 20000
    for now in (day, day + 1, day + 86399):
        at = drift.next_daily(now)
        assert now < at <= now + 86400 and (at - day) % 86400 == drift.minute * 60


async def test_the_daily_loop_waits_for_its_minute_then_checks():
    waits = []
    reasons = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) > 2:
            raise asyncio.CancelledError

    clock = [86400.0 * 100]
    drift, _ = checker(Network(ROUTE_A, APPLIED), sleep=sleep, clock=lambda: clock[0], rng=random.Random(3))

    async def check(reason="daily"):
        reasons.append(reason)

    drift.check = check
    with pytest.raises(asyncio.CancelledError):
        await drift.run_daily()
    assert reasons == ["daily", "daily"]
    assert waits[0] == drift.next_daily(clock[0]) - clock[0]


async def test_a_check_follows_an_incident_ten_minutes_later_at_most_once_an_hour():
    clock = [50_000.0]
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    net = Network(ROUTE_A, APPLIED)
    drift, _ = checker(net, sleep=sleep, clock=lambda: clock[0])
    drift.incident_ended()
    drift.incident_ended()  # one is waiting already
    await drift._incident_task
    assert slept == [drift_module.INCIDENT_DELAY_S] and len(net.runs) == 1
    clock[0] += 1000
    drift.incident_ended()  # less than an hour after that check
    assert drift._incident_task.done() and len(net.runs) == 1
    clock[0] += 3600
    drift.incident_ended()
    await drift._incident_task
    assert len(net.runs) == 2
    await drift.close()


# --- wiring ----------------------------------------------------------------------------------------------------------

class FakeClassifier:
    def __init__(self, callback):
        self.on_incident_resolved = callback


async def test_the_component_hooks_incident_end_after_the_notifier_and_stops_the_checker(tmp_path):
    heard = []
    state = AppState(repository=MemoryRepo(), classifier=FakeClassifier(lambda inc: heard.append(("notify", inc))))
    comp = await component.register(Context(app=app, config=AppConfig(), state=state))
    assert isinstance(state.drift, DriftChecker)
    assert state.drift.options.asn_lookup is False
    state.classifier.on_incident_resolved("incident")
    assert heard == [("notify", "incident")]
    assert state.drift._incident_task is not None and not state.drift._incident_task.done()
    await comp.stop()
    assert state.drift._incident_task.done()


async def test_background_checks_can_be_turned_off_and_the_asn_lookup_on():
    state = AppState(repository=MemoryRepo(), classifier=FakeClassifier(None))
    await component.register(Context(app=app, config=AppConfig(discovery={"background": False}), state=state))
    assert state.drift is None and state.classifier.on_incident_resolved is None
    state = AppState(repository=MemoryRepo())
    await component.register(Context(app=app, config=AppConfig(discovery={"asn_lookup": True}), state=state))
    assert state.drift.options.asn_lookup is True


def test_the_discovery_section_refuses_unknown_keys():
    with pytest.raises(ValueError):
        DiscoverySettings(backgrond=True)
    assert DiscoverySettings() == DiscoverySettings(background=True, asn_lookup=False)


async def test_the_drift_api(app_state):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.get("/api/path/drift")).json() == {"checked_at": None, "state": "never", "changes": [],
                                                            "discovery_id": None, "error": None}
        net = Network(ROUTE_B, APPLIED)
        app_state.drift, sent = checker(net)
        await app_state.drift.check()
        body = (await ac.get("/api/path/drift")).json()
        assert body["state"] == "changed" and {c["kind"] for c in body["changes"]} == {"new", "gone"}
        assert set(body["changes"][0]) == {"kind", "hop", "text"} and body["checked_at"]
        dismissed = await ac.post("/api/path/drift/dismiss")
        assert dismissed.status_code == 200 and dismissed.json()["state"] == "same"
        assert (await ac.get("/api/path/drift")).json()["state"] == "same"


# --- the notification ------------------------------------------------------------------------------------------------

async def test_a_route_change_notification_links_to_path_health():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    hook = WebhookSender(WebhookConfig(url="https://hooks.example.com/x", name="hook"), {},
                         "http://pengu.example.com:8000/")
    notifier = Notifier("http://ntfy.test/topic", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        webhooks=[hook], public_url="http://pengu.example.com:8000/")
    notifier.notify_path_changed(["A new hop on the route: Provider hop 2."])
    await asyncio.gather(*list(notifier._tasks))
    ntfy = next(r for r in requests if r.url.host == "ntfy.test")
    assert ntfy.headers["Click"] == "http://pengu.example.com:8000/#live"
    assert ntfy.headers["Title"] == "Pengumon: your provider's route changed"
    assert "Provider hop 2" in ntfy.content.decode()
    body = json.loads(next(r for r in requests if r.url.host == "hooks.example.com").content)
    assert (body["event"], body["link"]) == ("path_changed", "http://pengu.example.com:8000/#live")
    await notifier.aclose()


async def test_without_a_public_url_ntfy_gets_no_click_link():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200)

    notifier = Notifier("http://ntfy.test/topic", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    notifier.notify_path_changed(["x"])
    await asyncio.gather(*list(notifier._tasks))
    assert "Click" not in requests[0].headers
    await notifier.aclose()
