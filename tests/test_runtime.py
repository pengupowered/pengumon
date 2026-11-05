"""Runtime loop: collectors and websocket sends must never hold up probing; disabled targets, the targets API."""

import asyncio
import json
import time

import httpx
from httpx import AsyncClient, ASGITransport

from pengumon import security
from pengumon.live.ws import ConnectionManager
from pengumon.capabilities import WanCounters
from pengumon.config import AppConfig
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.path import MAX_PATH_HOPS
from pengumon.engine.prober import NetworkProber
from pengumon.engine.loop import probe_background_worker
from pengumon.main import app
from pengumon.services.component import collector_background_worker
from pengumon.incidents.model import SystemDiagnosis
from pengumon.live.message import LiveStreamMessage
from pengumon.models.probes import ProbeSample, TargetConfig

from device_helpers import build_router


class FakeProber:
    """Stands in for NetworkProber: records when each round ran."""

    def __init__(self):
        self.stats = {}
        self.round_times = []

    def probing_problem(self):
        return None

    async def execute_probe_round(self, targets, dns_probes):
        self.round_times.append(time.monotonic())
        return []


class FakeClassifier:
    async def evaluate(self, samples, stats):
        return SystemDiagnosis()


class SlowCollector:
    latest_snapshot = None

    def __init__(self, seconds):
        self.seconds = seconds
        self.polls = 0

    async def poll(self, force=False):
        self.polls += 1
        await asyncio.sleep(self.seconds)


class FakeSocket:
    def __init__(self, hang=False):
        self.hang = hang
        self.sent = []
        self.closed = False

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.hang:
            await asyncio.sleep(3600)
        self.sent.append(text)

    async def close(self):
        self.closed = True


def _config(interval=0.05):
    config = AppConfig()
    config.prober.interval_seconds = interval
    return config


