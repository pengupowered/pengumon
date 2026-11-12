"""The 10-second and quarter-hour rollups (schema v18): what the flush writes, that every report and history query that
reads them gives what the per-minute (or raw) query gives, across daylight-saving changes and zones whose offset is not
whole hours, what retention keeps, and the migration that builds them.

Characterization: each query is run twice on the same data, once the new way (quarters with per-minute edges) and once
the way it was before v18 (rollup_1m alone; the per-minute code path is kept for zones that are not whole quarters and
is forced here), and the answers must be equal. Latencies are multiples of 1/4 ms in the seeded rollups, so sums are
exact in floating point and equal means equal, whatever order the additions are made in."""

import asyncio
import random
import sqlite3
import time
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from pengumon.db import migrations
from pengumon.db import probes as probes_module
from pengumon.db.repository import DatabaseRepository
from pengumon.db.rollups import split_minutes
from pengumon.db.schema import UPSERT_ROLLUP_15M_FROM_1M
from pengumon.models.probes import ProbeSample
from pengumon.reports import queries

DAY = 86400
UTC = timezone.utc
ZONES = {
    "UTC": UTC,
    "New_York": ZoneInfo("America/New_York"),        # DST: 2026-03-08 07:00 UTC, 2026-11-01 06:00 UTC
    "London": ZoneInfo("Europe/London"),             # DST: 2026-03-29 01:00 UTC, 2026-10-25 01:00 UTC
    "Kolkata": ZoneInfo("Asia/Kolkata"),             # +05:30
    "Kathmandu": ZoneInfo("Asia/Kathmandu"),         # +05:45
    "Lord_Howe": ZoneInfo("Australia/Lord_Howe"),    # +10:30 / +11:00 (a half-hour DST change, 2026-04-04 15:00 UTC)
    "+05:45": timezone(timedelta(hours=5, minutes=45)),
    "-00:07": timezone(timedelta(minutes=-7)),       # not whole quarters: the per-minute path, both ways
}
# Seeded stretches of per-minute rollups (UTC), each across daylight-saving changes of the zones above.
BLOCKS = [(1772755200, 1773100800),   # 2026-03-06 .. 03-10: New York springs forward
          (1774569600, 1774915200),   # 2026-03-27 .. 03-31: London springs forward
          (1775174400, 1775433600),   # 2026-04-03 .. 04-06: Lord Howe goes back half an hour
          (1792713600, 1793664000)]   # 2026-10-23 .. 11-03: London and New York go back
TARGETS = ("gw", "isp", "pub")


# --- seeding -----------------------------------------------------------------------------------------------------------

