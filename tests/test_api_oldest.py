"""GET /api/oldest: the oldest stored time of each kind of data (a chart at All starts there and asks once)."""

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.db.oldest import TABLES, oldest_stored
from pengumon.main import app, security_config


@pytest.fixture
def open_access(monkeypatch):
    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", [])):
        monkeypatch.setattr(security_config, name, value)


async def _run(repo, *statements):
    async with repo._db.write() as db:
        for sql, params in statements:
            await db.execute(sql, params)


async def _get():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get("/api/oldest")


async def test_an_empty_database_has_no_oldest(repo):
    assert await oldest_stored(repo) == {kind: None for kind in TABLES}


async def test_each_kind_is_the_oldest_of_its_tiers_and_devices(repo):
    await _run(
        repo,
        ("INSERT INTO samples (ts, target, ok) VALUES (5000.5, 1, 1)", ()),
        ("INSERT INTO rollup_1m (minute, target, n, ok) VALUES (60, 1, 1, 1)", ()),  # 3600 s
        ("INSERT INTO rollup_15m (quarter, target, n, ok) VALUES (3, 1, 1, 1)", ()),  # 2700 s: the oldest of the probes
        ("INSERT INTO device_samples (device_id, ts, status) VALUES ('b', 900, 'ok')", ()),
        ("INSERT INTO device_samples (device_id, ts, status) VALUES ('a', 4000, 'ok')", ()),
        ("INSERT INTO device_samples (device_id, ts, status) VALUES ('c', 7000, 'ok')", ()),
        ("INSERT INTO device_rollup_15m (device_id, quarter) VALUES ('a', 2)", ()),  # 1800 s, before a's raw rows
        ("INSERT INTO mesh_rollup_15m (device_id, quarter, node_id, clients_sum, clients_n, clients_max) VALUES ('m', 10, 'n', 1, 1, 1)", ()),
        ("INSERT INTO line_samples (device_id, ts, technology, status) VALUES ('x', 12345, 'docsis', 'ok')", ()),
        ("INSERT INTO traffic_hourly (device_id, hour, client, up, down, read_at) VALUES ('t', 7200, 'c', 1, 1, 7300)", ()),
        ("INSERT INTO speed_tests (ts, trigger, provider, status) VALUES (99.5, 'manual', 'p', 'ok')", ()),
    )
    assert await oldest_stored(repo) == {"probes": 2700.0, "router": 900.0, "mesh": 9000.0, "line": 12345.0,
                                         "traffic": 7200.0, "speedtest": 99.5}


async def test_the_route_answers_the_same(app_state, repo, open_access):
    app_state.repository = repo
    await _run(repo, ("INSERT INTO speed_tests (ts, trigger, provider, status) VALUES (42, 'manual', 'p', 'ok')", ()))
    body = (await _get()).json()
    assert body["speedtest"] == 42.0 and body["probes"] is None and set(body) == set(TABLES)


async def test_the_route_without_a_repository_says_none_everywhere(app_state, open_access):
    app_state.repository = None
    assert (await _get()).json() == {kind: None for kind in TABLES}
