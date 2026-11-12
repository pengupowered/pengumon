"""Bucketed router history (GET /api/devices/{id}/history?bucket_seconds=&max_points=) and the DOCSIS per-channel
history (GET /api/devices/{id}/channels/history): bucketing arithmetic, filtering, caps, error codes, the guards."""

import time
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.samples import bucket_width, device_sample
from pengumon.lines.docsis import channel_history_rows, line_sample
from pengumon.main import app, security_config
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot

from device_helpers import build_modem, build_router

TOKEN = "tok-hist"


@pytest.fixture(autouse=True)
def _open_guards(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _devices():
    refuse = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    return DeviceRegistry([build_modem(client=httpx.AsyncClient(transport=refuse)),
                           build_router(client=httpx.AsyncClient(transport=refuse))])


async def _router_poll(repo, ts, rx, tx, cpu, mem, clients):
    await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
        timestamp=ts, wan_rx_rate_mbps=rx, wan_tx_rate_mbps=tx, cpu_usage_pct=cpu, mem_usage_pct=mem,
        client_count=clients, status="healthy")))


def _snapshot(ts, channels):
    return DocsisSignalSnapshot(timestamp=ts, status="healthy", channels=channels)


def _ch(cid, direction="downstream", power=1.0, snr=38.0, recent=0):
    return DocsisChannel(channel_id=cid, direction=direction, frequency_mhz=500.0, power_dbmv=power,
                         snr_db=snr if direction == "downstream" else None, modulation="QAM256",
                         uncorrectables_recent=recent)


# --- bucket width -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("span,bucket,points,want", [
    (3600, None, None, 10),          # 9 s rounded up to the 10 s poll
    (6 * 3600, None, None, 60),      # 54 s -> a minute
    (86400, None, None, 240),        # 216 s -> 4 min
    (7 * 86400, None, None, 1560),   # 1512 s -> 26 min
    (3600, 300, None, 300),          # requested width kept when it is wide enough
    (86400, 10, None, 240),          # ... widened when it is not
    (86400, None, 100, 900),         # fewer points requested
    (0, None, None, 10),             # empty range
    (100, None, 5000, 10),           # max_points is capped
])
def test_bucket_width(span, bucket, points, want):
    assert bucket_width(span, bucket, points) == want
    assert span / bucket_width(span, bucket, points) <= 1000


# --- the bucketed endpoint --------------------------------------------------------------------------------------------

async def test_buckets_average_and_max_with_edges_and_nulls(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    base = (int(time.time()) // 600) * 600 - 3000  # a bucket boundary for 60 s buckets, well inside the retention
    await _router_poll(repo, base + 0, 10.0, 1.0, 20.0, 50.0, 10)
    await _router_poll(repo, base + 30, 30.0, 3.0, 40.0, 60.0, 12)
    await _router_poll(repo, base + 59.9, 20.0, 2.0, 30.0, 70.0, 11)     # still the first bucket
    await _router_poll(repo, base + 60, 100.0, 5.0, 90.0, 80.0, 20)      # the edge opens the second
    await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
        timestamp=base + 70, status="error")))                          # a failed poll: all NULL, ignored by AVG/MAX
    await _router_poll(repo, base + 200, 7.0, 0.5, 1.0, 2.0, 3)           # bucket at +180
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history",
                           params={"start_time": base, "end_time": base + 300, "bucket_seconds": 60})
    assert res.status_code == 200
    body = res.json()
    assert body["bucket_seconds"] == 60
    assert body["fields"] == ["ts", "rx_avg", "rx_max", "tx_avg", "tx_max", "cpu_avg", "cpu_max", "mem_avg", "mem_max",
                              "clients_avg", "clients_max", "temp_avg", "temp_max", "link_min"]
    assert body["rows"] == [
        [base, 20.0, 30.0, 2.0, 3.0, 30.0, 40.0, 60.0, 70.0, 11.0, 12, None, None, None],
        [base + 60, 100.0, 100.0, 5.0, 5.0, 90.0, 90.0, 80.0, 80.0, 20.0, 20, None, None, None],
        [base + 180, 7.0, 7.0, 0.5, 0.5, 1.0, 1.0, 2.0, 2.0, 3.0, 3, None, None, None],
    ]
    assert body["capacity"] == {"down_mbps": None, "up_mbps": None}


async def test_a_bucket_of_failed_polls_has_null_figures(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    base = (int(time.time()) // 600) * 600 - 3000
    await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(timestamp=base + 1, status="error")))
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history", params={"start_time": base, "end_time": base + 60,
                                                                  "bucket_seconds": 60})
    assert res.json()["rows"] == [[base] + [None] * 13]


