"""Host services: error classification, status states, the notification grace period, the incident kind filters,
/api/services, the metrics and the services config."""

import asyncio
import ssl
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from pengumon import __version__  # noqa: F401
from pengumon.live.ws import ConnectionManager
from pengumon.config import AppConfig
from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.services.checker import SERVICE_KIND, ServiceChecker, classify_error, display_url
from pengumon.main import app, security_config
from pengumon.observability.prometheus import render_metrics
from pengumon.incidents.model import Incident, SystemDiagnosis
from pengumon.notify.notifier import Notifier

from device_helpers import Clock

URL = "http://127.0.0.1:9000/api/health"
SVC = ServiceCheckConfig(id="app_a", name="App A", url=URL, open_url="http://host.lan:9000/",
                         description="The A app", ca_file="/secret/path/ca.pem")
QUIET = ServiceCheckConfig(id="quiet", name="Quiet", url="http://127.0.0.1:9001/h", notify=False)


# --- error classification -------------------------------------------------------------------------------------------

def _chain(outer, cause):
    outer.__cause__ = cause
    return outer


def test_an_empty_connect_error_is_named_by_its_type():
    err = classify_error(httpx.ConnectError(""))
    assert (err.kind, err.message) == ("unreachable", "ConnectError")


def test_refused_connection():
    err = classify_error(_chain(httpx.ConnectError(""), ConnectionRefusedError(111, "Connection refused")))
    assert (err.kind, err.message) == ("refused", "connection refused (stopped or restarting)")


def test_timeouts():
    for exc in (httpx.ConnectTimeout(""), httpx.ReadTimeout("slow")):
        err = classify_error(exc)
        assert err.kind == "timeout" and type(exc).__name__ in err.message


def test_tls_certificate_not_trusted_walks_the_cause_chain():
    ssl_err = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer")
    ssl_err.verify_message = "unable to get local issuer certificate"
    err = classify_error(_chain(httpx.ConnectError(""), _chain(OSError("wrapped"), ssl_err)))
    assert (err.kind, err.message) == ("tls", "certificate not trusted")


def test_tls_certificate_expired():
    ssl_err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    ssl_err.verify_message = "certificate has expired"
    assert classify_error(_chain(httpx.ConnectError("x"), ssl_err)).message == "certificate expired"


def test_dns_failure():
    import socket
    err = classify_error(_chain(httpx.ConnectError("[Errno -2] Name or service not known"),
                                socket.gaierror(-2, "Name or service not known")))
    assert err.kind == "dns" and "not found" in err.message


def test_other_transport_errors_are_unreachable_with_their_detail():
    err = classify_error(httpx.ReadError("connection reset by peer"))
    assert (err.kind, err.message) == ("unreachable", "connection reset by peer")


def test_display_url_drops_scheme_credentials_and_query():
    assert display_url("https://user:pw@127.0.0.1:9443/health?token=x") == "127.0.0.1:9443/health"
    assert display_url("http://[::1]:8080/") == "[::1]:8080/"


# --- the checker: states, latency, http errors, the grace period -----------------------------------------------------

