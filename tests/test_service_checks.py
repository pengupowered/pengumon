"""ServiceChecker: HTTP health checks of the other apps on this host, raising service_down incidents."""

import httpx

from pengumon.config import AppConfig
from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.services.checker import ServiceChecker
from pengumon.incidents.model import Incident
from pengumon.notify import Notifier
from pengumon.reports.common import _split_incidents


def checks(*services, failures=3):
    return ServiceChecksConfig(interval_seconds=30, failures_for_incident=failures, services=list(services))


WT = ServiceCheckConfig(id="app_a", name="App A", url="http://127.0.0.1:9000/api/health")
CM = ServiceCheckConfig(id="app_b", name="App B", url="https://127.0.0.1:9443/health")


class Answers:
    """MockTransport handler: per URL a queue of status codes, or an exception class to raise."""

    def __init__(self, **by_id):
        self.by_url = {WT.url: list(by_id.get("wt", [])), CM.url: list(by_id.get("cm", []))}
        self.seen = []

    def __call__(self, request):
        self.seen.append(str(request.url))
        answer = self.by_url[str(request.url)].pop(0)
        if isinstance(answer, type):
            raise answer("boom", request=request)
        return httpx.Response(answer, json={"status": "ok"})


def checker(repo, answers, *services, failures=3, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(answers))
    return ServiceChecker(checks(*services, failures=failures), repo, client_factory=lambda svc: client, **kw)


async def make_repo(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    return repo


async def test_incident_after_consecutive_failures_and_resolved_on_success(tmp_path):
    repo = await make_repo(tmp_path)
    started, resolved = [], []
    answers = Answers(wt=[200, 503, httpx.ConnectError, 503, 503, 200])
    sc = checker(repo, answers, WT, on_incident_started=started.append, on_incident_resolved=resolved.append)
    for _ in range(3):
        await sc.poll(force=True)
    assert await repo.get_recent_incidents() == [] and not started  # two failures so far
    await sc.poll(force=True)  # third failure in a row
    [inc] = await repo.get_recent_incidents()
    assert (inc.kind, inc.cause_code, inc.severity, inc.affected_targets, inc.resolved) == (
        "service_down", "service_unavailable", "critical", ["app_a"], False)
    assert inc.title == "App A is not responding" and "HTTP 503" in inc.description
    assert [i.id for i in started] == [inc.id]
    await sc.poll(force=True)  # still down: no second incident
    assert len(await repo.get_recent_incidents()) == 1
    await sc.poll(force=True)  # answers again
    [inc] = await repo.get_recent_incidents()
    assert inc.resolved and inc.end_time is not None
    assert [i.id for i in resolved] == [inc.id] and resolved[0].duration_seconds is not None
    assert sc.status["app_a"].ok is True and sc.status["app_a"].consecutive_failures == 0
    await repo.close()


async def test_services_are_independent_and_disabled_ones_are_skipped(tmp_path):
    repo = await make_repo(tmp_path)
    answers = Answers(wt=[200, 200], cm=[httpx.ConnectTimeout, httpx.ConnectTimeout])
    off = ServiceCheckConfig(id="off", name="Off", url="http://127.0.0.1:1/x", enabled=False)
    sc = checker(repo, answers, WT, CM, off, failures=2)
    await sc.poll(force=True)
    await sc.poll(force=True)
    [inc] = await repo.get_recent_incidents()
    assert inc.affected_targets == ["app_b"] and "ConnectTimeout" in inc.description
    assert "http://127.0.0.1:1/x" not in answers.seen and "off" not in sc.status
    await repo.close()


async def test_resolve_open_closes_incidents_on_shutdown(tmp_path):
    repo = await make_repo(tmp_path)
    sc = checker(repo, Answers(wt=[500]), WT, failures=1)
    await sc.poll(force=True)
    await sc.resolve_open()
    [inc] = await repo.get_recent_incidents()
    assert inc.resolved
    await repo.close()


async def test_a_bad_ca_file_counts_as_a_failed_check(tmp_path):
    repo = await make_repo(tmp_path)
    bad = ServiceCheckConfig(id="tls", name="TLS app", url="https://127.0.0.1:1/health",
                             ca_file=str(tmp_path / "missing.pem"))
    sc = ServiceChecker(checks(bad, failures=1), repo)
    await sc.poll(force=True)
    [inc] = await repo.get_recent_incidents()
    assert "missing.pem" in inc.description
    await sc.aclose()
    await repo.close()


def test_config_defaults_to_no_service_checks():
    cfg = AppConfig()
    assert cfg.service_checks.services == [] and cfg.service_checks.interval_seconds == 30


def test_network_reports_leave_service_incidents_out():
    rows = [{"kind": "gateway_down", "expected": 0}, {"kind": "busy_latency", "expected": 1},
            {"kind": "service_down", "expected": 0}]
    real, excluded = _split_incidents(rows)
    assert [r["kind"] for r in real] == ["gateway_down"] and excluded == 1


def _incident(target, iid):
    return Incident(id=iid, start_time=0, cause_code="service_unavailable", title=f"{target} is not responding",
                    description="d", severity="critical", affected_targets=[target], kind="service_down")


def test_notifier_leaves_service_incidents_to_the_grace_period_path():
    sent = []
    n = Notifier("http://ntfy.test/topic", client=httpx.AsyncClient())
    n._dispatch = lambda **kw: sent.append(kw)
    n.on_incident_started(_incident("app_a", 1))
    n.on_incident_resolved(_incident("app_a", 1))
    assert sent == []