async def _stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_slow_collectors_do_not_delay_probe_rounds(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    prober = FakeProber()
    docsis, router = SlowCollector(5), SlowCollector(5)
    tasks = [
        asyncio.create_task(collector_background_worker("DOCSIS", docsis, 0.1)),
        asyncio.create_task(collector_background_worker("router", router, 0.1)),
        asyncio.create_task(
            probe_background_worker(_config(), prober, FakeClassifier(), repo, ws=ConnectionManager())
        ),
    ]
    await asyncio.sleep(1.0)
    await _stop(*tasks)

    assert docsis.polls == 1 and router.polls == 1  # both really are stuck in their 5 s poll
    gaps = [b - a for a, b in zip(prober.round_times, prober.round_times[1:])]
    assert len(prober.round_times) >= 6
    assert max(gaps) < 0.4


async def test_collector_worker_keeps_polling_after_an_error():
    class Flaky:
        polls = 0

        async def poll(self, force=False):
            self.polls += 1
            if self.polls == 1:
                raise RuntimeError("router unreachable")

    flaky = Flaky()
    task = asyncio.create_task(collector_background_worker("router", flaky, 0.05))
    await asyncio.sleep(0.4)
    await _stop(task)
    assert flaky.polls >= 3


async def test_hanging_websocket_client_is_dropped_without_stalling_the_loop(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    manager = ConnectionManager(send_timeout=0.2)
    stuck, healthy = FakeSocket(hang=True), FakeSocket()
    await manager.connect(stuck)
    await manager.connect(healthy)
    prober = FakeProber()
    tasks = [
        asyncio.create_task(manager.run_broadcaster()),
        asyncio.create_task(
            probe_background_worker(
                _config(), prober, FakeClassifier(), repo, ws=manager
            )
        ),
    ]
    await asyncio.sleep(1.0)
    await _stop(*tasks)

    assert manager.active_connections == [healthy]
    assert stuck.closed
    assert len(healthy.sent) >= 3  # kept receiving while the other one hung
    gaps = [b - a for a, b in zip(prober.round_times, prober.round_times[1:])]
    assert max(gaps) < 0.4


async def test_every_broadcast_carries_the_incidents_version_and_no_incident_list():
    manager = ConnectionManager()
    sock = FakeSocket()
    await manager.connect(sock)
    msg = LiveStreamMessage(timestamp=1.0, diagnosis=SystemDiagnosis(), targets=[], incidents_version=7)

    await manager.broadcast(msg)
    await manager.broadcast(msg)

    sent = [json.loads(s) for s in sock.sent]
    assert [m["incidents_version"] for m in sent] == [7, 7]
    assert not {"recent_incidents", "docsis", "router"} & set(sent[0])


async def test_publish_keeps_only_the_newest_message():
    manager = ConnectionManager()
    sock = FakeSocket()
    await manager.connect(sock)
    for i in range(5):
        manager.publish(LiveStreamMessage(timestamp=float(i), diagnosis=SystemDiagnosis(), targets=[]))
    task = asyncio.create_task(manager.run_broadcaster())
    await asyncio.sleep(0.1)
    await _stop(task)
    assert len(sock.sent) == 1
    assert '"timestamp":4.0' in sock.sent[0]


async def test_disabled_targets_are_not_probed_and_record_nothing():
    prober = NetworkProber(ping_timeout=0.5, history_window=5)
    off = TargetConfig(id="off", name="Off", address="192.0.2.1", category="custom", enabled=False)
    on = TargetConfig(id="on", name="On", address="192.0.2.2", category="custom")
    dns_off = DnsProbeConfig(id="dns_off", name="DNS off", domain="example.com", enabled=False)

    real_icmp = prober.probe_icmp

    async def icmp(target):
        if not target.enabled:
            return await real_icmp(target)  # the real disabled path
        return ProbeSample(target_id=target.id, success=True, latency_ms=3.0)

    prober.probe_icmp = icmp
    samples = await prober.execute_probe_round(targets=[off, on], dns_probes=[dns_off])

    assert [s.target_id for s in samples] == ["on"]
    assert prober.stats["off"].sent == 0 and prober.stats["dns_off"].sent == 0
    assert prober.stats["on"].sent == 1
    assert await real_icmp(off) is None
    assert await prober.probe_dns(dns_off) is None


async def test_targets_api_edits_the_stored_path_and_enforces_limits(tmp_path, app_state):
    """POST and DELETE /api/targets (the add-target dialog, the trace dialog's Monitor) edit the stored probe path
    one hop at a time; any hop can be removed now, its history kept (hidden)."""
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    config = AppConfig(targets=[TargetConfig(id="gw", name="Gateway", address="192.168.100.1", category="gateway")])
    app_state.config = config
    app_state.repository = repo
    app_state.prober = NetworkProber()
    app_state.prober.register_target(config.targets[0])

    def body(tid):
        return {"id": tid, "name": "Custom", "address": "192.0.2.5", "category": "custom"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        assert (await ac.post("/api/targets", json=body("mine"))).status_code == 200
        assert (await ac.post("/api/targets", json=body("mine"))).status_code == 400  # duplicate
        assert (await ac.post("/api/targets", json=body("gw"))).status_code == 400  # taken by a hop of the path
        assert [t.id for t in (await repo.load_path())[1]] == ["gw", "mine"]
        assert "mine" in app_state.prober.stats

        assert (await ac.delete("/api/targets/nope")).status_code == 404
        assert (await ac.delete("/api/targets/mine")).status_code == 200
        assert [t.id for t in (await repo.load_path())[1]] == ["gw"]
        assert [t.id for t in config.targets] == ["gw"]
        assert "mine" not in app_state.prober.stats
        assert await repo.removed_path_ids() == {"mine"}

        security.reset_rate_limits()  # this test posts more than the 20/min rate limit allows
        for i in range(MAX_PATH_HOPS - 1):
            if i and i % 19 == 0:
                security.reset_rate_limits()
            assert (await ac.post("/api/targets", json=body(f"c{i}"))).status_code == 200
        security.reset_rate_limits()
        refused = await ac.post("/api/targets", json=body("extra"))
        assert refused.status_code == 400 and str(MAX_PATH_HOPS) in refused.json()["detail"]


def _router_transport(counter):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login.cgi":
            return httpx.Response(200, json={"asus_token": "tok"})
        if request.url.path == "/appGet.cgi" and b"netdev" in request.content:
            counter["n"] += 1
            return httpx.Response(
                200, text=f"INTERNET_rx: {counter['n'] * 125_000_000}, INTERNET_tx: {counter['n'] * 12_500_000}"
            )
        return httpx.Response(500)

    return httpx.MockTransport(handler)


async def test_router_collector_keeps_one_client_and_a_rate_history():
    client = httpx.AsyncClient(transport=_router_transport({"n": 0}))
    wall = {"t": time.time()}
    collector = build_router(password="pw", client=client, wall_clock=lambda: wall["t"])

    await collector.poll(force=True)
    assert collector.monitor.recent_rates() == []  # first poll only establishes the counters
    wall["t"] += 0.5
    await collector.poll(force=True)
    assert collector.monitor.recent_rates() == []  # half a second later: too soon for a rate, baseline kept
    wall["t"] += 0.5
    await collector.poll(force=True)
    wall["t"] += 1.0
    await collector.poll(force=True)

    rates = collector.monitor.recent_rates()
    assert len(rates) == 2
    ts, rx, tx = rates[-1]
    assert ts == wall["t"]
    assert [(r[1], r[2]) for r in rates] == [(2000.0, 200.0), (1000.0, 100.0)]  # 250 MB over 1 s, 125 MB over 1 s
    assert collector.driver.http._client is client and not client.is_closed
    await collector.aclose()
    assert client.is_closed


async def test_router_rate_history_covers_ten_minutes_and_skips_failures():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    collector = build_router(password="pw", client=client)
    now = time.time()
    wan = collector.monitor.wan
    # Counter readings whose successive deltas give 1.0, 2.0 and 3.0 Mbps at now-700, now-500 and now-5.
    wan.update(now - 701, WanCounters(0, 0))
    wan.update(now - 700, WanCounters(125_000, 125_000))
    wan.update(now - 500, WanCounters(125_000 + 50_000_000, 125_000 + 50_000_000))
    wan.update(now - 5, WanCounters(125_000 + 50_000_000 + 185_625_000, 125_000 + 50_000_000 + 185_625_000))
    assert [r[1] for r in collector.monitor.recent_rates()] == [2.0, 3.0]

    # An unreachable router adds nothing.
    snap = await collector.poll(force=True)
    assert snap.status == "error"
    assert [r[1] for r in collector.monitor.recent_rates()] == [2.0, 3.0]
    await collector.aclose()


async def test_responses_over_1kib_are_gzipped():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/", headers={"Accept-Encoding": "gzip"})
        assert res.headers.get("content-encoding") == "gzip"
