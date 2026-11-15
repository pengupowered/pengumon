"""The router's and the mesh's storage tiers (schema v21): rollups written with every poll, the history read from raw
where it exists and from the minute and quarter-hour tiers beyond (and equal to the raw answer while raw exists), what
retention keeps, the migration that builds the tiers from the raw rows, and the hourly app lists.

Time is fixed (NOW, patched into the two query modules) so a boundary never moves while a test runs. One seeding serves
both answers: the "raw" answer is read with retention_days=7 (raw covers everything seeded), the "tiered" one with
retention_days=1, which makes everything older than a day come from the tiers: the two must agree."""

import sqlite3
from types import SimpleNamespace

import pytest

from pengumon.capabilities import TrafficApps
from pengumon.db import clients as clients_module
from pengumon.db import device_tiers
from pengumon.db import devices as devices_module
from pengumon.db import migrations
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import DeviceSample, MeshCount

DAY = 86400
NOW = 1_800_000_450.0                    # not on a minute or a quarter
BASE = 1_799_740_800                     # a multiple of 900, 3 days before NOW (rounded to a quarter)
STEP = 20
SAMPLES = (3 * DAY) // STEP - 20         # the last one is a few minutes before NOW
FIGURES = ("wan_rx_mbps", "wan_tx_mbps", "cpu_pct", "mem_pct", "client_count", "cpu_temp_c")


@pytest.fixture(autouse=True)
def _fixed_time(monkeypatch):
    clock = SimpleNamespace(time=lambda: NOW)
    monkeypatch.setattr(devices_module, "time", clock)
    monkeypatch.setattr(clients_module, "time", clock)


def _sample(k: int) -> DeviceSample:
    """Poll k: some figures missing now and then (a poll that could not measure), counts on every third poll."""
    ts = BASE + k * STEP
    counts = {}
    if k % 3 == 0:
        online = 10 + k % 7
        counts = dict(clients_online=online, clients_wired=4, clients_wifi_2g=k % 3 + 1, clients_wifi_5g=online - 6,
                      clients_wifi_other=0)
    return DeviceSample(
        "r1", ts, "healthy", wan_rx_mbps=None if k % 11 == 0 else (k * 7 % 101) / 4, wan_tx_mbps=(k * 3 % 53) / 4,
        cpu_pct=(k * 5 % 97) / 2, mem_pct=40 + k % 9, client_count=None if k % 13 == 0 else 5 + k % 4,
        cpu_temp_c=None if k % 5 else 50.5 + k % 6, wan_link_mbps=None if k % 17 else 1000 + 1500 * (k % 2), **counts)


