"""Preview before Apply (engine/path_preview.py, POST/GET/DELETE /api/path/preview): the draft's hops that are not
probed live are probed once a second for a while through the prober's backend, recording nothing; single flight per
client, at most two app-wide, stopped early on request and at shutdown."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.icmp_base import ProbeResult
from pengumon.engine.path import PathManager
from pengumon.engine.path_preview import HopTally, PreviewStore
from pengumon.engine.prober import NetworkProber
from pengumon.main import app
from pengumon.models.probes import TargetConfig

from icmp_fakes import FakeBackend

GW = TargetConfig(id="gw", name="Gateway", address="192.0.2.1", category="gateway")
ISP = TargetConfig(id="isp_1", name="Provider hop 1", address="198.51.100.9", category="isp")
PUB = TargetConfig(id="pub", name="Public", address="203.0.113.53", category="public")

ROUNDS = 4


def _hop(t: TargetConfig, position: int, **extra) -> dict:
    return {**t.model_dump(), "position": position, **extra}


@pytest.fixture
async def preview_app(tmp_path, app_state):
    repo = DatabaseRepository(db_path=str(tmp_path / "pv.db"), sample_flush_seconds=0)
    await repo.initialize()
    config = AppConfig(targets=[GW, PUB])
    backend = FakeBackend(answer=lambda address, ttl: ProbeResult("reply", address, 4.0 if address.startswith(
        "198.") else 9.0))
    prober = NetworkProber(backend=backend)
    manager = PathManager(config, repo, prober)
    await manager.load()
    prober.set_targets(config.targets)
    manager.previews = PreviewStore(seconds=ROUNDS, interval=0.01, stagger=0)
    app_state.config, app_state.repository, app_state.prober, app_state.path = config, repo, prober, manager
    yield app_state, backend
    await manager.previews.close()


def _client(host="192.0.2.10"):
    return AsyncClient(transport=ASGITransport(app=app, client=(host, 40000)), base_url="http://test")


async def _done(ac, preview_id):
    for _ in range(300):
        body = (await ac.get(f"/api/path/preview/{preview_id}")).json()
        if body["state"] == "done":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("the preview did not finish")


async def test_a_preview_probes_only_the_hops_not_probed_live_and_records_nothing(preview_app):
    state, backend = preview_app
    stats_before = {k: (v.sent, v.received) for k, v in state.prober.stats.items()}
    moved = _hop(PUB, 2, address="203.0.113.54")  # a live hop whose address changed: probed
    draft = [_hop(GW, 0), _hop(ISP, 1), moved, _hop(TargetConfig(id="off", name="Off", address="192.0.2.99",
                                                                 enabled=False), 3)]
    async with _client() as ac:
        res = await ac.post("/api/path/preview", json={"hops": draft})
        assert res.status_code == 200, res.text
        assert res.json()["probed"] == ["isp_1", "pub"]
        body = await _done(ac, res.json()["id"])
    assert set(body["hops"]) == {"isp_1", "pub"}
    assert body["hops"]["isp_1"] == {"sent": ROUNDS, "received": ROUNDS, "loss_pct": 0.0, "median_ms": 4.0,
                                     "last_ms": 4.0, "kind": "reply"}
    assert body["hops"]["pub"]["median_ms"] == 9.0
    assert 0 <= body["elapsed_s"] <= ROUNDS
    assert sorted({s[0] for s in backend.sent}) == ["198.51.100.9", "203.0.113.54"]  # not the live gateway's address
    assert len(backend.sent) == 2 * ROUNDS
    # nothing recorded: no prober statistics, no samples
    assert {k: (v.sent, v.received) for k, v in state.prober.stats.items()} == stats_before
    assert "isp_1" not in state.prober.stats
    await state.repository.flush_samples()
    assert await state.repository.get_history(time.time() - 600, time.time() + 60) == []


async def test_timeouts_and_unreachable_hops_show_their_loss_and_kind(preview_app):
    state, backend = preview_app

    def answer(address, ttl):
        if address == "198.51.100.9":
            return ProbeResult("timeout")
        return ProbeResult("unreachable", "192.0.2.1", 1.0)

    backend._answer = answer
    state.prober.ping_timeout = 0.01
    async with _client() as ac:
        res = await ac.post("/api/path/preview", json={"hops": [_hop(ISP, 0), _hop(PUB, 1, address="203.0.113.60")]})
        body = await _done(ac, res.json()["id"])
    assert body["hops"]["isp_1"] == {"sent": ROUNDS, "received": 0, "loss_pct": 100.0, "median_ms": None,
                                     "last_ms": None, "kind": "timeout"}
    assert body["hops"]["pub"]["kind"] == "unreachable" and body["hops"]["pub"]["loss_pct"] == 100.0


def test_an_ipv6_hop_this_host_cannot_reach_is_not_a_loss():
    tally = HopTally()
    tally.add({"kind": "ipv6_unavailable", "rtt_ms": None})
    assert tally.public() == {"sent": 0, "received": 0, "loss_pct": None, "median_ms": None, "last_ms": None,
                              "kind": "ipv6_unavailable"}
    for kind in ("ttl_exceeded", "error"):  # anything but a reply, a timeout or no IPv6 is "unreachable"
        tally.add({"kind": kind, "rtt_ms": None})
    tally.add({"kind": "reply", "rtt_ms": 2.0})
    tally.add({"kind": "reply", "rtt_ms": 6.0})
    assert tally.public() == {"sent": 4, "received": 2, "loss_pct": 50.0, "median_ms": 4.0, "last_ms": 6.0,
                              "kind": "reply"}


async def test_a_clients_new_preview_stops_its_previous_one_and_at_most_two_run_at_once(preview_app):
    state, _ = preview_app
    state.path.previews.seconds = 100000
    draft = {"hops": [_hop(ISP, 0)]}
    async with _client("192.0.2.10") as a, _client("192.0.2.11") as b, _client("192.0.2.12") as c:
        first = (await a.post("/api/path/preview", json=draft)).json()["id"]
        second = (await a.post("/api/path/preview", json=draft)).json()["id"]
        assert first != second
        assert (await a.get(f"/api/path/preview/{first}")).json()["state"] == "done"
        assert (await a.get(f"/api/path/preview/{second}")).json()["state"] == "running"
        assert (await b.post("/api/path/preview", json=draft)).status_code == 200
        refused = await c.post("/api/path/preview", json=draft)
        assert refused.status_code == 429 and refused.headers["retry-after"] == "30"
        assert len(state.path.previews.running()) == 2


async def test_delete_stops_a_preview_early_and_an_unknown_one_is_404(preview_app):
    state, backend = preview_app
    state.path.previews.seconds = 100000
    async with _client() as ac:
        preview_id = (await ac.post("/api/path/preview", json={"hops": [_hop(ISP, 0)]})).json()["id"]
        await asyncio.sleep(0.05)
        stopped = await ac.delete(f"/api/path/preview/{preview_id}")
        assert stopped.status_code == 200 and stopped.json()["state"] == "done"
        sent = len(backend.sent)
        await asyncio.sleep(0.05)
        assert len(backend.sent) == sent  # nothing more is sent
        assert (await ac.get(f"/api/path/preview/{preview_id}")).json()["state"] == "done"
        assert (await ac.delete("/api/path/preview/nope")).status_code == 404
        assert (await ac.get("/api/path/preview/nope")).status_code == 404


async def test_shutdown_stops_running_previews(preview_app):
    state, backend = preview_app
    state.path.previews.seconds = 100000
    async with _client() as ac:
        await ac.post("/api/path/preview", json={"hops": [_hop(ISP, 0)]})
    await asyncio.sleep(0.02)
    await state.path.previews.close()
    assert state.path.previews.running() == []
    sent = len(backend.sent)
    await asyncio.sleep(0.05)
    assert len(backend.sent) == sent


async def test_a_finished_preview_is_forgotten_after_a_while():
    now = [100.0]
    store = PreviewStore(clock=lambda: now[0], seconds=1, interval=0.0, stagger=0)

    async def probe(address, family):
        return {"kind": "reply", "rtt_ms": 1.0}

    preview_id = await store.start("c", {"h": ("192.0.2.1", 4)}, probe)
    for _ in range(5):
        await asyncio.sleep(0)
    assert store.get(preview_id)["state"] == "done"
    now[0] += 121
    assert store.get(preview_id) is None


@pytest.mark.parametrize("hops, says", [
    ([_hop(ISP, 0), _hop(ISP, 1)], "unique"),
    ([_hop(ISP, 0, address="198.51.100.9; reboot")], "Host"),
    ([_hop(TargetConfig(id=f"h{i}", name="H", address="192.0.2.7"), i) for i in range(65)], "at most 64"),
])
async def test_a_preview_takes_only_a_valid_draft(preview_app, hops, says):
    async with _client() as ac:
        res = await ac.post("/api/path/preview", json={"hops": hops})
    assert res.status_code == 422 and says.lower() in res.text.lower(), res.text


async def test_previews_are_rate_limited(preview_app):
    async with _client("192.0.2.20") as ac:
        codes = [(await ac.post("/api/path/preview", json={"hops": []})).status_code for _ in range(13)]
    assert codes[:12] == [200] * 12 and codes[12] == 429