class Script:
    """MockTransport handler: a queue of status codes or exceptions (instances or classes) for all urls."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, request):
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            a.request = request
            raise a
        return httpx.Response(a)


async def make_checker(tmp_path, script, *services, interval=30, failures=3, notify_after=300, **kw):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    clock = Clock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(script))
    cfg = ServiceChecksConfig(interval_seconds=interval, failures_for_incident=failures,
                              notify_after_seconds=notify_after, services=list(services))
    checker = ServiceChecker(cfg, repo, client_factory=lambda s: client, clock=clock, **kw)
    return checker, repo, clock


async def tick(checker, clock, seconds=30):
    clock.now += seconds
    await checker.poll()


async def test_states_up_degraded_down_and_back(tmp_path):
    script = Script(200, 503, httpx.ConnectError(""), 503, 200)
    checker, repo, clock = await make_checker(tmp_path, script, SVC)
    assert checker.state_of("app_a") == "unknown"
    states = []
    for _ in range(5):
        await tick(checker, clock)
        states.append(checker.state_of("app_a"))
    assert states == ["up", "degraded", "degraded", "down", "up"]


async def test_since_last_ok_status_code_error_and_failures(tmp_path):
    script = Script(200, 503, 503)
    checker, repo, clock = await make_checker(tmp_path, script, SVC)
    await tick(checker, clock)
    st = checker.status["app_a"]
    assert (st.since, st.last_ok, st.last_status_code, st.error) == (1030.0, 1030.0, 200, None)
    await tick(checker, clock)
    await tick(checker, clock)
    assert st.since == 1060.0  # when it stopped answering, not the later failed checks
    assert (st.last_ok, st.last_checked, st.consecutive_failures, st.last_status_code) == (1030.0, 1090.0, 2, 503)
    assert st.error.as_dict() == {"kind": "http", "message": "HTTP 503"}


async def test_latency_is_measured_per_check(tmp_path):
    ticks = iter([10.0, 10.25, 20.0, 20.5])
    script = Script(200, 500)
    checker, repo, clock = await make_checker(tmp_path, script, SVC, timer=lambda: next(ticks))
    await tick(checker, clock)
    assert checker.status["app_a"].latency_ms == pytest.approx(250.0)
    await tick(checker, clock)  # an HTTP error still got an answer
    assert checker.status["app_a"].latency_ms == pytest.approx(500.0)


async def test_no_latency_when_nothing_answered(tmp_path):
    checker, repo, clock = await make_checker(tmp_path, Script(httpx.ConnectTimeout("")), SVC)
    await tick(checker, clock)
    st = checker.status["app_a"]
    assert st.latency_ms is None and st.error.kind == "timeout"


async def test_incident_description_carries_the_clear_message_not_the_empty_exception(tmp_path):
    script = Script(*[httpx.ConnectError("")] * 3)
    checker, repo, clock = await make_checker(tmp_path, script, SVC)
    for _ in range(3):
        await tick(checker, clock)
    [inc] = await repo.get_recent_incidents()
    assert "ConnectError:" not in inc.description and inc.description.endswith(": ConnectError")
    assert "127.0.0.1:9000/api/health" in inc.description and "http://" not in inc.description


async def test_client_setup_failure_is_kind_config(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    bad = ServiceCheckConfig(id="tls", name="TLS", url="https://127.0.0.1:1/h", ca_file=str(tmp_path / "missing.pem"))
    checker = ServiceChecker(ServiceChecksConfig(services=[bad]), repo)
    await checker.poll()
    error = checker.status["tls"].error
    assert error.kind == "config"
    assert "missing.pem" in error.message and "Error" not in error.message  # a sentence, not the exception


async def test_no_announcement_before_the_grace_period_and_one_after(tmp_path):
    downs, ups = [], []
    script = Script(*[503] * 12, 200)
    checker, repo, clock = await make_checker(
        tmp_path, script, SVC, notify_after=300,
        on_service_down=lambda s, e, secs: downs.append((s.id, e.message, secs)),
        on_service_up=lambda s, secs: ups.append((s.id, secs)))
    for _ in range(10):  # failing for 0..270 s
        await tick(checker, clock)
    assert downs == []
    await tick(checker, clock)  # 300 s since the first failure
    await tick(checker, clock)
    assert downs == [("app_a", "HTTP 503", 300.0)]  # once
    assert ups == []
    await tick(checker, clock)
    assert len(downs) == 1 and ups == [("app_a", pytest.approx(360.0))]


async def test_a_short_restart_never_announces_down_or_up(tmp_path):
    downs, ups = [], []
    script = Script(200, 503, 503, 503, 200)
    checker, repo, clock = await make_checker(
        tmp_path, script, SVC, on_service_down=lambda *a: downs.append(a), on_service_up=lambda *a: ups.append(a))
    for _ in range(5):
        await tick(checker, clock)
    assert downs == [] and ups == []
    assert len(await repo.get_recent_incidents()) == 1  # but the outage is recorded as an incident


async def test_notify_false_never_announces(tmp_path):
    downs, ups = [], []
    script = Script(*[503] * 12, 200)
    checker, repo, clock = await make_checker(
        tmp_path, script, QUIET, on_service_down=lambda *a: downs.append(a), on_service_up=lambda *a: ups.append(a))
    for _ in range(13):
        await tick(checker, clock)
    assert downs == [] and ups == []
    assert checker.state_of("quiet") == "up"


async def test_service_incidents_do_not_reach_the_incident_notifier(tmp_path):
    sent = []
    notifier = Notifier("http://ntfy.test/t", client=httpx.AsyncClient())
    notifier._dispatch = lambda **kw: sent.append(kw)
    checker, repo, clock = await make_checker(
        tmp_path, Script(503, 503, 503, 200), SVC, on_incident_started=notifier.on_incident_started,
        on_incident_resolved=notifier.on_incident_resolved)
    for _ in range(4):
        await tick(checker, clock)
    assert sent == []


# --- the notifier's wording ------------------------------------------------------------------------------------------

def test_notifier_service_messages_are_default_priority_and_name_the_app():
    from pengumon.services.checker import ServiceError
    sent = []
    n = Notifier("http://ntfy.test/t", client=httpx.AsyncClient())
    n._dispatch = sent.append
    n.notify_service_down(SVC, ServiceError("refused", "connection refused (stopped or restarting)"), 310)
    n.notify_service_up(SVC, 400)
    down, up = sent
    assert down.priority == "default" and "App A not responding: connection refused" in down.message
    assert "network" not in (down.title + down.message).lower()
    assert up.priority == "default" and "App A" in up.title


# --- config -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["ftp://host/x", "http://user:pw@host/", "http://host/?token=1", "http://host/?",
                                 "javascript:alert(1)", "//host/x", "http://"])
def test_open_url_rejects_unsafe_values(bad):
    with pytest.raises(ValidationError):
        ServiceCheckConfig(id="a", name="A", url=URL, open_url=bad)


def test_service_config_defaults_and_valid_open_url():
    s = ServiceCheckConfig(id="a", name="A", url=URL, open_url=" https://host.lan:9443/ui ")
    assert (s.open_url, s.description, s.notify) == ("https://host.lan:9443/ui", None, True)
    assert ServiceChecksConfig().notify_after_seconds == 300


# --- incident kind filters --------------------------------------------------------------------------------------------

def _inc(kind, title, start, expected=False, targets=()):
    return Incident(start_time=start, cause_code="service_unavailable" if kind == SERVICE_KIND else "unknown_outage",
                    title=title, description="d", severity="warning", kind=kind, expected=expected,
                    affected_targets=list(targets))


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "f.db"))
    await r.initialize()
    await r.create_incident(_inc("gateway_down", "net 1", 1.0))
    await r.create_incident(_inc(SERVICE_KIND, "svc 1", 2.0, targets=["app_a"]))
    await r.create_incident(_inc("busy_latency", "net busy", 3.0, expected=True))
    await r.create_incident(_inc(SERVICE_KIND, "svc 2", 4.0, targets=["app_a"]))
    return r


async def test_repository_kind_filters(repo):
    titles = lambda incs: [i.title for i in incs]  # noqa: E731
    assert titles(await repo.get_recent_incidents()) == ["svc 2", "net busy", "svc 1", "net 1"]
    assert titles(await repo.network_incidents()) == ["net busy", "net 1"]
    assert titles(await repo.service_incidents()) == ["svc 2", "svc 1"]
    assert titles(await repo.network_incidents(include_expected=False)) == ["net 1"]
    assert await repo.count_incidents() == 4
    assert await repo.count_network_incidents() == 2
    assert await repo.count_network_incidents(include_expected=False) == 1
    assert await repo.count_incidents(scope="service") == 2


async def test_service_rows_never_count_as_network_ones_when_the_kind_is_not_registered(repo, monkeypatch):
    # A process that never imported the services feature (a tool, a bare repository) has no service_down kind
    # registered: its rows must land in no list rather than in the network one.
    from pengumon.incidents import taxonomy
    monkeypatch.delitem(taxonomy.KINDS, SERVICE_KIND)
    fresh = DatabaseRepository(db_path=repo.db_path)  # no cached lists from the fixture
    await fresh.initialize()
    titles = [i.title for i in await fresh.network_incidents(limit=10)]
    assert "svc 1" not in titles and "svc 2" not in titles
    assert await fresh.count_network_incidents() == 2


async def test_incident_cache_keys_include_the_filters(repo):
    everything = await repo.get_recent_incidents(limit=10)
    network = await repo.network_incidents(limit=10)
    assert len(everything) == 4 and len(network) == 2
    assert await repo.network_incidents(limit=10) is network
    await repo.create_incident(_inc("gateway_down", "net 2", 5.0))  # invalidates every key
    assert len(await repo.network_incidents(limit=10)) == 3


def _open_guards(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_api_incidents_defaults_to_network_and_takes_a_scope(app_state, repo, monkeypatch):
    _open_guards(monkeypatch)
    app_state.repository = repo
    async with _client() as ac:
        default = (await ac.get("/api/incidents")).json()
        service = (await ac.get("/api/incidents", params={"scope": "service"})).json()
        both = (await ac.get("/api/incidents", params={"scope": "all"})).json()
        network = (await ac.get("/api/incidents", params={"scope": "network", "include_expected": "false"})).json()
        bad = await ac.get("/api/incidents", params={"scope": "nonsense"})
    assert [i["title"] for i in default] == ["net busy", "net 1"]
    assert [i["title"] for i in service] == ["svc 2", "svc 1"]
    assert len(both) == 4 and [i["title"] for i in network] == ["net 1"]
    assert bad.status_code == 422


async def test_the_network_list_does_not_use_up_the_cap_on_service_incidents(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "cap.db"))
    await r.initialize()
    await r.create_incident(_inc("gateway_down", "the only network one", 1.0))
    for n in range(520):
        await r.create_incident(_inc(SERVICE_KIND, f"svc {n}", 10.0 + n, targets=["app_a"]))
    got = await r.network_incidents(limit=500)
    assert [i.title for i in got] == ["the only network one"]


# --- /api/services ----------------------------------------------------------------------------------------------------

async def services_state(app_state, tmp_path, repo, script, *services, polls=1):
    checker, _, clock = await make_checker(tmp_path, script, *services)
    checker.repository = repo
    for _ in range(polls):
        await tick(checker, clock)
    app_state.service_checker, app_state.repository = checker, repo
    return checker


async def test_api_services_shape(app_state, repo, tmp_path, monkeypatch):
    _open_guards(monkeypatch)
    app_state.config = AppConfig(service_checks=ServiceChecksConfig(services=[SVC]))
    await services_state(app_state, tmp_path, repo, Script(503, httpx.ConnectError("")), SVC, polls=2)
    async with _client() as ac:
        res = await ac.get("/api/services")
    assert res.status_code == 200
    [svc] = res.json()
    assert set(svc) == {"id", "name", "description", "open_url", "check_url_display", "status", "since",
                        "last_checked", "last_ok", "latency_ms", "last_status_code", "consecutive_failures",
                        "error", "incidents"}
    assert (svc["id"], svc["name"], svc["description"]) == ("app_a", "App A", "The A app")
    assert svc["open_url"] == "http://host.lan:9000/"
    assert svc["check_url_display"] == "127.0.0.1:9000/api/health"
    assert (svc["status"], svc["consecutive_failures"], svc["last_ok"]) == ("degraded", 2, None)
    assert svc["error"] == {"kind": "unreachable", "message": "ConnectError"}
    assert [i["title"] for i in svc["incidents"]] == ["svc 2", "svc 1"]  # newest first, this service only
    assert "/secret/path" not in res.text and "ca_file" not in res.text and URL not in res.text


async def test_api_services_limits_history_to_five_and_handles_unchecked_services(app_state, repo, tmp_path, monkeypatch):
    _open_guards(monkeypatch)
    for n in range(8):
        await repo.create_incident(_inc(SERVICE_KIND, f"more {n}", 100.0 + n, targets=["quiet"]))
    checker, _, _ = await make_checker(tmp_path, Script(), SVC, QUIET)
    app_state.service_checker, app_state.repository = checker, repo
    async with _client() as ac:
        a, quiet = (await ac.get("/api/services")).json()
    assert (a["status"], a["since"], a["latency_ms"], a["error"]) == ("unknown", None, None, None)
    assert len(quiet["incidents"]) == 5 and quiet["incidents"][0]["title"] == "more 7"


async def test_api_services_is_empty_without_services(app_state, monkeypatch):
    _open_guards(monkeypatch)
    async with _client() as ac:
        assert (await ac.get("/api/services")).json() == []


async def test_api_services_requires_the_token(app_state, monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "tok-1")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    async with _client() as ac:
        assert (await ac.get("/api/services")).status_code == 401
        assert (await ac.get("/api/services", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await ac.get("/api/services", headers={"Authorization": "Bearer tok-1"})).status_code == 200


# --- the feature --------------------------------------------------------------------------------------------------------

async def test_host_services_feature_follows_configuration(app_state, monkeypatch):
    from pengumon.devices.registry import DeviceRegistry
    _open_guards(monkeypatch)
    app_state.devices = DeviceRegistry([])

    async def feature():
        async with _client() as ac:
            return {f["feature"]: f for f in (await ac.get("/api/features")).json()}["host_services"]

    app_state.config = AppConfig()
    assert (await feature())["available"] is False
    off = ServiceCheckConfig(id="o", name="O", url=URL, enabled=False)
    app_state.config = AppConfig(service_checks=ServiceChecksConfig(services=[off]))
    assert (await feature())["available"] is False
    app_state.config = AppConfig(service_checks=ServiceChecksConfig(services=[off, SVC]))
    f = await feature()
    assert (f["available"], f["reason"], f["provided_by"]) == (True, None, ["app_a"])


# --- metrics --------------------------------------------------------------------------------------------------------------

async def test_metrics_count_network_incidents_only_and_export_service_series(app_state, repo, tmp_path):
    timings = iter([0.0, 0.2, 5.0, 5.0])
    checker, _, clock = await make_checker(tmp_path, Script(200, httpx.ConnectError("")), SVC, QUIET,
                                           timer=lambda: next(timings))
    checker.config.services = [SVC]
    app_state.repository, app_state.service_checker = repo, checker
    assert "pengumon_service_up" not in await render_metrics(app_state)  # nothing checked yet
    await tick(checker, clock)
    text = await render_metrics(app_state)
    assert "pengumon_incidents_total 1.0" in text  # 'net 1' only: the expected one and both service ones are out
    assert 'pengumon_service_up{service="app_a"} 1.0' in text
    assert 'pengumon_service_check_seconds{service="app_a"} 0.2' in text
    await tick(checker, clock)
    text = await render_metrics(app_state)
    assert 'pengumon_service_up{service="app_a"} 0.0' in text
    assert "pengumon_service_check_seconds" not in text  # no answer, no latency
