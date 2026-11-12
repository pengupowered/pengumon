"""ServiceChecker: HTTP health checks of the other apps on this host, raising service_down incidents."""

import httpx

from pengumon.config import AppConfig
from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.services.checker import ServiceChecker
from pengumon.incidents.model import Incident
from pengumon.notify.notifier import Notifier
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


# --- bounds, deadlines and containment of one check ---

import asyncio  # noqa: E402

import pytest  # noqa: E402

from pengumon.services.checker import classify_error, display_url  # noqa: E402


class _Endless(httpx.AsyncByteStream):
    """A body that never ends (an event stream entered as the health URL)."""

    def __init__(self):
        self.sent = 0

    async def __aiter__(self):
        while True:
            self.sent += 1
            yield b"x" * 65536
            await asyncio.sleep(0)


async def test_an_endless_body_is_read_to_a_cap_and_the_check_finishes(tmp_path):
    repo = await make_repo(tmp_path)
    body = _Endless()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)))
    sc = ServiceChecker(checks(WT), repo, client_factory=lambda svc: client)
    await asyncio.wait_for(sc.poll(force=True), timeout=10)
    assert sc.state_of("app_a") == "up" and body.sent <= 3


async def test_a_check_that_never_answers_ends_at_its_deadline_and_others_still_run(tmp_path):
    repo = await make_repo(tmp_path)
    slow = ServiceCheckConfig(id="slow", name="Slow", url="http://127.0.0.1:9100/h", timeout_seconds=0.5)

    async def handler(request):
        if request.url.port == 9100:
            await asyncio.sleep(3600)  # httpx's per-operation timeouts never see this
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sc = ServiceChecker(checks(slow, WT), repo, client_factory=lambda svc: client)
    await asyncio.wait_for(sc.poll(force=True), timeout=10)
    assert sc.status["slow"].error.kind == "timeout" and sc.state_of("slow") == "degraded"
    assert sc.state_of("app_a") == "up"


async def test_one_service_raising_does_not_stop_the_others(tmp_path):
    repo = await make_repo(tmp_path)
    sc = checker(repo, Answers(wt=[200], cm=[200]), WT, CM)
    real = sc._check

    async def check(service):
        if service.id == "app_a":
            raise RuntimeError("unexpected")
        return await real(service)

    sc._check = check
    await sc.poll(force=True)
    assert sc.state_of("app_b") == "up" and sc.state_of("app_a") == "unknown"


async def test_a_result_for_a_service_removed_during_the_request_is_dropped(tmp_path):
    repo = await make_repo(tmp_path)
    cfg = checks(WT, failures=1)
    sc = ServiceChecker(cfg, repo, client_factory=lambda svc: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    real = sc._check

    async def check(service):
        result = await real(service)
        cfg.services.clear()  # removed while the request was in flight
        return result

    sc._check = check
    await sc.poll(force=True)
    assert "app_a" not in sc.status and await repo.get_recent_incidents() == []


def test_display_url_survives_a_port_above_65535():
    assert display_url("http://host:99999/health") == "host/health"
    assert display_url("http://host:8080/health?x=1") == "host:8080/health"


@pytest.mark.parametrize("url", ["http://host:99999/h", "http://host:0x/h"])
def test_a_service_url_with_a_bad_port_is_refused(url):
    with pytest.raises(ValueError):
        ServiceCheckConfig(id="a", name="A", url=url)
    with pytest.raises(ValueError):
        ServiceCheckConfig(id="a", name="A", url="http://host/h", open_url=url)


def test_service_text_fields_are_bounded():
    with pytest.raises(ValueError):
        ServiceCheckConfig(id="a", name="A", url="http://host/h", description="x" * 1001)
    with pytest.raises(ValueError):
        ServiceCheckConfig(id="a" * 65, name="A", url="http://host/h")


def test_error_text_from_the_remote_side_is_one_short_line():
    err = classify_error(httpx.ReadError("bad\nINFO forged line\r\n" + "y" * 500))
    assert "\n" not in err.message and "\r" not in err.message and len(err.message) < 260


def test_checks_ignore_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    from pengumon.services.checker import _client_for
    client = _client_for(WT)
    assert not client._trust_env


class _Once(httpx.AsyncByteStream):
    """A body handed over as the client reads it (httpx.Response(content=...) with a Content-Encoding inflates it at once)."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


async def test_a_compressed_answer_is_counted_after_decoding_and_decoded_only_to_the_cap(tmp_path):
    """The check asks for no compression, and a gzip bomb from the host (a few hundred KB that inflate to
    gigabytes) is cut at the cap while it is decoded, not after httpx inflated a whole piece."""
    import tracemalloc
    import zlib
    z = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    bomb = b"".join([z.compress(bytes(1 << 20)) for _ in range(64)] + [z.flush()])
    assert len(bomb) < 128 * 1024
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Once(bomb))

    repo = await make_repo(tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sc = ServiceChecker(checks(WT), repo, client_factory=lambda svc: client)
    tracemalloc.start()
    try:
        await sc.poll(force=True)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert sc.state_of("app_a") == "up"
    assert seen[0].headers["accept-encoding"] == "identity"
    assert peak < 8 * 1024 * 1024