async def test_empty_and_inverted_ranges_answer_no_rows(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    async with _client() as ac:
        empty = await ac.get("/api/devices/router/history", params={"start_time": now - 600, "end_time": now,
                                                                    "max_points": 50})
        inverted = await ac.get("/api/devices/router/history", params={"start_time": now, "end_time": now - 600,
                                                                       "max_points": 50})
    assert empty.status_code == 200 and empty.json()["rows"] == []
    assert inverted.status_code == 200 and inverted.json()["rows"] == []


async def test_automatic_bucketing_caps_the_points_and_the_range_is_clipped_to_retention(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    await _router_poll(repo, now - 10 * 86400, 1.0, 1.0, 1.0, 1.0, 1)   # beyond the 7 day retention: never returned
    for i in range(0, 7 * 86400, 1800):                                  # a sample every 30 min across the week
        await _router_poll(repo, now - i - 5, 5.0, 1.0, 10.0, 20.0, 4)
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history", params={"start_time": now - 30 * 86400, "end_time": now,
                                                                  "max_points": 300})
    body = res.json()
    assert body["start_time"] >= now - 7 * 86400 - 5
    assert body["bucket_seconds"] >= (7 * 86400) / 300
    assert 0 < len(body["rows"]) <= 300
    assert all(r[1] == 5.0 for r in body["rows"])
    # a width below the cap is raised, not obeyed
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history", params={"start_time": now - 86400, "end_time": now,
                                                                  "bucket_seconds": 10})
    assert res.json()["bucket_seconds"] == 240


