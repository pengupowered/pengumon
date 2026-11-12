"""Seeds a large probe database and times the dashboard's slow reads and the sample flush (docs/storage.md, "Speed").

Runs in the test image (scripts/test.sh --command ...) or anywhere the app's requirements are installed, against
whichever build is first on PYTHONPATH, so the same seeded file can be timed with an older build and this one:

    python scripts/bench-storage.py seed  <db> [--days 7] [--targets 16] [--rollup-days 37]
    python scripts/bench-storage.py run   <db> [--repeat 5] [--answers <file.json>]
    python scripts/bench-storage.py flush <db> [--batches 30]

`seed` creates the schema with the build on PYTHONPATH, then writes `--days` of raw samples (each target probed about
once a second, a little loss, three-minute outages every six hours on the far hops), their per-minute rollups (the
build's own UPSERT_ROLLUP_1M_FROM_SAMPLES) and synthetic per-minute rollups back to `--rollup-days`, plus an incident
every few hours. Every value is made up (192.0.2.x addresses, "hopN" ids). `run` opens the file, flushes nothing, and
times each read `--repeat` times (the clock is pinned to the end of the seeded data, so two builds read the same
windows); `--answers` writes the answers as JSON for comparing builds. `flush` times the flush of 10-second batches (16
targets x 10 probes) appended after the seeded data. Each prints one line per measurement: name, median and best ms.
"""

import argparse
import asyncio
import json
import os
import statistics
import sqlite3
import sys
import time
from typing import Any, Dict

DAY = 86400


def _targets(n):
    from pengumon.models.probes import TargetConfig
    cats = ["local", "gateway"] + ["isp"] * max(n - 4, 0) + ["public", "public"]
    return [TargetConfig(id=f"hop{i}", name=f"Hop {i}", address=f"192.0.2.{i + 1}", category=cats[i]) for i in range(n)]