@pytest.fixture
async def seeded(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "tiers.db"))
    await repo.initialize()
    mesh = []
    for k in range(SAMPLES):
        await repo.insert_device_sample(_sample(k))
        if k % 9 == 0:
            await repo.insert_mesh_counts([MeshCount("r1", BASE + k * STEP, n, (k // 9) % 5 + i) for i, n in
                                           enumerate(("n1", "n2"))])
    await repo.insert_device_sample(DeviceSample("other", BASE + 100, "healthy", wan_rx_mbps=99.0))
    yield repo
    await repo.close()


def _close(a, b):
    assert len(a) == len(b), (len(a), len(b))
    for ra, rb in zip(a, b):
        assert len(ra) == len(rb)
        for x, y in zip(ra, rb):
            assert (x is None and y is None) or (x == y if isinstance(y, str) else x == pytest.approx(y, rel=1e-9, abs=1e-9)), (ra, rb)


def _boundary() -> int:
    return device_tiers.raw_boundary(NOW, 1, 60)


async def _both(repo, method, *args):
    repo.retention_days = 7
    raw = await getattr(repo, method)(*args)
    repo.retention_days = 1
    tiered = await getattr(repo, method)(*args)
    repo.retention_days = 7
    return raw, tiered


# --- what is written with every poll ---------------------------------------------------------------------------------

async def test_a_poll_writes_its_minute_and_quarter_exact_from_the_raw_rows(seeded):
    con = sqlite3.connect(seeded.db_path)
    minute = (BASE + 5 * DAY // 4) // 60       # a minute well inside the data
    polls = [_sample(k) for k in range(SAMPLES) if int((BASE + k * STEP) // 60) == minute]
    assert len(polls) == 3
    row = con.execute("SELECT wan_rx_mbps_sum, wan_rx_mbps_n, wan_rx_mbps_max, cpu_temp_c_n, wan_link_min, "
                      "clients_online_n, clients_online_sum FROM device_rollup_1m WHERE device_id = 'r1' AND minute = ?",
                      (minute,)).fetchone()
    rx = [p.wan_rx_mbps for p in polls if p.wan_rx_mbps is not None]
    online = [p.clients_online for p in polls if p.clients_online is not None]
    links = [p.wan_link_mbps for p in polls if p.wan_link_mbps is not None]
    assert row == (pytest.approx(sum(rx)), len(rx), max(rx), sum(p.cpu_temp_c is not None for p in polls),
                   min(links) if links else None, len(online), sum(online) if online else None)
    q = minute // 15
    quarter = con.execute("SELECT wan_tx_mbps_sum, wan_tx_mbps_n FROM device_rollup_15m WHERE device_id = 'r1' "
                          "AND quarter = ?", (q,)).fetchone()
    tx = [_sample(k).wan_tx_mbps for k in range(SAMPLES) if int((BASE + k * STEP) // 900) == q]
    assert quarter == (pytest.approx(sum(tx)), len(tx))
    # another device has its own rows
    assert con.execute("SELECT COUNT(*) FROM device_rollup_1m WHERE device_id = 'other'").fetchone() == (1,)
    con.close()


async def test_mesh_counts_write_their_quarters(seeded):
    con = sqlite3.connect(seeded.db_path)
    q = (BASE + DAY) // 900
    got = con.execute("SELECT node_id, clients_sum, clients_n, clients_max FROM mesh_rollup_15m WHERE device_id = 'r1' "
                      "AND quarter = ? ORDER BY node_id", (q,)).fetchall()
    for node, i in (("n1", 0), ("n2", 1)):
        values = [(k // 9) % 5 + i for k in range(0, SAMPLES, 9) if int((BASE + k * STEP) // 900) == q]
        assert (node, sum(values), len(values), max(values)) in got
    con.close()


# --- the merge equals the raw answer ----------------------------------------------------------------------------------

@pytest.mark.parametrize("width", [900, 1800, 3600, 7200])
async def test_router_history_from_tiers_equals_raw(seeded, width):
    """Raw (7 days kept) against raw for the last day and the tiers before it: whole quarters from the quarter tier,
    the partial quarters at the edges from the minute tier. The range starts and ends inside a bucket."""
    start = BASE + 3600 + 13 * 60          # a whole minute, inside a bucket and a quarter
    end = BASE + 3 * DAY - 7 * 3600 - 17.0
    raw, tiered = await _both(seeded, "get_device_samples_bucketed", "r1", start, end, width)
    assert len(raw) > 20
    _close(raw, tiered)


async def test_the_range_reaching_into_the_raw_part_equals_raw_too(seeded):
    start = BASE + 7 * 3600                  # a quarter edge: bucket 900 is whole
    end = NOW - 500
    raw, tiered = await _both(seeded, "get_device_samples_bucketed", "r1", start, end, 900)
    assert end > _boundary() > start
    _close(raw, tiered)


@pytest.mark.parametrize("width", [60, 300, 600])
async def test_minute_buckets_come_from_the_minute_tier_and_equal_raw(seeded, width):
    """A range with no whole quarter in the tier part has only minute edges: buckets of whole minutes are exact."""
    boundary = _boundary()
    start = boundary - 11 * 60
    raw, tiered = await _both(seeded, "get_device_samples_bucketed", "r1", start, NOW - 100, width)
    assert any(r[0] < boundary for r in tiered)
    _close(raw, tiered)


async def test_a_bucket_narrower_than_the_tier_is_widened_to_it(seeded):
    """10-second buckets over a stretch only the quarter tier holds come out 900 s wide; raw ones stay 10 s."""
    seeded.retention_days = 1
    rows = await seeded.get_device_samples_bucketed("r1", BASE + 3600, BASE + 3 * 3600, 10)
    # eight quarters, and the minute the range ends in (minutes are widened to 60 s only)
    assert [r[0] for r in rows] == [BASE + 3600 + i * 900 for i in range(8)] + [BASE + 3 * 3600]
    rows = await seeded.get_device_samples_bucketed("r1", NOW - 1200, NOW - 900, 10)
    assert {r[0] % 20 for r in rows} == {BASE % 20}  # polls every 20 s: raw buckets, not widened


async def test_the_start_inside_a_minute_is_rounded_out_to_its_minute_where_only_tiers_have_it(seeded):
    start = BASE + 3600 + 30.0
    end = BASE + 4 * 3600 + 59.0
    raw, tiered = await _both(seeded, "get_device_samples_bucketed", "r1", start, end, 3600)
    whole_minute = await seeded.get_device_samples_bucketed("r1", BASE + 3600, end, 3600)  # raw, from the minute's start
    assert raw[0][1] != pytest.approx(tiered[0][1])
    _close(whole_minute, tiered)


async def test_a_range_older_than_raw_reads_the_tiers(seeded):
    """Raw rows pruned (retention 1 day): the minutes and quarters still give the history."""
    async with seeded._db.write() as db:  # what the hourly cleanup does (with the real clock) to rows older than raw's days
        await db.execute("DELETE FROM device_samples WHERE ts < ?", (NOW - DAY,))
        await db.execute("DELETE FROM mesh_samples WHERE ts < ?", (NOW - DAY,))
    seeded.retention_days = 1
    start, end = BASE + 3600, BASE + 2 * 3600
    rows = await seeded.get_device_samples_bucketed("r1", start, end, 900)
    assert [r[0] for r in rows] == [start + 900 * i for i in range(5)] and rows[0][1] is not None
    expected = [_sample(k).wan_rx_mbps for k in range(SAMPLES) if start <= BASE + k * STEP < start + 900
                and _sample(k).wan_rx_mbps is not None]
    assert rows[0][1] == pytest.approx(sum(expected) / len(expected))
    assert (await seeded.get_client_counts("r1", start, end, 900))[0][1] is not None
    assert (await seeded.get_mesh_counts("r1", start, end, 900))[0][2] is not None


async def test_past_the_minute_tier_the_edges_round_out_to_whole_quarters(seeded):
    async with seeded._db.write() as db:
        await db.execute("DELETE FROM device_rollup_1m WHERE minute < ?", ((BASE + 2 * DAY) // 60,))
    seeded.retention_days = 1
    start = BASE + 3600 + 5 * 60          # inside a quarter; the minutes are gone: its whole quarter is read
    rows = await seeded.get_device_samples_bucketed("r1", start, BASE + 7200, 900)
    assert rows[0][0] == BASE + 3600
    q = [_sample(k).wan_tx_mbps for k in range(SAMPLES) if BASE + 3600 <= BASE + k * STEP < BASE + 4500]
    assert rows[0][3] == pytest.approx(sum(q) / len(q))


async def test_client_counts_from_tiers_equal_raw(seeded):
    start, end = BASE + 3600 + 13 * 60, BASE + 3 * DAY - 7 * 3600 - 17.0
    for width in (900, 3600):
        raw, tiered = await _both(seeded, "get_client_counts", "r1", start, end, width)
        assert len(raw) > 10
        _close(raw, tiered)
    boundary = _boundary()
    raw, tiered = await _both(seeded, "get_client_counts", "r1", boundary - 660, NOW - 100, 60)
    _close(raw, tiered)


async def test_mesh_counts_from_tiers_equal_raw(seeded):
    start, end = BASE + 3600, BASE + 3 * DAY - 7 * 3600 - 17.0
    for width in (900, 1800, 3600):
        raw, tiered = await _both(seeded, "get_mesh_counts", "r1", start, end, width)
        assert len(raw) > 10
        _close(raw, tiered)
    raw, tiered = await _both(seeded, "get_mesh_counts", "r1", BASE + 3600, NOW - 100, 900)
    _close(raw, tiered)


async def test_a_range_inside_raw_is_the_raw_query(seeded):
    """Nothing about the tiers changes an answer raw covers (the default retention keeps all of the seeded days)."""
    rows = await seeded.get_device_samples_bucketed("r1", BASE, BASE + 3600, 60)
    assert len(rows) == 61 and rows[0][0] == BASE and len(rows[0]) == 14
    assert await seeded.get_device_samples_bucketed("nobody", BASE, NOW, 60) == []


# --- retention ----------------------------------------------------------------------------------------------------------

async def test_retention_cuts_each_tier_at_its_time(tmp_path, monkeypatch):
    import time as real_time
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    now = real_time.time()
    for age in (40, 20, 1):
        await repo.insert_device_sample(DeviceSample("r1", now - age * DAY, "healthy", cpu_pct=1.0))
        await repo.insert_mesh_counts([MeshCount("r1", now - age * DAY, "n1", 2)])
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=30, router_retention_days=100,
                                         mesh_retention_days=100)
    assert (removed["device_rollup_1m"], removed["device_rollup_15m"], removed["mesh_rollup_15m"]) == (1, 0, 0)
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=30, router_retention_days=30,
                                         mesh_retention_days=30)
    assert (removed["device_rollup_1m"], removed["device_rollup_15m"], removed["mesh_rollup_15m"]) == (0, 1, 1)
    await repo.close()


# --- the migration ----------------------------------------------------------------------------------------------------

async def test_v21_builds_the_tiers_from_the_raw_rows_present(tmp_path):
    path = str(tmp_path / "v20.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    for k in range(0, 4000):
        await repo.insert_device_sample(_sample(k))
        if k % 9 == 0:
            await repo.insert_mesh_counts([MeshCount("r1", BASE + k * STEP, "n1", k % 5)])
    await repo.close()
    con = sqlite3.connect(path)
    want = {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
            for t in ("device_rollup_1m", "device_rollup_15m", "mesh_rollup_15m")}
    for t in ("device_rollup_1m", "device_rollup_15m", "mesh_rollup_15m", "traffic_app_hours"):
        con.execute(f"DROP TABLE {t}")
    con.execute("PRAGMA user_version=20")
    con.commit()
    con.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    con = sqlite3.connect(path)
    for t, rows in want.items():
        got = con.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
        assert len(rows) > 10 and len(got) == len(rows)
        for a, b in zip(got, rows):
            assert a == pytest.approx(b)
    assert con.execute("SELECT COUNT(*) FROM traffic_app_hours").fetchone() == (0,)
    con.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION  # runs once


# --- the hourly app lists ------------------------------------------------------------------------------------------

def _day_of(window_end):
    return None


async def test_every_round_keeps_its_app_windows_and_they_are_read_back_by_range(repo):
    hour = 3600
    t0 = 1_800_000_000
    for i in range(3):
        end = t0 + i * hour
        await repo.store_traffic_apps("r1", [TrafficApps("", end, (("Web", 10 + i, 20),)),
                                             TrafficApps("aa", end, (("Video", 1, 2 + i),))], end + 5, _day_of)
    await repo.store_traffic_apps("r1", [TrafficApps("", t0, (("Web", 99, 99),))], t0 + 60, _day_of)  # a re-read
    await repo.store_traffic_apps("other", [TrafficApps("", t0, (("X", 1, 1),))], t0, _day_of)
    # the hourly windows ending first_end <= window_end <= last_end, newest first, each (client, window_end) once
    # (the latest one of each client is also in traffic_apps: the same list)
    rows = await repo.get_traffic_app_windows("r1", t0, t0 + 2 * hour)
    assert sorted(rows, key=lambda r: (-r[1], r[0])) == rows
    assert sorted(rows) == sorted([("", t0, [["Web", 99, 99]]), ("aa", t0, [["Video", 1, 2]]),
                                   ("", t0 + hour, [["Web", 11, 20]]), ("aa", t0 + hour, [["Video", 1, 3]]),
                                   ("", t0 + 2 * hour, [["Web", 12, 20]]), ("aa", t0 + 2 * hour, [["Video", 1, 4]])])
    only = await repo.get_traffic_app_windows("r1", t0 + 1, t0 + hour)
    assert {(c, w) for c, w, _ in only} == {("", t0 + hour), ("aa", t0 + hour)}
    only = await repo.get_traffic_app_windows("r1", t0, t0 + 2 * hour, client="aa")
    assert [w for _, w, _ in only] == [t0 + 2 * hour, t0 + hour, t0]
    assert await repo.get_traffic_app_windows("r1", t0 + 0.5, t0 + hour - 1) == []


async def test_the_app_windows_are_kept_31_days(repo):
    import time as real_time
    now = int(real_time.time())
    for age in (40, 32, 30, 1):
        await repo.store_traffic_apps("r1", [TrafficApps("", now - age * DAY, (("Web", 1, 1),))], now, _day_of)
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=90)
    assert removed["traffic_app_hours"] == 2
    # the pruned ones are gone from the hourly table (the latest one of the client stays in traffic_apps)
    async with repo._db.read() as db:
        async with db.execute("SELECT window_end FROM traffic_app_hours WHERE device_id = 'r1' ORDER BY window_end") as c:
            ends = [r[0] for r in await c.fetchall()]
    assert ends == [now - 30 * DAY, now - 1 * DAY]