async def test_capacity_comes_from_the_line_load(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    app_state.classifier = SimpleNamespace(load=SimpleNamespace(down_mbps=1000.0, up_mbps=35.0))
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history", params={"max_points": 10})
    assert res.json()["capacity"] == {"down_mbps": 1000.0, "up_mbps": 35.0}


async def test_without_bucket_parameters_the_answer_is_the_raw_snapshots(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    await _router_poll(repo, now - 30, 12.5, 1.5, 33.0, 44.0, 9)
    async with _client() as ac:
        res = await ac.get("/api/devices/router/history")
    rows = res.json()
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["wan_rx_rate_mbps"] == 12.5 and rows[0]["client_count"] == 9 and "timestamp" in rows[0]


async def test_bucket_parameters_are_validated(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    async with _client() as ac:
        for params in ({"bucket_seconds": 0}, {"max_points": 1}, {"max_points": 100000}, {"bucket_seconds": "x"}):
            assert (await ac.get("/api/devices/router/history", params=params)).status_code == 422
        assert (await ac.get("/api/devices/nope/history", params={"max_points": 10})).status_code == 404
        assert (await ac.get("/api/devices/modem/history", params={"max_points": 10})).status_code == 400


# --- channel history --------------------------------------------------------------------------------------------------

async def _seed_channels(repo, now):
    for i, ts in enumerate((now - 7200, now - 3600, now - 60)):
        await repo.insert_line_sample(line_sample("modem", _snapshot(ts + 0.7, [
            _ch(1, power=1.0 + i, snr=38.0 - i, recent=i * 5),
            _ch(2, snr=None if i == 1 else 37.0),
            _ch(1, "upstream", power=44.5 + i),
        ])))
    # a reading with no channels (a failed poll) and one from before the retention
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(timestamp=now - 30, status="error")))
    await repo.insert_line_sample(line_sample("modem", _snapshot(now - 9 * 86400, [_ch(1)])))


async def test_channel_history_rows_are_decoded_compact_and_ordered(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    await _seed_channels(repo, now)
    async with _client() as ac:
        res = await ac.get("/api/devices/modem/channels/history")
    body = res.json()
    assert body["fields"] == ["ts", "direction", "channel_id", "power_dbmv", "snr_db", "uncorrectables_recent"]
    rows = body["rows"]
    assert len(rows) == 9                                       # 3 readings x 3 channels; none for the failed poll
    assert rows == sorted(rows, key=lambda r: r[0])
    assert rows[0] == [int(now - 7200 + 0.7), "d", 1, 1.0, 38.0, 0]
    assert rows[2] == [int(now - 7200 + 0.7), "u", 1, 44.5, None, 0]
    assert [r[3:] for r in rows if r[0] == int(now - 60 + 0.7) and r[1] == "d" and r[2] == 1] == [[3.0, 36.0, 10]]
    assert [r[4] for r in rows if r[2] == 2] == [37.0, None, 37.0]


async def test_channel_history_filters_by_channel_and_direction(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    await _seed_channels(repo, now)
    async with _client() as ac:
        one = (await ac.get("/api/devices/modem/channels/history", params={"channel_id": 2})).json()["rows"]
        up = (await ac.get("/api/devices/modem/channels/history", params={"direction": "upstream"})).json()["rows"]
        both = (await ac.get("/api/devices/modem/channels/history",
                             params={"channel_id": 1, "direction": "downstream"})).json()["rows"]
        none = (await ac.get("/api/devices/modem/channels/history", params={"channel_id": 99})).json()["rows"]
        bad = await ac.get("/api/devices/modem/channels/history", params={"direction": "sideways"})
    assert [r[2] for r in one] == [2, 2, 2] and {r[1] for r in one} == {"d"}
    assert len(up) == 3 and {r[1] for r in up} == {"u"}
    assert [(r[1], r[2]) for r in both] == [("d", 1)] * 3
    assert none == [] and bad.status_code == 422


async def test_channel_history_range_defaults_and_caps(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    now = time.time()
    await _seed_channels(repo, now)
    await repo.insert_line_sample(line_sample("modem", _snapshot(now - 5 * 86400, [_ch(7)])))
    async with _client() as ac:
        default = (await ac.get("/api/devices/modem/channels/history")).json()["rows"]
        week = (await ac.get("/api/devices/modem/channels/history", params={"start_time": now - 30 * 86400,
                                                                            "end_time": now})).json()["rows"]
        window = (await ac.get("/api/devices/modem/channels/history",
                               params={"start_time": now - 4000, "end_time": now - 3000})).json()["rows"]
        inverted = (await ac.get("/api/devices/modem/channels/history",
                                 params={"start_time": now, "end_time": now - 10})).json()["rows"]
    assert 7 not in {r[2] for r in default}                 # 5 days ago is outside the 24 h default
    assert 7 in {r[2] for r in week}                         # a 30 day request reaches back to the retention only
    assert min(r[0] for r in week) >= now - 7 * 86400 - 1    # the 9 day old reading is not there
    assert {r[0] for r in window} == {int(now - 3600 + 0.7)}
    assert inverted == []


async def test_channel_history_error_codes(app_state, repo):
    app_state.devices, app_state.repository = _devices(), repo
    async with _client() as ac:
        assert (await ac.get("/api/devices/nope/channels/history")).status_code == 404
        res = await ac.get("/api/devices/router/channels/history")      # no access line
        assert res.status_code == 400
    # a line that is not DOCSIS
    modem = app_state.devices.get("modem")
    modem.driver.line_technology = "gpon"
    async with _client() as ac:
        res = await ac.get("/api/devices/modem/channels/history")
    assert res.status_code == 400 and "DOCSIS" in res.json()["detail"]


async def test_channel_history_503_before_startup(app_state):
    async with _client() as ac:
        assert (await ac.get("/api/devices/modem/channels/history")).status_code == 503
    app_state.devices = _devices()
    async with _client() as ac:
        assert (await ac.get("/api/devices/modem/channels/history")).status_code == 503


def test_channel_history_rows_skip_other_technologies_and_unreadable_details():
    good = line_sample("modem", _snapshot(100.0, [_ch(1)]))
    other = good.__class__(**{**good.__dict__, "technology": "gpon", "ts": 101.0})
    broken = good.__class__(**{**good.__dict__, "detail": '{"v":9,"ch":[]}', "ts": 102.0})
    torn = good.__class__(**{**good.__dict__, "detail": '{"v":1,"ch":[[1,"d"]]}', "ts": 103.0})
    rows = channel_history_rows([good, other, broken, torn])
    assert rows == [[100, "d", 1, 1.0, 38.0, 0]]


# --- the security middleware on the new routes ------------------------------------------------------------------------

async def test_new_routes_need_the_token_and_carry_the_security_headers(app_state, repo, monkeypatch):
    app_state.devices, app_state.repository = _devices(), repo
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    paths = ("/api/devices/router/history?max_points=10", "/api/devices/modem/channels/history")
    async with _client() as ac:
        for path in paths:
            assert (await ac.get(path)).status_code == 401
            assert (await ac.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
            ok = await ac.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
            assert ok.status_code == 200
            assert ok.headers["x-content-type-options"] == "nosniff"
            assert "default-src" in ok.headers["content-security-policy"]


async def test_new_routes_reject_a_foreign_host(app_state, repo, monkeypatch):
    app_state.devices, app_state.repository = _devices(), repo
    monkeypatch.setattr(security_config, "allowed_hosts", ["test"])
    async with _client() as ac:
        for path in ("/api/devices/router/history?max_points=10", "/api/devices/modem/channels/history"):
            assert (await ac.get(path, headers={"Host": "evil.example"})).status_code == 421
            assert (await ac.get(path)).status_code == 200