def _minute_rows(ids, rng):
    """Per-minute rollups for BLOCKS: busy and quiet minutes, minutes with fewer than 10 probes, dead minutes (every
    probe of 10 or more lost), minutes without percentiles (as before v11) and missing minutes."""
    rows = []
    for lo, hi in BLOCKS:
        for minute in range(lo // 60, hi // 60):
            for t in TARGETS:
                if rng.random() < 0.03:
                    continue  # nothing stored for that minute
                n = rng.choice((60, 60, 60, 59, 61, 8, 30))
                roll = rng.random()
                ok = 0 if roll < 0.02 else (n - rng.randint(1, n) if roll < 0.1 else n)
                if ok:
                    lats = [rng.randint(4, 400) / 4 for _ in range(3)]
                    lat_sum, lat_min, lat_max = sum(lats) / 3 * ok, min(lats), max(lats)
                    lat_sum = round(lat_sum * 4) / 4
                    p10, p90 = (None, None) if rng.random() < 0.05 else (lat_min, lat_max)
                else:
                    lat_sum = lat_min = lat_max = p10 = p90 = None
                jit_n = rng.choice((0, ok))
                jit_sum = rng.randint(0, 40) / 4 * jit_n if jit_n else None
                rows.append((minute, ids[t], n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, p10, p90))
    return rows


@pytest.fixture(scope="module")
def seeded_file(tmp_path_factory):
    """A database file with BLOCKS of per-minute rollups and the quarter-hour rollups derived from them, as the flush
    derives them."""
    path = str(tmp_path_factory.mktemp("tiers") / "tiers.db")
    asyncio.run(migrations.migrate(path))
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO targets (key) VALUES (?)", [(t,) for t in TARGETS])
    ids = dict(con.execute("SELECT key, id FROM targets"))
    con.executemany("INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, "
                    "lat_p90) VALUES (?,?,?,?,?,?,?,?,?,?,?)", _minute_rows(ids, random.Random(18)))
    for lo, hi in BLOCKS:
        con.execute(UPSERT_ROLLUP_15M_FROM_1M, (lo // 60, hi // 60))
    con.commit()
    con.close()
    return path


@pytest.fixture
async def seeded(seeded_file, tmp_path):
    copy = str(tmp_path / "copy.db")
    src, dst = sqlite3.connect(seeded_file), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    repo = DatabaseRepository(db_path=copy)
    await repo.initialize()
    yield repo
    await repo.close()


def _windows(rng, count):
    """(start, end) pairs inside one block or across two: on quarters, on minutes, anywhere."""
    out = []
    for i in range(count):
        lo, hi = BLOCKS[rng.randrange(len(BLOCKS))]
        start = rng.uniform(lo - 3600, hi - 3 * 3600)
        if i % 3 == 0:
            start = start // 900 * 900
        elif i % 3 == 1:
            start = start // 60 * 60
        end = rng.uniform(start + 600, min(hi + 3600, start + 3 * DAY)) if i % 5 else start + 30 * DAY
        out.append((start, end))
    return out


def _per_minute(monkeypatch):
    """The queries as they were before v18: rollup_1m alone."""
    monkeypatch.setattr(queries, "quarter_aligned", lambda *a: False)
    monkeypatch.setattr(probes_module, "STATS_QUARTERS_MIN_SECONDS", float("inf"))


def _rows(rows):
    return sorted(tuple(sorted(r.items())) for r in rows)


# --- split_minutes ------------------------------------------------------------------------------------------------------

def test_a_window_splits_into_whole_quarters_and_per_minute_edges_or_whole_quarters_past_rollup_1m():
    for first in range(0, 64):
        for end in [*range(first + 1, 80), None]:
            for floor in (None, *range(0, 80, 2)):
                q_lo, q_hi, edges = split_minutes(first, end, floor)
                read = [m for q in range(q_lo, q_hi if q_hi is not None else 8) for m in range(q * 15, q * 15 + 15)]
                read += [m for a, b in edges for m in range(a, b)]
                assert len(read) == len(set(read)), (first, end, floor)  # nothing read twice
                assert all(floor is not None and a >= floor for a, _ in edges)  # rollup_1m only where it has minutes
                limit = end if end is not None else 120
                wanted = set(range(first, limit))
                got = {m for m in read if m < 120}
                if floor is not None and floor <= first:
                    assert got == wanted, (first, end, floor, q_lo, q_hi, edges)
                else:  # rounded out to the quarters the window touches, no further
                    lo, hi = first // 15 * 15, -(-limit // 15) * 15
                    assert wanted <= got and all(lo <= m < hi for m in got), (first, end, floor)


# --- the reports: the same answers as rollup_1m alone ------------------------------------------------------------------

async def _report_answers(repo, start, end, tz):
    return (_rows(await queries.loss_grid_rows(repo, start, tz, now=end)),
            _rows(await queries.loss_grid_rows(repo, start, tz, exclude=("isp",), per_day=False, now=end)),
            _rows(await queries.hourly_loss(repo, start, tz, now=end)),
            _rows(await queries.daily_availability(repo, start, tz, now=end)))


@pytest.mark.parametrize("zone", list(ZONES))
async def test_report_queries_give_the_per_minute_answers(seeded, zone, monkeypatch):
    tz = ZONES[zone]
    windows = _windows(random.Random(zone), 24)
    new = [await _report_answers(seeded, start, end, tz) for start, end in windows]
    assert any(sum(dict(r)["sent"] for r in answer[0]) for answer in new)  # the windows hold probes
    _per_minute(monkeypatch)
    for (start, end), got in zip(windows, new):
        assert got == await _report_answers(seeded, start, end, tz), (zone, start, end)
    # the daily totals as the report lists them: by day
    monkeypatch.undo()
    days = [r["day"] for r in await queries.daily_availability(seeded, BLOCKS[-1][0] + 420.0, tz, now=BLOCKS[-1][1])]
    assert days == sorted(days) and len(set(days)) >= 11


@pytest.mark.parametrize("zone", list(ZONES))
async def test_uptime_blocks_give_the_per_minute_answers(seeded, zone, monkeypatch):
    tz = ZONES[zone]
    rng = random.Random("blocks" + zone)
    windows = [(s // 900 * 900 if i % 2 else s, e) for i, (s, e) in enumerate(_windows(rng, 16))]
    new = [await queries.block_stats(seeded, s, e, 900, UTC) for s, e in windows]
    new_days = [await queries.block_stats(seeded, s, e, 86400, tz) for s, e in windows]
    _per_minute(monkeypatch)
    for (s, e), got, got_days in zip(windows, new, new_days):
        assert _rows(got) == _rows(await queries.block_stats(seeded, s, e, 900, UTC)), (zone, s, e)
        assert _rows(got_days) == _rows(await queries.block_stats(seeded, s, e, 86400, tz)), (zone, s, e)
    assert any(r["dead_minutes"] for rows in new for r in rows)  # the data has dead minutes to count


async def test_the_quarter_path_is_taken_for_whole_quarter_zones_only(seeded, monkeypatch):
    seen = []
    real = queries.merged_rows_sql
    monkeypatch.setattr(queries, "merged_rows_sql", lambda *a, **k: seen.append(1) or real(*a, **k))
    start, end = BLOCKS[0][0] + 1000, BLOCKS[0][1]
    for zone in ("UTC", "New_York", "Kathmandu", "Lord_Howe", "+05:45"):
        await queries.loss_grid_rows(seeded, start, ZONES[zone], now=end)
    assert len(seen) == 5
    await queries.loss_grid_rows(seeded, start, ZONES["-00:07"], now=end)
    assert len(seen) == 5


async def test_stats_give_the_per_minute_answers(seeded, monkeypatch):
    rng = random.Random(7)
    windows = _windows(rng, 30)
    new = [(await seeded.get_rollup_stats(s, e), await seeded.get_hop_stats(s, e)) for s, e in windows]
    _per_minute(monkeypatch)
    for (s, e), (rollup, hop) in zip(windows, new):
        assert rollup == await seeded.get_rollup_stats(s, e), (s, e)
        assert hop == await seeded.get_hop_stats(s, e), (s, e)
    assert any(r["sent"] for rows, _ in new for r in rows)


async def test_quarter_history_buckets_equal_the_per_minute_buckets(seeded):
    rng = random.Random(11)
    floor = sqlite3.connect(seeded.db_path).execute("SELECT MIN(minute) FROM rollup_1m").fetchone()[0]
    con = sqlite3.connect(seeded.db_path)
    try:
        for start, end in _windows(rng, 30):
            for bucket in (900, 1800, 3600, 4 * 3600, 86400):
                for ids in (None, [1], [1, 3]):
                    new = con.execute(*seeded._quarter_query(start, end, bucket, floor, ids)).fetchall()
                    old = con.execute(*seeded._rollup_query(start, end, bucket, ids)).fetchall()
                    assert sorted(new) == sorted(old), (start, end, bucket, ids)
    finally:
        con.close()


async def test_past_the_per_minute_retention_quarters_keep_the_history_and_edges_round_to_the_quarter(seeded,
                                                                                                      monkeypatch):
    """rollup_1m pruned up to a cutoff in the middle of a quarter: windows that begin on a quarter before it give what
    the per-minute rollups gave before they were pruned; a window that begins inside a quarter before it counts that
    whole quarter (rounded out)."""
    lo, hi = BLOCKS[-1]
    cutoff = (lo + 4 * DAY) // 60 + 7  # minute 7 of a quarter
    tz = ZONES["New_York"]
    on_quarter = [(lo + k * 3600.0, hi) for k in (0, 5, 30)]
    inside = [(lo + 3600 + 7 * 60 + 13.0, hi), (lo + 2 * DAY + 11 * 60.0, hi)]
    _per_minute(monkeypatch)
    before = {w: (await queries.daily_availability(seeded, w[0], tz, now=w[1]),
                  await queries.block_stats(seeded, w[0], w[1], 900, UTC),
                  await seeded.get_hop_stats(*w)) for w in on_quarter}
    rounded = {w: (await queries.daily_availability(seeded, w[0] // 900 * 900, tz, now=w[1]),
                   await queries.block_stats(seeded, w[0] // 900 * 900, w[1], 900, UTC)) for w in inside}
    monkeypatch.undo()
    with sqlite3.connect(seeded.db_path) as db:
        db.execute("DELETE FROM rollup_1m WHERE minute < ?", (cutoff,))
    for w, (daily, blocks, hops) in before.items():
        assert _rows(await queries.daily_availability(seeded, w[0], tz, now=w[1])) == _rows(daily)
        assert _rows(await queries.block_stats(seeded, w[0], w[1], 900, UTC)) == _rows(blocks)
        assert await seeded.get_hop_stats(*w) == hops
    for w, (daily, blocks) in rounded.items():
        assert _rows(await queries.daily_availability(seeded, w[0], tz, now=w[1])) == _rows(daily)
        assert _rows(await queries.block_stats(seeded, w[0], w[1], 900, UTC)) == _rows(blocks)


# --- the flush and the 10-second buckets -------------------------------------------------------------------------------

def _percentile_rank(values, fraction_tenths):
    """Nearest rank: the smallest rank r with r * 10 >= len * fraction_tenths (1-based)."""
    ordered = sorted(values)
    r = next(r for r in range(1, len(ordered) + 1) if r * 10 >= len(ordered) * fraction_tenths)
    return ordered[r - 1]


def _random_samples(rng, start, seconds, targets=("a", "b")):
    out = []
    for t in targets:
        ts = start + rng.random()
        while ts < start + seconds:
            ok = rng.random() > 0.08
            out.append(ProbeSample(target_id=t, timestamp=ts, success=ok,
                                   latency_ms=round(rng.uniform(1, 80), 3) if ok else None,
                                   jitter_ms=round(rng.uniform(0, 5), 3) if rng.random() < 0.7 else None,
                                   packet_loss_pct=0.0 if ok else 100.0, error=None if ok else "x"))
            ts += rng.uniform(0.3, 1.7)
    return out


async def test_the_flush_writes_exact_10_second_and_quarter_rollups(repo):
    rng = random.Random(3)
    start = int(time.time() // 900) * 900 - 2 * 3600 - 37  # batches straddle slots, minutes and quarters
    samples = _random_samples(rng, start, 1800)
    rng.shuffle(samples)
    for at in range(0, len(samples), 97):  # flushed in batches, in no particular order
        await repo.insert_samples(samples[at:at + 97])
        await repo.flush_samples()
    await repo.insert_samples(samples[:50])  # duplicates: ignored by the samples, and the rollups stay exact
    await repo.flush_samples()

    ids = repo._target_ids
    expected = {}
    for s in samples:
        expected.setdefault((int(s.timestamp // 10), ids[s.target_id]), []).append(s)
    con = sqlite3.connect(repo.db_path)
    try:
        got = {(slot, t): rest for slot, t, *rest in con.execute(
            "SELECT slot, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90 FROM rollup_10s")}
        assert set(got) == set(expected)
        for key, group in expected.items():
            lat = [s.latency_ms for s in group if s.success]
            jit = [s.jitter_ms for s in group if s.jitter_ms is not None]
            n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, p10, p90 = got[key]
            assert (n, ok, jit_n) == (len(group), len(lat), len(jit))
            assert (lat_min, lat_max) == ((min(lat), max(lat)) if lat else (None, None))
            assert (p10, p90) == ((_percentile_rank(lat, 1), _percentile_rank(lat, 9)) if lat else (None, None))
            assert lat_sum == pytest.approx(sum(lat)) if lat else lat_sum is None
            assert jit_sum == pytest.approx(sum(jit)) if jit else jit_sum is None
        quarters = con.execute("SELECT * FROM rollup_15m ORDER BY quarter, target").fetchall()
        assert quarters == con.execute("""
            SELECT minute / 15, target, SUM(n), SUM(ok), SUM(lat_sum), MIN(lat_min), MAX(lat_max), SUM(jit_sum),
                   SUM(jit_n), SUM(lat_p10 * ok), SUM(CASE WHEN lat_p10 IS NOT NULL THEN ok END), SUM(lat_p90 * ok),
                   SUM(CASE WHEN lat_p90 IS NOT NULL THEN ok END), SUM(CASE WHEN ok = 0 AND n >= 10 THEN 1 ELSE 0 END)
            FROM rollup_1m GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
        assert sum(q[2] for q in quarters) == len(samples) and len({q[0] for q in quarters}) == 3
    finally:
        con.close()


async def test_10_second_buckets_equal_the_raw_buckets_with_their_exact_band(repo):
    """Bucket 10: every column what the raw query with its exact band gives, the partial slots at either end of the
    range included. Wider buckets of whole slots: the same counts and loss, latency and jitter to the last digit (see
    below); the band the slots' percentiles weighted by their answered probes."""
    rng = random.Random(5)
    start = int(time.time()) - 3 * 3600
    samples = _random_samples(rng, start, 3600, targets=("a", "b", "c"))
    await repo.insert_samples(samples)
    await repo.flush_samples()
    con = sqlite3.connect(repo.db_path)
    try:
        for lo, hi in [(start + 3.25, start + 3000.5), (start + 10, start + 1200), (start + 17.5, start + 19.0),
                       (start + 20, start + 20), (start - 100, start + 4000)]:
            for ids in (None, [repo._target_ids["b"]]):
                new = con.execute(*repo._slot_query(lo, hi, 10, ids)).fetchall()
                old = con.execute(*repo._raw_bucket_query(lo, hi, 10, ids, band=True)).fetchall()
                assert sorted(new) == sorted(old), (lo, hi, ids)
                for width in (20, 30, 50, 120):
                    wide = con.execute(*repo._slot_query(lo, hi, width, ids)).fetchall()
                    raw = con.execute(*repo._raw_bucket_query(lo, hi, width, ids, band=True)).fetchall()
                    wide_rows, raw_rows = sorted(r[:7] for r in wide), sorted(r[:7] for r in raw)
                    # counts and loss exact; latency and jitter are sums added in another order (slot by slot), so a
                    # mean that falls on a rounding tie (x.xx5) can round the other way: within 0.01
                    assert [(r[:2], r[3], r[5:]) for r in wide_rows] == [(r[:2], r[3], r[5:]) for r in raw_rows]
                    for w, r in zip(wide_rows, raw_rows):
                        for i in (2, 4):
                            assert (w[i] is None) == (r[i] is None) and (w[i] is None or abs(w[i] - r[i]) <= 0.0100001)
                    tens = {(r[0], r[1]): r for r in new}
                    for target, bucket, *_, p10, p90 in wide:
                        slots = [tens[(target, b)] for b in range(bucket, bucket + width, 10) if (target, b) in tens]
                        weighted = [(s[7], s[5]) for s in slots if s[7] is not None]
                        if not weighted:
                            assert p10 is None
                            continue
                        # the slots' unrounded percentiles are in rollup_10s; their rounded ones are within 0.005
                        mean = sum(p * k for p, k in weighted) / sum(k for _, k in weighted)
                        assert p10 == pytest.approx(mean, abs=0.011)
    finally:
        con.close()


async def test_history_asks_rollup_10s_for_whole_tens_and_widens_to_whole_tens(repo, monkeypatch):
    start = int(time.time()) - 2 * 3600
    await repo.insert_samples(_random_samples(random.Random(9), start, 3600))
    rows, width = await repo.get_history_buckets(start, start + 3600, bucket_seconds=10)
    assert width == 10 and rows and all(r["latency_p10_ms"] is not None for r in rows if r["successful_count"])
    monkeypatch.setattr(probes_module, "HISTORY_ROW_CAP", 400)  # 3600 s x 2 targets / 400 rows: 18 s buckets at least
    rows, width = await repo.get_history_buckets(start, start + 3600, bucket_seconds=10)
    assert width == 20 and 0 < len(rows) <= 400
    rows, width = await repo.get_history_buckets(start, start + 3600, bucket_seconds=2)
    assert width == 18  # not whole tens: the raw buckets, as before
    used = []
    real = repo._slot_query
    monkeypatch.setattr(repo, "_slot_query", lambda *a, **k: used.append(a[2]) or real(*a, **k))
    await repo.get_history_buckets(start, start + 3600, bucket_seconds=30)
    await repo.get_history_buckets(start, start + 3600, bucket_seconds=15)
    assert used == [30]


async def test_history_of_whole_quarters_and_of_ranges_older_than_the_minutes_reads_the_quarters(tmp_path, monkeypatch):
    repo = DatabaseRepository(db_path=str(tmp_path / "q.db"), sample_flush_seconds=3600, retention_days=1,
                              rollup_retention_days=2, long_rollup_retention_days=10)
    await repo.initialize()
    now = time.time()
    for back in (12 * DAY, 5 * DAY, 600):  # past every retention, quarter-hour rollups only (once pruned), all three
        await repo.insert_samples([ProbeSample(target_id="a", timestamp=now - back + i, success=True, latency_ms=5.0)
                                   for i in range(120)])
    await repo.cleanup_old_samples()
    with sqlite3.connect(repo.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM rollup_1m WHERE minute < ?", ((now - 2 * DAY) // 60,)).fetchone() == (0,)
    used = []
    real = repo._quarter_query
    monkeypatch.setattr(repo, "_quarter_query", lambda *a, **k: used.append(a[2]) or real(*a, **k))
    rows, width = await repo.get_history_buckets(now - 30 * DAY, now, bucket_seconds=3600)
    assert width == 3600 and sum(r["total_count"] for r in rows) == 240  # the 5-day-old probes are still there
    rows, width = await repo.get_history_buckets(now - 6 * DAY, now, bucket_seconds=60)
    assert width == 900 and sum(r["total_count"] for r in rows) == 240  # older than the minutes: whole quarters
    rows, width = await repo.get_history_buckets(now - DAY, now, bucket_seconds=60)
    assert width == 60 and sum(r["total_count"] for r in rows) == 120  # the minutes, as before
    assert used == [3600, 900]
    await repo.close()


# --- retention ---------------------------------------------------------------------------------------------------------

async def test_retention_keeps_each_tier_for_its_own_setting(repo):
    now = time.time()
    base = now // 900 * 900 - 600  # each stretch is one whole minute: 6 slots, 1 minute, 1 quarter
    for back in (200 * DAY, 60 * DAY, 10 * DAY, 3600):
        await repo.insert_samples([ProbeSample(target_id="a", timestamp=base - back + i, success=True, latency_ms=1.0)
                                   for i in range(60)])
    await repo.flush_samples()

    def count(table):
        with sqlite3.connect(repo.db_path) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    assert [count(t) for t in ("rollup_10s", "rollup_1m", "rollup_15m")] == [4 * 6, 4, 4]
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=90, long_rollup_retention_days=400)
    assert (removed["samples"], removed["rollup_10s"], removed["rollup_1m"], removed["rollup_15m"]) == (180, 18, 1, 0)
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=30, long_rollup_retention_days=100)
    assert (removed["rollup_1m"], removed["rollup_15m"]) == (1, 1)  # the 60-day minute goes; its quarter stays
    assert [count(t) for t in ("samples", "rollup_10s", "rollup_1m", "rollup_15m")] == [60, 6, 2, 3]
    assert (await repo.get_hop_stats(now - 70 * DAY))[0]["sent"] == 180  # 60 + 60 (minutes) + 60 (quarters only)


async def test_the_defaults_keep_minutes_90_days_and_quarters_400_and_speed_tests_with_the_quarters(tmp_path):
    from pengumon.config import DatabaseConfig
    from pengumon.settings import registry

    config = DatabaseConfig()
    assert (config.retention_days, config.rollup_retention_days, config.long_rollup_retention_days) == (7, 90, 400)
    repo = DatabaseRepository(db_path=str(tmp_path / "d.db"))
    assert (repo.rollup_retention_days, repo.long_rollup_retention_days) == (90, 400)
    # quarter-hour history is at least as long as the per-minute one; unset, it follows a longer per-minute setting
    assert DatabaseConfig(rollup_retention_days=500).long_rollup_retention_days == 500
    assert DatabaseConfig(rollup_retention_days=200).long_rollup_retention_days == 400
    with pytest.raises(ValueError, match="long_rollup_retention_days must be at least"):
        DatabaseConfig(rollup_retention_days=500, long_rollup_retention_days=400)
    for bad in (29, 3651):
        with pytest.raises(ValueError):
            DatabaseConfig(long_rollup_retention_days=bad)
    setting = registry.get("database.long_rollup_retention_days")
    assert setting.label == "Keep quarter-hour history for" and setting.default() == 400
    assert (setting.constraints()["min"], setting.constraints()["max"]) == (30, 3650)
    assert "quarter-hour" in registry.get("database.rollup_retention_days").effect


# --- the migration -----------------------------------------------------------------------------------------------------

async def _v17(path, seed):
    """A database at v17 (no rollup_10s, no rollup_15m) holding what `seed(repo)` stored through the app."""
    repo = DatabaseRepository(db_path=path, sample_flush_seconds=0)
    await repo.initialize()
    await seed(repo)
    await repo.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE rollup_10s")
        db.execute("DROP TABLE rollup_15m")
        db.execute("PRAGMA user_version=17")


async def test_v18_builds_both_tables_from_what_is_stored(tmp_path):
    path = str(tmp_path / "v17.db")
    now = time.time()
    samples = _random_samples(random.Random(1), now - 2 * DAY, 3 * 3600) + _random_samples(random.Random(2), now - 600, 300)

    async def seed(repo):
        await repo.insert_samples(samples)

    await _v17(path, seed)
    with sqlite3.connect(path) as db:  # per-minute rollups older than the raw samples (past their retention)
        db.execute("INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, "
                   "lat_p90) VALUES (?, 1, 60, 50, 500.0, 1.0, 30.0, NULL, 0, 2.0, 20.0)", (int((now - 50 * DAY) // 60),))
        minutes = db.execute("SELECT * FROM rollup_1m ORDER BY minute, target").fetchall()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION >= 18

    fresh = str(tmp_path / "fresh.db")  # the same probes written by this build's flush
    repo = DatabaseRepository(db_path=fresh, sample_flush_seconds=0)
    await repo.initialize()
    await repo.insert_samples(samples)
    await repo.close()
    with sqlite3.connect(path) as db, sqlite3.connect(fresh) as other:
        assert db.execute("SELECT * FROM rollup_1m ORDER BY minute, target").fetchall() == minutes
        tens = db.execute("SELECT * FROM rollup_10s ORDER BY slot, target").fetchall()
        assert tens and tens == other.execute("SELECT * FROM rollup_10s ORDER BY slot, target").fetchall()
        quarters = db.execute("SELECT * FROM rollup_15m ORDER BY quarter, target").fetchall()
        assert quarters[0][:4] == (int((now - 50 * DAY) // 900), 1, 60, 50)  # the old minute has its quarter
        assert quarters[1:] == other.execute("SELECT * FROM rollup_15m ORDER BY quarter, target").fetchall()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION  # runs once


async def _stored_retention(path, value):
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO settings_values (key, value, updated) VALUES (?, ?, 0)",
                   ("database.rollup_retention_days", str(value)))
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('settings_revision', '3')")
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('settings_initialized', '1')")


@pytest.mark.parametrize("stored,after", [(400, None), (500, "500"), (90, "90")])
async def test_v18_takes_a_per_minute_retention_stored_at_the_old_default_to_the_new_one(tmp_path, stored, after):
    """config.example.yaml had rollup_retention_days: 400, and the one-time import stored it as the owner's: such an
    install keeps 400 days of per-minute rows unless the upgrade moves it to the new default. Any other value is the
    owner's and stays."""
    path = str(tmp_path / "v17.db")

    async def nothing(repo):
        pass

    await _v17(path, nothing)
    await _stored_retention(path, stored)
    await migrations.migrate(path)
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT value FROM settings_values WHERE key = 'database.rollup_retention_days'").fetchone()
        assert (row[0] if row else None) == after
        versions = db.execute("SELECT revision, source, summary FROM settings_versions").fetchall()
        revision = db.execute("SELECT value FROM meta WHERE key = 'settings_revision'").fetchone()[0]
    if after is None:
        assert revision == "4" and [(r, s) for r, s, _ in versions] == [(4, "upgrade")]
        assert "400 days, the old default" in versions[0][2]
    else:
        assert revision == "3" and versions == []


# --- the 10-second rollups have their own retention -------------------------------------------------------------------

async def test_the_10_second_rollups_are_kept_for_slot_retention_days_not_for_the_raw_samples(repo):
    now = time.time()
    base = now // 900 * 900 - 600
    for back in (10 * DAY, 5 * DAY, 3600):
        await repo.insert_samples([ProbeSample(target_id="a", timestamp=base - back + i, success=True, latency_ms=1.0)
                                   for i in range(60)])
    await repo.flush_samples()
    repo.slot_retention_days = 7

    def count(table):
        with sqlite3.connect(repo.db_path) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    removed = await repo.cleanup_old_samples(retention_days=3, rollup_retention_days=90, long_rollup_retention_days=400)
    assert (removed["samples"], removed["rollup_10s"]) == (120, 6)  # raw: 5 and 10 days; 10 s: only the 10-day one
    assert [count(t) for t in ("samples", "rollup_10s")] == [60, 12]
    # a raw retention longer than slot_retention_days is the floor (the 10-second rollups are never shorter)
    removed = await repo.cleanup_old_samples(retention_days=9, rollup_retention_days=90, long_rollup_retention_days=400)
    assert removed["rollup_10s"] == 0


async def test_10_second_buckets_older_than_the_raw_samples_come_from_the_10_second_rollups(repo):
    now = time.time()
    start = int(now // 600) * 600 - 5 * DAY
    await repo.insert_samples([ProbeSample(target_id="a", timestamp=start + i, success=True, latency_ms=2.0)
                               for i in range(600)])
    await repo.flush_samples()
    repo.retention_days, repo.slot_retention_days = 3, 7
    await repo.cleanup_old_samples()
    rows, width = await repo.get_history_buckets(start, start + 600, bucket_seconds=10)
    assert width == 10 and len(rows) == 60 and all(r["total_count"] == 10 for r in rows)  # not minute buckets
    rows, width = await repo.get_history_buckets(start, start + 600)  # raw samples are gone: minutes
    assert width == 60 and len(rows) == 10


def test_slot_retention_days_in_the_config_and_the_settings():
    from pengumon.config import DatabaseConfig
    from pengumon.settings import registry

    assert DatabaseConfig().slot_retention_days == 7
    assert DatabaseConfig(retention_days=3).slot_retention_days == 7      # its own: shorter raw does not shorten it
    assert DatabaseConfig(retention_days=14).slot_retention_days == 14    # unset: not shorter than the raw samples
    assert DatabaseConfig(retention_days=14, slot_retention_days=30).slot_retention_days == 30
    with pytest.raises(ValueError, match="slot_retention_days must be at least retention_days"):
        DatabaseConfig(retention_days=14, slot_retention_days=10)
    with pytest.raises(ValueError, match="rollup_retention_days must be at least slot_retention_days"):
        DatabaseConfig(slot_retention_days=40, rollup_retention_days=30)
    setting = registry.get("database.slot_retention_days")
    assert setting.label == "Keep 10-second history for" and setting.default() == 7
    assert (setting.constraints()["min"], setting.constraints()["max"]) == (1, 90)
