"""Dossier honesty, reliability report, health check, Prometheus metrics and ntfy notifications."""

import asyncio
import re
import time
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import __version__
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.registry import DeviceRegistry
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.prober import NetworkProber
from pengumon.lines.docsis import DocsisHistory, line_sample
from pengumon.main import app
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample, TargetConfig, TargetStats
from pengumon.notify import Notifier
from pengumon.reports.common import find_primary_failing_hop
from pengumon.reports.dossier import build_dossier
from pengumon.reports.reliability import build_report

from device_helpers import build_modem


def _cfg():
    targets = [
        SimpleNamespace(id="router", name="Router", address="192.168.1.1", category="local"),
        SimpleNamespace(id="modem", name="Modem", address="192.168.100.1", category="gateway"),
        SimpleNamespace(id="hub", name="ISP Hub", address="198.51.100.5", category="isp"),
    ]
    return SimpleNamespace(
        targets=targets, dns_probes=[], classifier=SimpleNamespace(busy_load_pct=60.0),
    )


async def _repo(tmp_path, loss=None, minutes=30):
    """Samples for the last `minutes` minutes, one per target every 2 s; loss maps target -> every Nth lost."""
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    loss = loss or {}
    now = time.time()
    samples = []
    for i in range(minutes * 30):
        ts = now - minutes * 60 + i * 2
        for tid in ("router", "modem", "hub"):
            every = loss.get(tid)
            lost = bool(every) and i % every == 0
            samples.append(ProbeSample(target_id=tid, timestamp=ts, success=not lost,
                                       latency_ms=None if lost else 5.0, packet_loss_pct=100.0 if lost else 0.0))
    await repo.insert_samples(samples)
    return repo


def _snapshot(channels=(), total=0, ts=None):
    return DocsisSignalSnapshot(
        timestamp=ts or time.time(), downstream_power_avg=1.0, downstream_snr_avg=38.0, upstream_power_avg=42.0,
        uncorrectables_total=total, status="healthy", channels=list(channels),
    )


async def _store(repo, snapshot):
    await repo.insert_line_sample(line_sample("modem", snapshot))


def _clean_channel():
    return DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0,
                         snr_db=38.0, modulation="QAM256", correcteds=10, uncorrectables=0)


def test_primary_failing_hop_needs_loss_above_earlier_hops():
    hop = lambda t, p: {"target_id": t, "sent": 100, "loss_pct": p}
    assert find_primary_failing_hop([hop("a", 0.0), hop("b", 0.2), hop("c", 3.0)])["target_id"] == "c"
    assert find_primary_failing_hop([hop("a", 3.0), hop("b", 3.2), hop("c", 3.4)])["target_id"] == "a"
    assert find_primary_failing_hop([hop("a", 0.0), hop("b", 0.3)]) is None
    assert find_primary_failing_hop([hop("a", 2.0), hop("b", 1.0), hop("c", 2.2)])["target_id"] == "a"


@pytest.mark.asyncio
async def test_dossier_makes_no_rf_claim_with_clean_docsis(tmp_path):
    repo = await _repo(tmp_path)
    await _store(repo, _snapshot([_clean_channel()], total=5, ts=time.time() - 600))
    await _store(repo, _snapshot([_clean_channel()], total=5))
    result = await build_dossier(repo, _cfg(), 24, line=DocsisHistory(repo, "modem"))
    md = result["formatted_markdown"].lower()
    for word in ("coax", "cable-plant", "reed-solomon", "proves", "proof", "conclusively", "root cause", "ingress"):
        assert word not in md
    assert "no single failing hop was isolated" in md
    assert result["primary_failing_hop"] is None
    assert result["total_incidents"] == 0


@pytest.mark.asyncio
async def test_dossier_names_hop_and_rf_only_with_evidence(tmp_path):
    repo = await _repo(tmp_path, loss={"hub": 10})
    bad = DocsisChannel(channel_id=7, direction="downstream", frequency_mhz=585.0, power_dbmv=0.0, snr_db=29.0,
                        modulation="QAM256", correcteds=10, uncorrectables=5000)
    await _store(repo, _snapshot([bad], total=100, ts=time.time() - 600))
    await _store(repo, _snapshot([bad], total=900))
    result = await build_dossier(repo, _cfg(), 24, line=DocsisHistory(repo, "modem"))
    md = result["formatted_markdown"]
    assert result["primary_failing_hop"] == "hub"
    assert "First hop with elevated loss:** ISP Hub (198.51.100.5)" in md
    assert "cable-plant" in md
    assert "800 new uncorrectable codewords" in md
    assert "T3/T4" not in md
    assert "proves" not in md.lower()


@pytest.mark.asyncio
async def test_dossier_excludes_expected_incidents(tmp_path):
    repo = await _repo(tmp_path, minutes=5)
    now = time.time()
    for expected, dur in ((False, 120.0), (True, 600.0), (True, 60.0)):
        iid = await repo.create_incident(Incident(
            start_time=now - 3000, cause_code="load_induced_latency" if expected else "local_wifi_mesh_drop",
            title="x", description="y", severity="warning", expected=expected,
        ))
        await repo.resolve_incident(iid, now - 3000 + dur)
    result = await build_dossier(repo, _cfg(), 24)
    assert result["total_incidents"] == 1
    assert result["total_duration_minutes"] == 2.0
    assert result["excluded_expected"] == 2
    assert "2 events during heavy line load were excluded (expected)" in result["formatted_markdown"]


