"""A seeded week for the AI pack's facts (tests/test_ai_pack_facts.py): the per-minute rollups of every hop for 7 days
ending a few minutes ago, and incidents with 1-second samples around them (the way the app stores them), written
straight into the database file so a 15-hop, 300-incident week takes seconds. Example values only: 192.0.2.x,
198.51.100.x, 192.168.88.x. Times are relative to the real clock, which the repository's retention reads."""

import random
import sqlite3
import time
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from pengumon.db.schema import UPSERT_ROLLUP_10S_FROM_SAMPLES, UPSERT_ROLLUP_15M_FROM_1M
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample

DAY = 86400.0
NORMAL_MS = {"local": 1.5, "gateway": 3.0, "isp": 10.0, "public": 18.0, "dns": 20.0}


def derive_rollups(con, first_minute: int, end_minute: int, first_ts: Optional[float] = None,
                   end_ts: Optional[float] = None) -> None:
    """What the app's flush derives from what the seed writes straight into the file: the quarter-hour rollups of the
    quarters of the minutes [first_minute, end_minute) from rollup_1m and, given [first_ts, end_ts), the 10-second
    rollups of those seconds from the raw samples."""
    con.execute(UPSERT_ROLLUP_15M_FROM_1M, (first_minute // 15 * 15, -(-end_minute // 15) * 15))
    if first_ts is not None:
        con.execute(UPSERT_ROLLUP_10S_FROM_SAMPLES, (int(first_ts // 10) * 10, -(-int(end_ts) // 10) * 10))


def week_end() -> float:
    """The end of the seeded week: the last full minute, five minutes ago."""
    return float(int(time.time() // 60) * 60 - 300)


def make_config(isp=2, public=1, dns=1):
    """A config with the standard path: Router, Line, `isp` provider hops, `public` internet servers, `dns` lookups."""
    t = [SimpleNamespace(id="router", name="Example-Mesh main", category="local", address="192.168.88.1", enabled=True),
         SimpleNamespace(id="line", name="Example gateway", category="gateway", address="192.168.100.1", enabled=True)]
    t += [SimpleNamespace(id=f"isp{i}", name=f"po-{i}.edge.example-isp.net", category="isp",
                          address=f"198.51.100.{i}", enabled=True) for i in range(1, isp + 1)]
    t += [SimpleNamespace(id=f"net{i}", name=f"Public resolver {i}", category="public", address=f"192.0.2.{i}",
                          enabled=True) for i in range(1, public + 1)]
    # the DNS probes as the app has them: no category and no address of their own, a nameserver
    d = [DnsProbeConfig(id=f"dns{i}", name=f"Lookup {i}", domain="example.com", nameserver="192.0.2.53")
         for i in range(1, dns + 1)]
    return SimpleNamespace(targets=t, dns_probes=d,
                           classifier=SimpleNamespace(busy_load_pct=60.0, degraded_loss_pct=15.0))


def incident_row(kind: str, start: float, seconds: float, expected: bool = False, severity: str = "warning",
                 cause_code: str = "access_line_drop", affected: Tuple[str, ...] = ("line",)) -> Incident:
    return Incident(start_time=start, cause_code=cause_code, title="Packet loss", description="d", severity=severity,
                    affected_targets=list(affected), kind=kind, expected=expected, load_pct=80.0 if expected else None)


def lost_targets(config, where: str) -> List[str]:
    """The targets that lose probes in an incident that starts at `where`: that hop and every later one ("carried")
    or that hop only ("one hop"), by target id prefix."""
    ids = [t.id for t in config.targets]
    return ids[ids.index(where):]


async def seed(repo, config, end: float, *, slow: Tuple[Tuple[str, float, float, float], ...] = (),
               gaps: Tuple[Tuple[float, float], ...] = (), seed_value: int = 1) -> Dict[str, int]:
    """Rollups for the 7 days ending at `end`, a healthy week with small noise: each (target, from, to, factor) in
    `slow` makes that hop `factor` times slower in that time; each (from, to) in `gaps` has no probe at all. Returns {target id: db id}."""
    rng = random.Random(seed_value)
    targets = [t for t in list(config.targets) + list(config.dns_probes)]
    await repo.insert_samples([ProbeSample(target_id=t.id, timestamp=end - 7 * DAY - 120, success=True, latency_ms=1.0,
                                           packet_loss_pct=0.0) for t in targets])
    await repo.flush_samples()
    con = sqlite3.connect(repo.db_path)
    ids = dict(con.execute("SELECT key, id FROM targets"))
    for table in ("samples", "rollup_1m", "rollup_10s", "rollup_15m"):
        con.execute(f"DELETE FROM {table}")
    first, last = int((end - 7 * DAY) // 60), int(end // 60)
    rows = []
    for m in range(first, last):
        if any(a <= m * 60 < b for a, b in gaps):
            continue
        for t in targets:
            base = NORMAL_MS[getattr(t, "category", "dns")]
            avg = base * (1 + rng.random() * 0.2)
            for target, a, b, factor in slow:
                if target == t.id and a <= m * 60 < b:
                    avg = base * factor
            rows.append((m, ids[t.id], 60, 60, avg * 60, avg, avg, 0.3 * 60, 60, avg * 0.9, avg * 1.3))
    con.executemany("INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, "
                    "lat_p10, lat_p90) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    derive_rollups(con, first, last)
    con.commit()
    con.close()
    return ids


async def add_incident(repo, config, ids: Dict[str, int], start: float, seconds: int, where: str, *,
                       carried: bool = True, kind: str = "gateway_loss", raw: bool = True, expected: bool = False,
                       severity: str = "warning", cause_code: str = "access_line_drop",
                       silent: Tuple[str, ...] = ()) -> int:
    """An incident at `start` for `seconds`: from the hop `where` on (`carried`) or at that hop only, every probe lost.
    With `raw` the 1-second samples around it are stored (and the rollups of its minutes show the loss), as in the
    app; without, only the rollups (the samples are past retention). `silent`: hops that answer nothing all the time
    (they ignore ping). Returns its id."""
    lost = set(lost_targets(config, where)) if carried else {where}
    targets = [t.id for t in list(config.targets) + list(config.dns_probes)]
    con = sqlite3.connect(repo.db_path)
    s0, s1 = int(start) - 12, int(start) + seconds + 4
    if raw:
        con.executemany("INSERT OR REPLACE INTO samples (ts, target, ok, latency_ms, jitter_ms, error) VALUES (?,?,?,?,?,?)",
                        [(float(ts), ids[t], 0 if lost_here else 1, None if lost_here else 5.0, None, None)
                         for ts in range(s0, s1 + 1) for t in targets
                         for lost_here in [t in silent or (t in lost and start <= ts < start + seconds)]])
    minutes = range(int(start // 60), int((start + seconds) // 60) + 1)
    for m in minutes:  # the rollups of the minutes it touched
        n_lost = sum(1 for ts in range(int(start), int(start + seconds)) if ts // 60 == m)
        for t in lost:
            con.execute("UPDATE rollup_1m SET ok = n - ?, lat_sum = lat_sum * (n - ?) / n WHERE minute = ? AND target = ?",
                        (n_lost, n_lost, m, ids[t]))
    derive_rollups(con, minutes[0], minutes[-1] + 1, *((s0, s1 + 1) if raw else ()))
    con.commit()
    con.close()
    iid = await repo.create_incident(incident_row(kind, start, seconds, expected, severity, cause_code, tuple(sorted(lost))))
    await repo.resolve_incident(iid, start + seconds + 3)
    return iid