def seed(path: str, days: int, n_targets: int, rollup_days: int) -> None:
    from pengumon.db.migrations import migrate
    from pengumon.db.schema import UPSERT_ROLLUP_1M_FROM_SAMPLES

    if os.path.exists(path):
        sys.exit(f"{path} exists")
    print("schema:", asyncio.run(migrate(path)))
    end = float(int(time.time()) // 60 * 60)
    start = end - days * DAY
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.executemany("INSERT INTO targets (id, key) VALUES (?, ?)", [(i + 1, f"hop{i}") for i in range(n_targets)])
    t0 = time.monotonic()
    # One probe per target per second, at a per-target fraction of the second; ~0.3 % loss (a fixed hash, so the
    # probe's ok and its latency agree); on the hops past the gateway a three-minute outage every six hours; latency
    # grows along the path, with noise and jitter.
    for tid in range(1, n_targets + 1):
        con.execute(f"""
            WITH RECURSIVE k(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM k WHERE i < {days * DAY - 1})
            INSERT INTO samples (ts, target, ok, latency_ms, jitter_ms, error)
            SELECT ts, {tid}, ok, CASE WHEN ok THEN {tid} * 1.5 + (abs(random()) % 4000) / 1000.0 END,
                   CASE WHEN ok THEN (abs(random()) % 2000) / 1000.0 END, CASE WHEN ok THEN NULL ELSE 'timeout' END
            FROM (SELECT {start} + i + {tid} / 64.0 AS ts,
                         CASE WHEN {tid} > 2 AND i % 21600 < 180 THEN 0
                              WHEN (i * 2654435761 + {tid} * 40503) % 1000 < 3 THEN 0 ELSE 1 END AS ok FROM k)
        """)
        con.commit()
    print(f"samples: {con.execute('SELECT COUNT(*) FROM samples').fetchone()[0]:,} in {time.monotonic() - t0:.0f} s")
    t0 = time.monotonic()
    for t in range(int(start), int(end) + 60, DAY):
        con.execute(UPSERT_ROLLUP_1M_FROM_SAMPLES, (t, t + DAY))
    con.commit()
    older = rollup_days - days
    if older > 0:  # per-minute rollups from before the raw window, like an install running for `rollup_days`
        first = int((start - older * DAY) // 60)
        con.execute(f"""
            WITH RECURSIVE k(m) AS (SELECT {first} UNION ALL SELECT m + 1 FROM k WHERE m < {int(start // 60) - 1}),
                 t(id) AS (SELECT id FROM targets)
            INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90)
            SELECT m, id, 60, ok, (id * 1.5 + 2) * ok, id * 1.5, id * 1.5 + 4, ok * 1.0, ok, id * 1.5 + 0.4,
                   id * 1.5 + 3.6
            FROM (SELECT m, id, CASE WHEN id > 2 AND m % 360 < 3 THEN 0
                                     WHEN (m * 2654435761 + id * 40503) % 1000 < 3 THEN 59 ELSE 60 END AS ok FROM k, t)
        """)
        con.commit()
    print(f"rollup_1m: {con.execute('SELECT COUNT(*) FROM rollup_1m').fetchone()[0]:,} in {time.monotonic() - t0:.0f} s")
    rows = []
    for k in range(int((start - max(older, 0) * DAY) // 21600), int(end // 21600)):
        at = k * 21600.0
        rows.append((at, at + 180, 180.0, "access_line_drop", "Packet loss", "d", "warning", '["hop3"]', 1, "isp_loss"))
    con.executemany("INSERT INTO incidents (start_time, end_time, duration_seconds, cause_code, title, description, "
                    "severity, affected_targets, resolved, kind) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bench_end', ?)", (repr(end),))
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"incidents: {len(rows)}; file {os.path.getsize(path) / 1e6:.0f} MB; data ends at {end:.0f}")


def _bench_end(path: str) -> float:
    con = sqlite3.connect(path)
    try:
        return float(con.execute("SELECT value FROM meta WHERE key = 'bench_end'").fetchone()[0])
    finally:
        con.close()


async def _timed(name: str, repeat: int, call, out: Dict[str, Any]):
    times = []
    answer = None
    for _ in range(repeat):
        t = time.perf_counter()
        answer = await call()
        times.append((time.perf_counter() - t) * 1000)
    print(f"{name:34s} median {statistics.median(times):8.1f} ms   best {min(times):8.1f} ms", flush=True)
    out[name] = answer


async def run(path: str, repeat: int, answers: str) -> None:
    from pengumon.config import AppConfig
    from pengumon.db.repository import DatabaseRepository
    from pengumon.reports.loss_grid import build_loss_grid
    from pengumon.reports.reliability import build_report
    from pengumon.reports.uptime import build_uptime

    end = _bench_end(path) + 30
    time.time = lambda: end  # every build reads the same windows (the repository's history clips by the clock)
    n_targets = sqlite3.connect(path).execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    config = AppConfig(targets=_targets(n_targets))
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    out: Dict[str, Any] = {}
    try:
        for label, span, bucket in (("history 1h bucket 10", 3600, 10), ("history 6h bucket 10 (band)", 6 * 3600, 10),
                                    ("history 12h bucket 10", 12 * 3600, 10), ("history 24h bucket 60", DAY, 60),
                                    ("history 7d bucket 60", 7 * DAY, 60), ("history 7d bucket 3600", 7 * DAY, 3600),
                                    ("history 30d bucket 3600", 30 * DAY, 3600)):
            await _timed(label, repeat, lambda: repo.get_history_buckets(end - span, end, None, bucket), out)
        for days in (7, 30, 90, 365):
            await _timed(f"report days={days}", repeat, lambda: build_report(repo, config, days, now=end), out)
        await _timed("loss-grid days=7", repeat, lambda: build_loss_grid(repo, 7, now=end), out)
        for window in ("24h", "7d", "30d"):
            await _timed(f"uptime {window}", repeat, lambda: build_uptime(repo, config, window, now=end), out)
        await _timed("hop stats 30d", repeat, lambda: repo.get_hop_stats(end - 30 * DAY, end), out)
    finally:
        await repo.close()
    if answers:
        with open(answers, "w", encoding="utf-8") as f:
            json.dump(out, f, sort_keys=True, default=list)


async def flush(path: str, batches: int) -> None:
    from pengumon.db.repository import DatabaseRepository
    from pengumon.models.probes import ProbeSample

    end = _bench_end(path)
    n_targets = sqlite3.connect(path).execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    repo = DatabaseRepository(db_path=path, sample_flush_seconds=3600)
    await repo.initialize()
    times = []
    try:
        for b in range(batches):  # 10 s of probes of every target, appended after the data, then flushed
            base = end + b * 10
            await repo.insert_samples([
                ProbeSample(target_id=f"hop{t}", timestamp=base + s + t / 64.0, success=True, latency_ms=2.0 + t,
                            jitter_ms=0.5) for t in range(n_targets) for s in range(10)])
            started = time.perf_counter()
            await repo.flush_samples()
            times.append((time.perf_counter() - started) * 1000)
    finally:
        await repo.close()
    print(f"flush of {n_targets * 10} samples ({batches} batches)  median {statistics.median(times):6.2f} ms   "
          f"best {min(times):6.2f} ms   worst {max(times):6.2f} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("db")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--targets", type=int, default=16)
    s.add_argument("--rollup-days", type=int, default=37)
    r = sub.add_parser("run")
    r.add_argument("db")
    r.add_argument("--repeat", type=int, default=5)
    r.add_argument("--answers", default="")
    f = sub.add_parser("flush")
    f.add_argument("db")
    f.add_argument("--batches", type=int, default=30)
    args = parser.parse_args()
    if args.cmd == "seed":
        seed(args.db, args.days, args.targets, args.rollup_days)
    elif args.cmd == "run":
        asyncio.run(run(args.db, args.repeat, args.answers))
    else:
        asyncio.run(flush(args.db, args.batches))


if __name__ == "__main__":
    main()