@pytest.mark.asyncio
async def test_report_availability_incidents_and_hours(tmp_path):
    repo = await _repo(tmp_path, loss={"hub": 4})
    now = time.time()
    for expected in (False, True):
        iid = await repo.create_incident(Incident(
            start_time=now - 900, cause_code="unknown_outage", title="t", description="d", severity="warning",
            expected=expected))
        await repo.resolve_incident(iid, now - 840)
    report = await build_report(repo, _cfg(), 7)
    hops = {h["target_id"]: h for h in report["availability"]["hops"]}
    assert hops["router"]["availability_pct"] == 100.0
    assert 74.0 < hops["hub"]["availability_pct"] < 76.0
    assert len(hops["hub"]["per_day"]) == len(report["availability"]["days"])
    assert report["incidents"]["count"] == 1
    assert report["incidents"]["minutes"] == 1.0
    assert report["incidents"]["excluded_expected"] == 1
    assert report["incidents"]["by_cause"] == [{"cause_code": "unknown_outage", "count": 1, "minutes": 1.0}]
    assert report["worst_hours"] and report["worst_hours"][0]["loss_pct"] > 0


@pytest.mark.asyncio
async def test_dossier_and_report_endpoints(tmp_path, app_state):
    repo = await _repo(tmp_path, minutes=2)
    app_state.repository = repo
    app_state.config = _cfg()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/incidents/dossier?hours=720")
        assert res.status_code == 200
        for key in ("formatted_markdown", "total_incidents", "total_duration_minutes", "hop_stats", "docsis",
                    "observation_hours"):
            assert key in res.json()
        assert (await ac.get("/api/incidents/dossier?hours=721")).status_code == 422
        assert (await ac.get("/api/report?days=30")).status_code == 200
        assert (await ac.get("/api/report?days=401")).status_code == 422
        assert (await ac.get("/api/report?days=0")).status_code == 422


@pytest.mark.asyncio
async def test_health_200_when_fresh_503_when_stale(app_state):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        app_state.prober = SimpleNamespace(last_round_ts=time.time() - 2)
        res = await ac.get("/api/health")
        assert res.status_code == 200 and res.json()["version"] == __version__
        app_state.prober = SimpleNamespace(last_round_ts=time.time() - 30)
        res = await ac.get("/api/health")
        assert res.status_code == 503 and "ago" in res.json()["reason"]
        app_state.prober = SimpleNamespace(last_round_ts=None)
        assert (await ac.get("/api/health")).status_code == 503
        app_state.prober = None
        assert (await ac.get("/api/health")).status_code == 503


_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?:\w+="(?:[^"\\]|\\.)*",?)*\})? (-?\d+(\.\d+)?(e[+-]?\d+)?)$')


@pytest.mark.asyncio
async def test_metrics_exposition_parses(tmp_path, app_state):
    repo = await _repo(tmp_path, minutes=1)
    prober = NetworkProber()
    prober.register_target(TargetConfig(id="r", name='Rou"ter\\x', address="192.168.1.1", category="local"))
    prober.stats["r"].last_ms = 1.5
    prober.stats["r"].avg_ms = None  # unknown values are omitted, not printed as NaN
    prober.last_round_ts = time.time()
    classifier = OutageClassifier(config=ClassifierConfig())
    modem = build_modem(password="pw")
    modem.monitor.latest_snapshot = DocsisSignalSnapshot(status="healthy")
    app_state.prober, app_state.classifier, app_state.repository = prober, classifier, repo
    app_state.devices = DeviceRegistry([modem])  # and no router
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = res.text
    assert text.endswith("\n")
    declared = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            declared.add(line.split()[2])
        elif line.startswith("# HELP "):
            continue
        else:
            m = _SAMPLE.match(line)
            assert m, f"unparsable line: {line!r}"
            assert m.group(1) in declared
    assert 'name="Rou\\"ter\\\\x"' in text
    assert f'pengumon_build_info{{version="{__version__}"}} 1.0' in text
    assert 'pengumon_collector_status{device="modem",status="healthy"} 1.0' in text
    for name in ("pengumon_target_last_latency_ms", "pengumon_system_status", "pengumon_incident_active",
                 "pengumon_incidents_total", "pengumon_probe_loop_age_seconds"):
        assert name in declared
    assert "pengumon_target_avg_latency_ms" not in text


def _mock_notifier(requests, status=200, **kw):
    def handler(request):
        requests.append(request)
        return httpx.Response(status)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Notifier("http://ntfy.test/topic", client=client, **kw)


def _incident(iid=1, cause="internet_backbone_transit_loss", **kw):
    return Incident(id=iid, start_time=1000.0, cause_code=cause, title="ISP Outage", description="Hub unreachable",
                    severity="critical", **kw)


async def _drain(n):
    await asyncio.gather(*list(n._tasks))


