"""Opt-in browser smoke test of the dashboard (PENGUMON_UI_SMOKE=1).

Starts the real app on a free port with a seeded database (tests/ui/serve.py), drives it with headless Chromium
through puppeteer-core (tests/ui/smoke.mjs) and fails on any page error, console error, CSP violation or failed
request. Supported way to run it: scripts/test-ui.sh, which builds Dockerfile.ui (on the shared ui-test-runner image: Chromium, Node, the npm packages
pinned in tests/ui/runner/package-lock.json) and runs the test in a locked-down container with no network and a read-only
root filesystem. Alternative: any installed Chrome (e.g. on Windows) named by PUPPETEER_EXECUTABLE_PATH, with
puppeteer-core installed from the lockfile into the directory $PGBROWSER (optionally PENGUMON_BROWSER_ARGS for extra
Chrome flags). None of it is a dependency of the project, so the test is skipped unless node and puppeteer-core exist.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pengumon.access.manager import SETUP_KEY
from pengumon.capabilities import BAND_5G, Client, ClientsReading, SystemReading, TrafficApps, TrafficHour
from dataclasses import astuple

from pengumon.db import schema as db_schema
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import MeshCount
from pengumon.devices.samples import device_sample
from pengumon.lines.docsis import line_sample
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident
from pengumon.incidents.spikes import LatencySpike
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import PathTraceHop, ProbeSample

ROOT = Path(__file__).resolve().parents[1]
UI = Path(__file__).resolve().parent / "ui"
PGBROWSER = os.environ.get("PGBROWSER") or str(Path.home() / "pgbrowser")
BROWSER_HOST = "pengumon-test"  # a single-label name, like homeserver: the app accepts those by default; tests/ui/lib.mjs maps it to 127.0.0.1 in the browser
TARGETS = [("local_router", "Hop 1: Local Router (LAN)", "local"),
           ("isp_gateway", "Hop 2: ISP Modem / Gateway", "gateway"),
           ("cloudflare_dns", "Public: Cloudflare Anycast (1.1.1.1)", "public")]

# the `longpath` variant: the owner's real shape (seven nodes, long configured names) under made-up vendor names
LONG_TARGETS = [("lp_router", "Hop 1: Example Vendor Wireless Router (LAN)", "local"),
                ("lp_modem", "Hop 2: Example Vendor Cable Modem / Gateway", "gateway"),
                ("lp_isp1", "Hop 3: Example Provider Regional Aggregation Router", "isp"),
                ("lp_isp2", "Hop 4: Example Provider Backbone Router", "isp"),
                ("lp_isp3", "Hop 5: Example Provider Peering Edge Router", "isp"),
                ("lp_public", "Public: Example Public Resolver Anycast (203.0.113.1)", "public"),
                ("lp_public2", "Public: Second Example Resolver Anycast (203.0.113.2)", "public"),
                ("lp_public3", "Public: Third Example Resolver Anycast (203.0.113.3)", "public")]

pytestmark = pytest.mark.skipif(
    os.environ.get("PENGUMON_UI_SMOKE") != "1" or not shutil.which("node")
    or not (Path(PGBROWSER) / "node_modules" / "puppeteer-core").is_dir(),
    reason="run scripts/test-ui.sh, or set PENGUMON_UI_SMOKE=1 with node, $PGBROWSER (puppeteer-core) and PUPPETEER_EXECUTABLE_PATH")


async def seed(db_path: str, now: float, access_answered: bool = True) -> None:
    """Three hops of 1 s probes for 2 h 20 min, 15 network incidents plus three of one outage the night before and a service one (a 2 h one, a short one with a path trace, one
    explained by load), DOCSIS and router history around them. `access_answered`: the first-run question (who can open the
    dashboard, pengumon.access) was answered "anyone on my home network"; only the `firstrun` variant leaves it open."""
    repo = DatabaseRepository(db_path=db_path)
    await repo.initialize()
    if access_answered:
        await repo.set_meta(SETUP_KEY, "open")
    start = float(int(now)) - 8400.0
    samples = []
    # one probe round per second, the targets 25 ms apart as the prober sends them, straddling the half second: rounding
    # timestamps to whole seconds would split every round (the inspector once did, and showed holes as "null%")
    for i, (tid, _, _) in enumerate(TARGETS):
        t = start + 0.49 + 0.025 * i
        while t < now - 5.0:
            # the recorded gateway drop (the gateway alone, as an incident recorded before 0.30 was), and a later one the
            # internet behind the gateway shares: under today's rules only that one is the gateway's fault (a gateway that
            # loses its probes while the internet answers only ignores them: engine/classifier/rules._traffic_passes), so
            # the outage-rules preview has an incident to open or not
            lost = ((tid == "isp_gateway" and now - 3000 <= t <= now - 2910)
                    or (tid in ("isp_gateway", "cloudflare_dns") and now - 600 <= t <= now - 510)) and int(t) % 2 == 0
            samples.append(ProbeSample(target_id=tid, timestamp=t, success=not lost, latency_ms=None if lost else 8.0 + int(t) % 5,
                                       packet_loss_pct=100.0 if lost else 0.0, jitter_ms=None if lost else 0.5))
            t += 1.0
    await repo.insert_samples(samples)
    await repo.flush_samples()

    def incident(title, begin, seconds, **kw):
        return Incident(start_time=begin, end_time=begin + seconds, duration_seconds=seconds,
                        cause_code=kw.pop("cause_code", "unknown_outage"), title=title,
                        description=kw.pop("description", f"{title} (seeded)"), severity=kw.pop("severity", "warning"),
                        resolved=True, **kw)

    long_id = await repo.create_incident(incident("Long evening outage", now - 7900, 7200.0, severity="critical"))
    # detected 3 s after the gateway's probes start failing and closed 8 s after they answer again, as the classifier does:
    # the app's start reads the loss span (about 90 s) from the probes, the recorded 98 s stay the incident's duration
    # a gateway verdict as the classifier words it: the gateway's home-side address, which does not use the line
    short_id = await repo.create_incident(incident(
        "Short gateway drop", now - 2997, 98.0, affected_targets=["isp_gateway"], kind="gateway_loss",
        cause_code="gateway_unreachable",
        description="Hop 2: ISP Modem / Gateway (192.168.100.1), the cable modem (gateway) on its home-side address, is "
                    "dropping 50.0% of probes while Hop 1: Local Router (LAN) answers normally. These probes do not use "
                    "the provider's line. Possible causes: the gateway itself (overloaded, its firmware or power) or the "
                    "link from the router to it."))
    await repo.create_incident(incident("Busy line slowdown", now - 2000, 120.0, load_pct=96.0, expected=True))
    for i in range(12):
        if i == 1:  # a line verdict: loss from the provider's first router on while the gateway answers
            await repo.create_incident(incident(
                "Line or ISP Hub Packet Loss", now - 1800 + i * 60, 20.0, kind="isp_loss", cause_code="access_line_drop",
                description="ISP Hub (198.51.100.1) lost 40.0% of recent probes while the local router and the gateway "
                            "answer normally. Possible causes: cable line (RF) impairment or congestion at the provider's "
                            "first router (the CMTS node), or the hop deprioritizing ping replies."))
            continue
        await repo.create_incident(incident(f"Seeded blip {i + 1}", now - 1800 + i * 60, 20.0))
    # one provider event the night before: three drops of the line, two minutes apart (inside the grouping gap), the first at
    # 03:12 (inside the maintenance hours, the line and the provider's hops only): one outage with the label. The hours are
    # the server's local ones (the container's TZ, which scripts/test-clock.sh sets), so the time is local too: 03:12 UTC was
    # the evening before in New York and the label was missing there.
    night = datetime.now().astimezone().replace(hour=3, minute=12, second=0, microsecond=0).timestamp() - 86400
    for title, begin, seconds, kind, cause, severity in (
            ("Night event drop 1", night, 240.0, "isp_loss", "access_line_drop", "critical"),
            ("Night event drop 2", night + 360, 180.0, "isp_loss", "access_line_drop", "warning"),
            ("Night event drop 3", night + 660, 120.0, "backbone_loss", "internet_backbone_transit_loss", "critical")):
        await repo.create_incident(incident(title, begin, seconds, kind=kind, cause_code=cause, severity=severity))
    # brief latency spells with no loss (not incidents, counted): three in the last two hours (two at the provider's hops, one at the
    # router), one explained by a busy line (left out of the counts), two the night before (the provider's hops, the gateway)
    for begin, seconds, kind, stage, expected in ((now - 6500, 45.0, "upstream_latency", "isp", False), (now - 5200, 70.0, "upstream_latency", "isp", False),
                                                  (now - 3500, 60.0, "busy_latency", "load", True), (now - 900, 30.0, "local_latency", "router", False),
                                                  (night + 1500, 50.0, "upstream_latency", "isp", False), (night + 5000, 80.0, "gateway_latency", "gateway", False)):
        await repo.create_latency_spike(LatencySpike(start_time=begin, end_time=begin + seconds, kind=kind, stage=stage, expected=expected, peak_ms=150.0))
    await repo.create_incident(Incident(
        start_time=now - 4000, end_time=now - 3900, duration_seconds=100.0, cause_code="service_unavailable",
        kind="service_down", title="Service Down is not responding", description="seeded service outage",
        severity="critical", resolved=True, affected_targets=["svc_down"]))
    await repo.update_incident_path_trace(short_id, [
        PathTraceHop(hop=1, ip="192.168.1.1", hostname="router.lan", latency_ms=1.2),
        PathTraceHop(hop=2, ip="192.168.100.1", latency_ms=None, loss_pct=100.0)])

    for k in range(0, 8400, 60):
        ts = start + k
        recent = 42 if now - 3000 <= ts <= now - 2900 else 0
        await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
            timestamp=ts, downstream_power_avg=1.3, downstream_snr_avg=35.9, upstream_power_avg=44.5,
            uncorrectables_total=1000 + k, uncorrectables_recent=recent, status="healthy",
            channels=[DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=537.0, power_dbmv=2.0, snr_db=31.5,
                                    uncorrectables=1000 + k, uncorrectables_recent=recent),
                      DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=531.0, power_dbmv=1.5,
                                    snr_db=36.0 + (k // 60) % 4, uncorrectables=10),
                      DocsisChannel(channel_id=1, direction="upstream", frequency_mhz=30.6, power_dbmv=44.5)])))
    for k in range(0, 8400, 10):
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
            timestamp=start + k, wan_rx_rate_mbps=50.0 + k % 40, wan_tx_rate_mbps=5.0, cpu_usage_pct=20.0 + k % 30, mem_usage_pct=40.0,
            client_count=12 + k // 3000, status="healthy")))
    for k in range(0, 8400, 60):  # client counts (aggregates only) of the made-up client list in serve.py
        ts = start + k
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
            timestamp=ts + 1, status="healthy"), clients=ClientsReading(
                (Client(key="w", connection="wired"),) * 2 + (Client(key="f", connection="wifi", band=BAND_5G),) * (k // 60 % 4 + 1))))
        await repo.insert_mesh_counts([MeshCount("router", ts + 1, "0000a1", 2), MeshCount("router", ts + 1, "0000a2", k // 60 % 4 + 1)])
    await repo.flush_samples()
    await seed_speed_tests(repo, now)
    await seed_router_traffic(repo, now)
    await repo.close()
    assert long_id and short_id


LONG_DAYS = 200  # how far back the `longhistory` variant's made-up history reaches (the retention default is 180)


async def seed_long_history(db_path: str, now: float) -> None:
    """The `longhistory` variant: on top of seed(), 200 days of history for the 90 d and All views of every chart, with
    made-up values. Probes (quarter-hour rollups, a bad evening every 11th day, so the loss grid and the uptime cells have
    something to say), the router and its mesh nodes (quarter-hour rollups), the cable line (a reading every 6 hours), the
    router's traffic (three evening hours a day), speed tests (one every ten days) and old incidents (one every four days).
    It stops an hour before the live data of seed() begins. Written straight into the tables the app's own rollups fill:
    a raw second of probes for 200 days would be 17 million rows."""
    repo = DatabaseRepository(db_path=db_path)
    await repo.initialize()
    end = int(now) - 8400 - 3600
    first_q = (int(now) - LONG_DAYS * 86400) // 900
    last_q = end // 900
    targets = {key: tid for tid, key in repo._target_keys.items()}
    probes = []
    for q in range(first_q, last_q):
        day, slot = divmod(q * 900 - first_q * 900, 86400)
        slot //= 900
        for i, (key, _, _) in enumerate(TARGETS):
            lost = 0
            if day % 11 == 5 and 76 <= slot <= 79:  # a bad evening, hardest at the gateway
                lost = 90 if key == "isp_gateway" else 30
            n = 900
            ok = n - lost
            lat = 8.0 + slot % 5 + i
            probes.append((q, targets[key], n, ok, ok * lat, lat - 2, lat + 6.0 + (20 if lost else 0), 0.5 * ok, ok, (lat - 1) * ok, ok, (lat + 3) * ok, ok, 0))
    figures = db_schema.DEVICE_ROLLUP_FIGURES
    clients = db_schema.DEVICE_ROLLUP_CLIENT_COLUMNS
    router_cols = [f"{c}_{x}" for c in figures for x in ("sum", "n", "max")] + ["wan_link_min"] + [f"{c}_{x}" for c in clients for x in ("sum", "n")]
    routers, nodes = [], []
    for q in range(first_q, last_q):
        slot = (q * 900 // 900) % 96
        n = 90
        rx = 50.0 + (slot % 40)
        row = []
        for c in figures:
            avg = {"wan_rx_mbps": rx, "wan_tx_mbps": 5.0, "cpu_pct": 20.0 + slot % 30, "mem_pct": 40.0, "client_count": 12.0 + slot // 30, "cpu_temp_c": 60.0 + slot % 7}[c]
            row += [avg * n, n, avg * 1.4]
        row.append(2500)
        for c in clients:
            row += [{"clients_online": 14, "clients_wired": 2, "clients_wifi_2g": 0, "clients_wifi_5g": 3 + slot % 4, "clients_wifi_other": 0}[c] * n, n]
        routers.append(("router", q, *row))
        nodes += [("router", q, "0000a1", 2 * 3, 3, 2), ("router", q, "0000a2", (1 + slot % 4) * 3, 3, 4)]
    line = []
    for k in range(0, LONG_DAYS * 4):
        ts = now - 8400 - 3600 - k * 21600.0
        recent = 42 if k % 17 == 0 else 0
        line.append(astuple(line_sample("modem", DocsisSignalSnapshot(
            timestamp=ts, downstream_power_avg=1.3, downstream_snr_avg=35.9, upstream_power_avg=44.5, uncorrectables_total=1000, uncorrectables_recent=recent,
            status="healthy", channels=[DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=537.0, power_dbmv=2.0, snr_db=31.5 + k % 5, uncorrectables=1000, uncorrectables_recent=recent),
                                        DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=531.0, power_dbmv=1.5, snr_db=36.0 + k % 4, uncorrectables=10),
                                        DocsisChannel(channel_id=1, direction="upstream", frequency_mhz=30.6, power_dbmv=44.5)]))))
    async with repo._db.write() as db:
        await db.executemany("INSERT OR REPLACE INTO rollup_15m (quarter, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, p10_sum, p10_ok, p90_sum, p90_ok, dead_minutes) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", probes)
        # the minute rollups of the last 24 hours (a real install keeps them for weeks): the history reads them for a range
        # of up to 24 h in buckets of 10 minutes (the target history's 24 h)
        minutes = [(m, targets[key], 60, 60, 60 * (8.0 + (m // 90) % 5 + i), 6.0 + i, 14.0 + i, 30.0, 60, 7.0 + i, 9.0 + i)
                   for m in range((int(now) - 86400) // 60, end // 60) for i, (key, _, _) in enumerate(TARGETS)]
        await db.executemany("INSERT OR REPLACE INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", minutes)
        await db.executemany(f"INSERT OR REPLACE INTO device_rollup_15m (device_id, quarter, {', '.join(router_cols)}) VALUES (?, ?{', ?' * len(router_cols)})", routers)
        await db.executemany("INSERT OR REPLACE INTO mesh_rollup_15m (device_id, quarter, node_id, clients_sum, clients_n, clients_max) VALUES (?, ?, ?, ?, ?, ?)", nodes)
        await db.executemany(repo._LINE_INSERT, line)
        # the router's and the mesh's raw readings of the last 7 days (every 5 minutes, up to the default seed's hours): a
        # real install keeps 7 days raw, and the history reads raw there, the tiers only before it
        raw_routers, raw_nodes = [], []
        for k in range(12 * 3, 12 * 24 * 7):
            ts = int(now) - k * 300
            slot = (ts // 900) % 96
            raw_routers.append(("router", float(ts), "healthy", 50.0 + slot % 40, 5.0, 20.0 + slot % 30, 40.0, 12 + slot // 30,
                                14, 2, 0, 3 + slot % 4, 0, 60.0 + slot % 7, 2500))
            raw_nodes += [("router", float(ts), "0000a1", 2), ("router", float(ts), "0000a2", 1 + slot % 4)]
        await db.executemany("INSERT OR IGNORE INTO device_samples (device_id, ts, status, wan_rx_mbps, wan_tx_mbps, cpu_pct, mem_pct, client_count, "
                             "clients_online, clients_wired, clients_wifi_2g, clients_wifi_5g, clients_wifi_other, cpu_temp_c, wan_link_mbps) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", raw_routers)
        await db.executemany("INSERT OR IGNORE INTO mesh_samples (device_id, ts, node_id, clients) VALUES (?, ?, ?, ?)", raw_nodes)
    hours = []
    for back in range(3, LONG_DAYS):
        midnight = (int(now) // 86400 - back) * 86400
        for h in (20, 21, 22):
            hours += [TrafficHour("", midnight + h * 3600, 2 * 10**8, (4 + back % 7) * 10**9), TrafficHour("AA:AA:AA:00:00:02", midnight + h * 3600, 10**7, 10**9)]
    await repo.upsert_traffic_hours("router", hours, now - 300)
    for days in (35, 50, 70, 100, 140, 170):
        await repo.insert_speed_test({"ts": now - days * 86400.0, "duration_s": 24.0, "trigger": "scheduled", "provider": "cloudflare", "server": "EXA", "status": "ok",
                                      "reason": None, "down_mbps": 200.0 + days % 60, "up_mbps": 18.0, "idle_ms": 12.0, "loaded_down_ms": 61.0, "loaded_up_ms": 33.0,
                                      "bytes_down": 25_000_000, "bytes_up": 10_000_000})
    for k in range(1, 45):
        begin = now - 3 * 86400 - k * 3.7 * 86400
        await repo.create_incident(Incident(start_time=begin, end_time=begin + 90.0, duration_seconds=90.0, cause_code="unknown_outage", title=f"Seeded old blip {k}",
                                            description="(seeded long history)", severity="warning", resolved=True))
    # old brief latency spells (one to three, every ninth day back to 174 days: the 30 d, 90 d and All counts differ)
    for k in range(1, 20):
        for n in range(1 + k % 3):
            begin = now - 3 * 86400 - k * 9 * 86400 + n * 5000
            await repo.create_latency_spike(LatencySpike(start_time=begin, end_time=begin + 40.0, kind="upstream_latency" if n else "local_latency", stage="isp" if n else "router", peak_ms=120.0))
    await repo.close()


async def seed_router_traffic(repo: DatabaseRepository, now: float) -> None:
    """The router's traffic (schema v19) for the made-up client list in serve.py: 36 complete hours, the newest one not
    published yet (the router publishes an hour after it ends); Desk PC downloads 75 GB in one morning hour, mostly one
    app; Ann's phone a little every hour; the router's app counts for the three 24-hour windows back from the newest hour
    and the app rows of the two whole days before today. Two device events (a restart, the WAN link down and back) and uptime, CPU
    temperature and WAN link readings on the router's history."""
    newest = int(now // 3600) * 3600 - 2 * 3600
    rows = []
    for n in range(36):
        h = newest - n * 3600
        big = 75 * 10**9 if n == 5 else 0
        phone = (n % 5 + 1) * 2 * 10**8
        rows += [TrafficHour("", h, 3 * 10**8 + big // 50, big + phone + 10**9),
                 TrafficHour("AA:AA:AA:00:00:02", h, phone // 20, phone),
                 TrafficHour("AA:AA:AA:00:00:03", h, 10**6, 5 * 10**6)]
        if big:
            rows.append(TrafficHour("AA:AA:AA:00:00:01", h, big // 50, big))
    await repo.upsert_traffic_hours("router", rows, now - 300)
    # the two whole days before today (UTC, the zone the test server's days are cut on): their app rows, as Pengumon
    # keeps them just after each midnight (the 7 and 30 day ranges add them up; the device dialog lists them)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for back, desk in ((1, 75 * 10**9), (2, 0)):
        day = (today - timedelta(days=back)).isoformat()
        end = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()) - (back - 1) * 86400
        desk_apps = (("Sony PlayStation", 1_500_000_000, desk),) if desk else ()
        await repo.store_traffic_apps("router", [
            TrafficApps("AA:AA:AA:00:00:01", end, desk_apps),
            TrafficApps("AA:AA:AA:00:00:02", end, (("YouTube", 10**8, 2 * 10**9),)),
            TrafficApps("", end, desk_apps + (("YouTube", 10**8, 2 * 10**9),)),
        ], now - 300, lambda w, d=day: d)
    # the router's 24-hour counts that tile the range back from the newest hour (what Pengumon stores each hour from 0.28):
    # the two before the latest hold the phone's YouTube only. Three of them, so that the two day rows above (which end at
    # midnights) overlap the chain whatever the time of day and are never the ones a 7 or 30 day range adds up.
    for back in (1, 2):
        end = newest + 3600 - back * 86400
        await repo.store_traffic_apps("router", [
            TrafficApps("AA:AA:AA:00:00:01", end, ()),
            TrafficApps("AA:AA:AA:00:00:02", end, (("YouTube", 10**8, 2 * 10**9),)),
            TrafficApps("", end, (("YouTube", 10**8, 2 * 10**9),)),
        ], now - 300, lambda w: f"window-{w}")
    window = newest + 3600
    await repo.store_traffic_apps("router", [
        TrafficApps("AA:AA:AA:00:00:01", window, (("Sony PlayStation", 1_500_000_000, 75 * 10**9),
                                                  ("General", 1_000_000, 50_000_000))),
        TrafficApps("AA:AA:AA:00:00:02", window, (("YouTube", 10**8, 2 * 10**9), ("Netflix", 5 * 10**7, 10**9),
                                                  ("Instagram", 4 * 10**7, 8 * 10**8))),
        TrafficApps("AA:AA:AA:00:00:03", window, (("General", 10**6, 5 * 10**6),)),
        TrafficApps("", window, (("Sony PlayStation", 1_500_000_000, 75 * 10**9), ("YouTube", 10**8, 2 * 10**9),
                                 ("Netflix", 5 * 10**7, 10**9), ("Instagram", 4 * 10**7, 8 * 10**8))),
    ], now - 300, lambda w: None)
    await repo.insert_device_event("router", now - 7000, "restarted", {"boot_at": round(now - 7100), "uptime_s": 100})
    await repo.insert_device_event("router", now - 5000, "wan_link", {"old": 2500, "new": 0})
    await repo.insert_device_event("router", now - 4940, "wan_link", {"old": 0, "new": 2500})
    for k in range(0, 3600, 60):
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(timestamp=now - 3600 + k + 2,
                                                                                      status="healthy"),
                                                      system=SystemReading(uptime_s=7100 + 3600 - k,
                                                                           cpu_temp_c=60.0 + k % 7,
                                                                           wan_link_mbps=2500)))


async def seed_speed_tests(repo: DatabaseRepository, now: float) -> None:
    """Made-up speed tests for the Speed card (steps/speed.mjs, which sets the 300/20 Mbps plan they are compared with while it
    runs): the newest 3 h ago, good, with a bufferbloat grade B (49 ms added); a failed one, a good one the router measured
    (the line's rate beside the test's own), a partial one and a skipped one before it; two older good ones for the 30 day
    range. The install's schedule offset is set so that the next
    daily test is 12 h away: nothing starts by itself."""
    hour = 3600.0

    def row(ago_h: float, status: str = "ok", **kw):
        values = {"ts": now - ago_h * hour, "duration_s": 24.0, "trigger": "scheduled", "provider": "cloudflare", "server": "EXA",
                  "status": status, "reason": None, "down_mbps": 236.4, "up_mbps": 18.7, "idle_ms": 12.0, "loaded_down_ms": 61.0,
                  "loaded_up_ms": 33.0, "bytes_down": 25_000_000, "bytes_up": 10_000_000}
        values.update(kw)
        return values

    gone = {"server": None, "down_mbps": None, "up_mbps": None, "idle_ms": None, "loaded_down_ms": None, "loaded_up_ms": None,
            "bytes_down": 0, "bytes_up": 0}
    for test in (
        row(3),
        row(27, "failed", reason="download: the provider refused the test (rate limit); try another provider", **gone),
        # an 8 s test the router measured: the line carried 305.0 / 20.1 Mbps while the test got 301.2 / 19.9
        row(51, "ok", down_mbps=301.2, up_mbps=19.9, idle_ms=11.0, loaded_down_ms=14.0, loaded_up_ms=13.0,
            bytes_down=301_000_000, bytes_up=20_000_000, wan_down_mbps=305.0, wan_up_mbps=20.1),
        row(75, "partial", reason="upload: timed out", up_mbps=None, loaded_up_ms=None, bytes_up=4_000_000),
        row(99, "skipped", reason="line busy (82% load, over 20% after 3 retries)", **gone),
        row(240, "ok", down_mbps=190.5, up_mbps=15.2, trigger="manual"),
        row(480, "ok", down_mbps=250.0, up_mbps=18.0),
    ):
        await repo.insert_speed_test(test)
    await repo.set_meta("speedtest_offset_s", str(int(now + 12 * hour) % 86400))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DEVICES = {
    "longpath": "",
    "full": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
            '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    # `full` with 200 days of made-up history behind it (seed_long_history): the steps that draw every chart at every range
    "longhistory": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                   '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    "bare": "",
    # two routers: one card each, each with its own history and setup
    "tworouters": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                  '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n'
                  '  - {id: router2, driver: asuswrt, url: "http://192.168.97.1", model: "Second_AP"}\n',
    # an access token: smoke.mjs signs in through the sign-in page; only the sign-out steps run (steps/sign-out.mjs)
    "token": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
             '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    # an access token with viewing without it on (PENGUMON_VIEW_WITHOUT_TOKEN): the page opens view only, then signs in
    # from the header (steps/sign-out.mjs)
    "viewonly": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    # a fresh install: the first-run question is open (who can open the dashboard); steps/access-setup.mjs answers it
    "firstrun": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    # a line device of a technology the page has no adapter for (serve.py relabels the modem driver)
    "unknownline": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                   '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
}


# `full` is most of the time of the suite (about 390 s of steps), so it runs as four slices of the step files (three
# came within seconds of the 240 s limit a run then had on a 4-vCPU CI runner once 0.27 had added its steps), each with
# its own app and browser (smoke.mjs, SHARD); the other variants take about 20 s each. With pytest-xdist
# (scripts/test-ui.sh) they all run side by side.
FULL_SHARDS = 4
# One run (a slice of `full` is about 100 s of steps on an idle machine, twice that on a 4-vCPU CI runner busy with the
# other runs): scripts/test-ui.sh sets the hang guard above it.
RUN_TIMEOUT_S = 420
# The order matters to pytest-xdist (scripts/test-ui.sh, 4 workers, `--dist worksteal`): each worker starts on a block of
# four runs and idle workers take the unstarted ones of the others, but never the last one. A slice of `full` every fourth
# run keeps two of them from queuing on one worker.
RUNS = [pytest.param(*r, id="-".join([r[0]] + ([r[1].replace("/", "of")] if r[1] != "1/1" else []) + ([r[2]] if len(r) > 2 else []))) for r in (
    # one pass in Firefox (core and Settings steps only, smoke.mjs FIREFOX_FILES), in two slices like `full`; they run beside the
    # Chromium ones
    ("full", "4/4"), ("full", "2/2", "firefox"), ("longpath", "1/1"),
    ("full", "1/4"), ("token", "1/1"), ("tworouters", "1/1"),
    ("full", "2/4"), ("viewonly", "1/1"), ("bare", "1/1"),
    ("full", "3/4"), ("full", "1/2", "firefox"), ("unknownline", "1/1"), ("firstrun", "1/1"),
    # every chart at every range, in every theme, on a desktop and a phone (the steps flagged `longhistory`), in three slices
    ("longhistory", "1/3"), ("longhistory", "2/3"), ("longhistory", "3/3"))]
assert {r.values[0] for r in RUNS} == set(DEVICES) and sum(r.values[0] == "full" and len(r.values) == 2 for r in RUNS) == FULL_SHARDS


@pytest.mark.parametrize("variant, shard, browser", [pytest.param(*r.values, id=r.id) if len(r.values) == 3 else pytest.param(*r.values, "chromium", id=r.id) for r in RUNS])
def test_dashboard_in_headless_browser(tmp_path, variant, shard, browser):
    """`full`: modem, router and a two-node mesh (every panel and the Local network modal). `bare`: no devices at all
    (no device cards, the rest of the live page works). `tworouters`: a card per router. `unknownline`: the line card's
    common part only. `token`: `full` behind an access token (sign in, sign out, sign out everywhere). `viewonly`: the
    token with PENGUMON_VIEW_WITHOUT_TOKEN on (view only, then sign in to edit). `firstrun`: the first-run question is
    still open (who can open the dashboard); every other variant has it answered."""
    db = tmp_path / "smoke.db"
    seeded_at = time.time()
    asyncio.run(seed(str(db), seeded_at, access_answered=variant != "firstrun"))
    if variant == "longhistory":
        asyncio.run(seed_long_history(str(db), seeded_at))
    config = tmp_path / "config.yaml"
    targets = "".join(
        f'  - {{id: "{t}", name: "{n}", category: {c}, address: "127.0.0.1", enabled: true}}\n'
        for t, n, c in (LONG_TARGETS if variant == "longpath" else TARGETS))
    config_text = (
        "prober: {interval_seconds: 1.0}\n"
        f"targets:\n{targets}"  # no dns_probes: the two default lookups
        f'database: {{db_path: "{db.as_posix()}"}}\n'
        "privacy: {client_details: full}\n"  # the Clients modal then shows names and addresses
        "line: {isp_name: Example ISP}\n"  # the dossier title and file name the smoke script checks
        "speedtest: {enabled: true, monthly_cap_mb: 2000}\n"  # the card's seeded results (nothing starts: the next test is 12 h away, the container has no network)
        "service_checks:\n  interval_seconds: 2\n  failures_for_incident: 1\n  services:\n"
        f'    - {{id: svc_up, name: "Service Up", url: "http://127.0.0.1:@PORT@/api/health", description: "the app itself",'
        ' open_url: "http://services.example.test:9000/app"}\n'
        '    - {id: svc_down, name: "Service Down", url: "http://127.0.0.1:1/health"}\n'
        + DEVICES[variant])

    # Its own data directory. Without PENGUMON_DATA the app uses ~/.local/state/pengumon, which every run in the container
    # (pytest-xdist) shares, and with it sessions.json: the `token` variant's "Sign out everywhere" then refused the cookie
    # that `viewonly` had just signed in with, closed its live socket (1008) and sent its page back to the sign-in page.
    env = {**os.environ, "PENGUMON_BROWSER": browser, "PYTHONPATH": str(ROOT / "src"), "PENGUMON_CONFIG": str(config), "PENGUMON_DATA": str(tmp_path / "data"), "PGBROWSER": PGBROWSER}
    # configured devices (a password each), so the features the history views need are available
    env.update({"PENGUMON_DEVICE_MODEM_PASSWORD": "smoke-pw", "PENGUMON_DEVICE_ROUTER_PASSWORD": "smoke-pw",
                "PENGUMON_DEVICE_ROUTER2_PASSWORD": "smoke-pw"})
    if variant == "unknownline":
        env["PENGUMON_SMOKE_LINE_TECHNOLOGY"] = "fiber"
    if variant == "full":  # a value pinned by the environment: the Settings page shows it as "From environment" (steps/settings.mjs)
        env["PENGUMON_LOG_LEVEL"] = "warning"
    if variant == "longpath":  # as on Windows: round trips in whole milliseconds (serve.py's stand-in backend)
        env["PENGUMON_SMOKE_PROBE_RESOLUTION_MS"] = "1.0"
    for name in ("PENGUMON_ACCESS_TOKEN", "PENGUMON_ALLOWED_HOSTS", "PENGUMON_ALLOWED_ORIGINS", "PENGUMON_VIEW_WITHOUT_TOKEN"):
        env.pop(name, None)
    headers = {}
    if variant == "viewonly":
        env["PENGUMON_VIEW_WITHOUT_TOKEN"] = "1"
    if variant in ("token", "viewonly"):  # made up for the test; the page signs in with it, the checks below send it as Bearer
        env["PENGUMON_ACCESS_TOKEN"] = env["PENGUMON_SMOKE_TOKEN"] = "smoke-token-not-a-secret"
        headers = {"Authorization": "Bearer smoke-token-not-a-secret"}
    # cwd: keeps anything written relative to it out of the repository (the data directory is PENGUMON_DATA above)
    server = None
    try:
        # free_port() closes its socket before the app binds, and several runs start at once (pytest-xdist): another one
        # can take the port in between. Then the app exits with "address already in use" and this starts again.
        for attempt in range(5):
            port = free_port()
            config.write_text(config_text.replace("@PORT@", str(port)), encoding="utf-8")
            server = subprocess.Popen([sys.executable, str(UI / "serve.py"), str(port)], cwd=tmp_path, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            # The browser opens the app by a name that is not local (lib.mjs maps it to 127.0.0.1), as people do over the LAN:
            # a page on 127.0.0.1 or localhost is a secure context, where the Clipboard API, notifications and downloads
            # work, so it hides what fails over http://homeserver:8000. The checks of this file talk to 127.0.0.1 directly.
            base = f"http://{BROWSER_HOST}:{port}"
            local = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if server.poll() is not None:
                    break
                try:
                    if urllib.request.urlopen(f"{local}/api/health", timeout=1).status == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                server.terminate()
                pytest.fail(f"the app did not start:\n{server.communicate(timeout=10)[0]}")
            if server.poll() is None:
                time.sleep(0.5)  # an answer may have come from another run's app that took this port first: ours exits
            if server.poll() is None:
                break
            output = server.communicate(timeout=10)[0]
            if "address already in use" not in output.lower() or attempt == 4:
                pytest.fail(f"the app did not start:\n{output}")
        for _ in range(100):  # the first service check ran before the port was open; the page loads the settled state
            services = urllib.request.Request(f"{local}/api/services", headers=headers)
            if '"status":"up"' in urllib.request.urlopen(services, timeout=5).read().decode():
                break
            time.sleep(0.2)
        else:
            pytest.fail("the service check of the app itself never came up")
        out_dir = tmp_path / "out"  # screenshots and the downloaded dossier; tmp_path is the only writable place in the container
        out_dir.mkdir()
        try:
            run = subprocess.run(["node", str(UI / "smoke.mjs"), base, str(out_dir), variant, shard], env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:  # say where the run was (the steps' progress lines on stderr)
            progress = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            pytest.fail(f"the browser run took more than {RUN_TIMEOUT_S} s; its progress:\n{progress}")
        print(run.stdout)  # shown with -s: the steps passed and the sparkline cost
        assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