@pytest.mark.asyncio
async def test_notifier_sends_start_and_resolve_with_token():
    requests = []
    n = _mock_notifier(requests, token="secret")
    inc = _incident(load_pct=72.0)
    n.on_incident_started(inc)
    await _drain(n)
    inc.duration_seconds = 300.0
    n.on_incident_resolved(inc)
    await _drain(n)
    assert len(requests) == 2
    start, end = requests
    assert str(start.url) == "http://ntfy.test/topic"
    assert start.headers["Authorization"] == "Bearer secret"
    assert "ISP Outage" in start.headers["Title"]
    body = start.content.decode()
    assert "internet_backbone_transit_loss" in body and "72%" in body
    assert "resolved" in end.headers["Title"].lower()
    assert "5.0 min" in end.content.decode()
    await n.aclose()


@pytest.mark.asyncio
async def test_notifier_skips_expected_and_rate_limits_per_cause():
    requests = []
    now = [0.0]
    n = _mock_notifier(requests, clock=lambda: now[0])
    n.on_incident_started(_incident(1, expected=True))
    n.on_incident_started(_incident(2))
    n.on_incident_started(_incident(3))                 # same cause within 5 min: suppressed
    n.on_incident_started(_incident(4, cause="dns_resolver_failure"))  # another cause is independent
    await _drain(n)
    assert len(requests) == 2
    n.on_incident_resolved(_incident(3))                # its start was never sent
    n.on_incident_resolved(_incident(2))                # resolve of an announced incident always goes out
    await _drain(n)
    assert len(requests) == 3
    now[0] = 301.0
    n.on_incident_started(_incident(5))
    await _drain(n)
    assert len(requests) == 4
    await n.aclose()


@pytest.mark.asyncio
async def test_notifier_failure_is_swallowed_and_never_blocks():
    requests = []
    n = _mock_notifier(requests, status=500)
    n.on_incident_started(_incident())  # returns immediately, nothing awaited
    assert len(requests) == 0
    await _drain(n)
    assert len(requests) == 1
    await n.aclose()


def test_notifier_from_env(monkeypatch):
    monkeypatch.delenv("PENGUMON_NOTIFY_URL", raising=False)
    assert Notifier.from_env() is None
    monkeypatch.setenv("PENGUMON_NOTIFY_URL", "https://ntfy.sh/t")
    assert Notifier.from_env().url == "https://ntfy.sh/t"


@pytest.mark.asyncio
async def test_classifier_calls_incident_callbacks():
    started, resolved = [], []
    c = OutageClassifier(
        config=ClassifierConfig(consecutive_failures_for_incident=2),
        on_incident_started=started.append, on_incident_resolved=resolved.append,
    )
    stats = {
        "local": TargetStats(target_id="local", name="L", category="local", address="a", enabled=True),
        "gateway": TargetStats(target_id="gateway", name="G", category="gateway", address="b", enabled=True),
        "isp": TargetStats(target_id="isp", name="I", category="isp", address="c", enabled=True),
        "public": TargetStats(target_id="public", name="P", category="public", address="d", enabled=True),
    }
    bad = [ProbeSample(target_id="local", success=True, latency_ms=1.0),
           ProbeSample(target_id="gateway", success=True, latency_ms=1.0),
           ProbeSample(target_id="isp", success=False), ProbeSample(target_id="public", success=False)]
    good = [ProbeSample(target_id=t, success=True, latency_ms=1.0) for t in stats]
    for _ in range(2):
        await c.evaluate(bad, stats)
    assert len(started) == 1 and resolved == []
    for _ in range(2):
        await c.evaluate(good, stats)
    assert resolved == started


@pytest.mark.asyncio
async def test_report_day_and_hour_buckets_follow_tz_offset(tmp_path, app_state):
    repo = await _repo(tmp_path, loss={"hub": 4}, minutes=2)
    now = time.time()
    utc = await build_report(repo, _cfg(), 2, now=now)
    east = await build_report(repo, _cfg(), 2, now=now, tz_offset_minutes=840)
    west = await build_report(repo, _cfg(), 2, now=now, tz_offset_minutes=-840)
    hour = lambda r: r["loss_by_hour"][0]["hour"]
    assert east["tz_offset_minutes"] == 840
    assert (hour(east) - hour(utc)) % 24 in (14, 15)  # the 2-minute window may straddle an hour boundary
    assert (hour(utc) - hour(west)) % 24 in (14, 15)
    # +14 h and -14 h are 28 h apart, so the sample day differs between them
    assert east["availability"]["days"] != west["availability"]["days"]

    app_state.repository = repo
    app_state.config = _cfg()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/report?days=1&tz_offset_minutes=-300")
        assert res.status_code == 200 and res.json()["tz_offset_minutes"] == -300
        assert (await ac.get("/api/report?days=1&tz_offset_minutes=841")).status_code == 422


def test_prometheus_label_values_escape_newlines():
    """A newline in a label value (a target name, say) must not end the sample line."""
    from pengumon.metrics import escape_label
    assert escape_label('a "b"\nc\\d') == 'a \\"b\\"\\nc\\\\d'
