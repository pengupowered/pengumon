"""The window rule of 0.28 (windows.py), asserted the same way for every view: a range is a rolling window that ends
now (to the resolution of its data) and spans exactly the range; a longer range contains the shorter one, so its totals
are never smaller; the apps and "not broken down" add up to the total; the figures of a range are those of its window,
checked against an independent count of what was stored. Made-up data (02:00:00:00:00:xx, example names), in America/New_York
across a local midnight, the day the clocks went back (1 November: 25 hours) and the hour in progress."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pengumon.capabilities import Client, ClientsReading, TrafficApps, TrafficHour
from pengumon.devices import traffic
from pengumon.models.probes import ProbeSample
from pengumon.reports.loss_grid import build_loss_grid
from pengumon.reports.queries import daily_availability
from pengumon.reports.reliability import build_report
from pengumon.reports.uptime import build_uptime

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 12, 13, 30, tzinfo=NY).timestamp()  # a Thursday, in the middle of the afternoon
HOUR = 3600
GAME, LAPTOP, PS5 = "02:00:00:00:00:03", "02:00:00:00:00:01", "02:00:00:00:00:09"
FLOOR = traffic.hour_floor(NOW)
RANGES = {"24h": 24, "7d": 168, "30d": 720}


def inventory():
    return ClientsReading((
        Client(key=GAME, label_parts=("Den console",), vendor="Example Games", kind="game", mac=GAME),
        Client(key=LAPTOP, label_parts=("laptop",), vendor="Example PC", kind="computer", mac=LAPTOP),
        Client(key=PS5, label_parts=("living room",), vendor="Example Games", kind="game", mac=PS5),
    ))


async def seed_traffic(repo):
    """35 days of hours up to the last one the router published (the one before the hour in progress, 12:00-13:00: so the
    window ends at FLOOR, 13:00). The game console and the laptop have traffic every hour; the PS5
    only in one hour 4 days ago, on a day with no app window. App windows: one per local day (ending at midnight) for
    the console, the laptop and everyone together, none for the 3 days 10-12 days back, and the latest ones ending at
    the window's end. Every app list is smaller than its client's traffic, so apps plus the rest equal the total."""
    rows = []
    for h in range(FLOOR - 35 * 24 * HOUR, FLOOR, HOUR):
        rows += [TrafficHour("", h, 3 * 10**6, 4 * 10**8), TrafficHour(GAME, h, 10**6, 2 * 10**8),
                 TrafficHour(LAPTOP, h, 10**6, 10**8)]
    ps5_hour = traffic.hour_floor(datetime(2026, 11, 8, 20, tzinfo=NY).timestamp())
    rows += [TrafficHour(PS5, ps5_hour, 5 * 10**6, 10**10)]
    rows = [r if not (r.client == "" and r.hour == ps5_hour) else TrafficHour("", r.hour, r.up + 5 * 10**6, r.down + 10**10)
            for r in rows]
    await repo.upsert_traffic_hours("router", rows, NOW)
    today = datetime.fromtimestamp(NOW, NY).date()
    for back in range(1, 34):
        if 10 <= back <= 12:
            continue
        day = today - timedelta(days=back)
        end = traffic.midnight(day + timedelta(days=1), NY)
        await repo.store_traffic_apps("router", [
            TrafficApps(GAME, end, (("Sony PlayStation", 10**5, 3 * 10**9),)),
            TrafficApps(LAPTOP, end, (("YouTube", 10**5, 10**9), ("Netflix", 10**5, 10**9))),
            TrafficApps("", end, (("Sony PlayStation", 10**5, 3 * 10**9), ("YouTube", 10**5, 10**9),
                                  ("Netflix", 10**5, 10**9))),
        ], NOW, lambda w, d=day: d.isoformat())
    await repo.store_traffic_apps("router", [
        TrafficApps(GAME, FLOOR, (("Sony PlayStation", 10**5, 2 * 10**9),)),
        TrafficApps(LAPTOP, FLOOR, (("YouTube", 10**5, 10**9),)),
        TrafficApps("", FLOOR, (("Sony PlayStation", 10**5, 2 * 10**9), ("YouTube", 10**5, 10**9))),
    ], NOW, lambda w: None)


def burst_times():
    """One minute of probes every 3 hours for 31 days, minute-aligned, the newest in the hour in progress."""
    newest = (int(NOW // 3600)) * 3600 + 5 * 60
    return [newest - k * 3 * HOUR for k in range(31 * 8)]


def lost_seconds(k_time):
    return 60 if (k_time // (3 * HOUR)) % 7 == 0 else 0  # every seventh burst is lost for the whole minute


async def seed_probes(repo):
    samples = []
    for t in burst_times():
        lost = lost_seconds(t)
        for i in range(60):
            ok = lost == 0
            samples.append(ProbeSample(target_id="a", timestamp=float(t + i), success=ok,
                                       latency_ms=5.0 if ok else None, packet_loss_pct=0.0 if ok else 100.0,
                                       jitter_ms=None, error=None if ok else "x"))
    await repo.insert_samples(samples)


def sent_ok_in(start, end):
    """(sent, ok) of the seeded probes with start <= timestamp < end, counted from the definition above."""
    sent = ok = 0
    for t in burst_times():
        inside = sum(1 for i in range(60) if start <= t + i < end)
        sent += inside
        ok += inside if lost_seconds(t) == 0 else 0
    return sent, ok


# --- traffic ---------------------------------------------------------------------------------------------------------

async def traffic_answers(repo):
    namer = traffic.Namer("full", inventory())
    out = {}
    for name, hours in RANGES.items():
        out[name] = await traffic.summary(repo, "router", hours, namer, now=NOW, tz=NY, with_hours=True)
    return out


async def test_traffic_ranges_roll_back_from_the_newest_hour_and_each_contains_the_shorter(repo):
    await seed_traffic(repo)
    answers = await traffic_answers(repo)
    for name, hours in RANGES.items():
        body = answers[name]
        assert body["end"] - body["start"] == hours * HOUR and body["hours"] == hours
        assert 0 <= NOW - body["end"] < 2 * HOUR  # to the hour: the newest the router published, within an hour of now
        assert body["end"] % HOUR == 0 and body["end"] == FLOOR
    for shorter, longer in (("24h", "7d"), ("7d", "30d")):
        a, b = answers[shorter], answers[longer]
        assert b["totals"]["down"] >= a["totals"]["down"] and b["totals"]["up"] >= a["totals"]["up"]
        assert sum(x["down"] for x in b["apps"]) >= sum(x["down"] for x in a["apps"])
        mine = {c["mac"]: c for c in a["clients"]}
        for c in b["clients"]:
            if c["mac"] in mine:
                assert c["down"] >= mine[c["mac"]]["down"] and c["up"] >= mine[c["mac"]]["up"]
        assert set(mine) <= {c["mac"] for c in b["clients"]}


async def test_the_apps_and_what_is_not_broken_down_add_up_to_the_total_for_everyone(repo):
    await seed_traffic(repo)
    for name, body in (await traffic_answers(repo)).items():
        for who, apps, rest, total in [("network", body["apps"], body["apps_unattributed"], body["totals"])] + [
                (c["label"], c["apps"], c["apps_unattributed"], {"up": c["up"], "down": c["down"]})
                for c in body["clients"]]:
            for way in ("up", "down"):
                assert sum(a[way] for a in apps) + rest[way] == total[way], (name, who, way)
        # the windows used lie inside the range and do not overlap
        ends = sorted(body["apps_covered"]["windows"])
        assert all(e - 24 * HOUR >= body["start"] and e <= body["end"] for e in ends)
        assert all(b - a >= 24 * HOUR for a, b in zip(ends, ends[1:]))
        assert body["apps_covered"]["hours"] == 24 * len(ends)


async def test_a_client_whose_traffic_is_on_a_day_without_an_app_window_is_not_broken_down(repo):
    await seed_traffic(repo)
    namer = traffic.Namer("full", inventory())
    week = await traffic.summary(repo, "router", 168, namer, now=NOW, tz=NY)
    ps5 = next(c for c in week["clients"] if c["mac"] == PS5)
    assert ps5["apps"] == [] and ps5["traffic_hint"] is None
    assert ps5["apps_unattributed"] == {"up": 5 * 10**6, "down": 10**10} == {"up": ps5["up"], "down": ps5["down"]}
    detail = await traffic.client_detail(repo, "router", PS5, 168, namer, now=NOW, tz=NY)
    assert detail["apps"] == [] and detail["apps_unattributed"] == {"up": 5 * 10**6, "down": 10**10}
    assert detail["totals"] == {"up": 5 * 10**6, "down": 10**10}
    assert detail["apps_covered"] == {"hours": 0, "windows": []}


async def test_a_client_detail_covers_the_same_window_and_adds_up_in_every_range(repo):
    await seed_traffic(repo)
    namer = traffic.Namer("full", inventory())
    previous = None
    for name, hours in RANGES.items():
        for mac in (GAME, LAPTOP):
            d = await traffic.client_detail(repo, "router", mac, hours, namer, now=NOW, tz=NY)
            assert d["end"] - d["start"] == hours * HOUR and d["end"] == FLOOR
            for way in ("up", "down"):
                assert sum(a[way] for a in d["apps"]) + d["apps_unattributed"][way] == d["totals"][way], (name, mac)
            if daily := d["daily"]:
                assert sum(c["down"] or 0 for c in daily) == d["totals"]["down"]  # the bars are the window's total
        if previous:
            assert d["totals"]["down"] >= previous
        previous = d["totals"]["down"]


async def test_the_bars_of_a_month_are_cut_at_the_window_and_the_clock_change_day_is_whole(repo):
    await seed_traffic(repo)
    body = (await traffic_answers(repo))["30d"]
    daily = body["daily"]
    assert daily[0]["partial"] and daily[-1]["partial"] and not any(c["partial"] for c in daily[1:-1])
    assert daily[-1]["hours"] == 13 and sum(c["hours"] for c in daily) == 720
    assert next(c for c in daily if c["day"] == "2026-11-01")["hours"] == 25
    assert sum(c["down"] or 0 for c in daily) == body["totals"]["down"]


def test_windows_every_hour_tile_exactly_newest_first():
    """When a window is stored every hour (a later step), newest first chooses H, H - 24 h, H - 48 h ..."""
    end = FLOOR
    windows = [(end - k * HOUR, [("A", 1, 2)]) for k in range(0, 24 * 31)]
    chosen = traffic.cover_apps(windows, end - 168 * HOUR, end)
    assert [w for w, _ in chosen] == [end - 24 * HOUR * k for k in range(7)]
    summed = traffic.sum_apps(chosen)
    assert summed == [("A", 7, 14)]
    assert traffic.cover_apps(windows, end - 25 * HOUR, end)[0][0] == end and len(traffic.cover_apps(
        windows, end - 25 * HOUR, end)) == 1
    assert traffic.cover_apps([], 0, 100) == []


# --- uptime, loss grid, report ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("window,days", [("7d", 7), ("30d", 30), ("90d", 90)])
async def test_the_uptime_strips_span_the_range_to_the_minute_and_count_exactly_the_window(repo, window, days):
    await seed_probes(repo)
    out = await build_uptime(repo, None, window, now=NOW, tz=NY)
    assert out["end"] - out["start"] == days * 86400 and 0 <= NOW - out["end"] < 60
    assert len(out["bounds"]) == len(out["partial"]) + 1 == len(out["targets"][0]["cells"]) + 1
    assert out["partial"][0] and out["partial"][-1] and not any(out["partial"][1:-1])
    assert sum(out["hours"]) == pytest.approx(days * 24)
    sent, ok = sent_ok_in(out["start"], out["end"])
    assert out["targets"][0]["availability_pct"] == round(ok * 100.0 / sent, 3)
    # a cell counts only its own probes: the first (cut) day holds the probes of its hours inside the window only
    first = out["bounds"][1]
    if days <= 30:  # (the seed holds 31 days: the first cell of a 90-day window is before it)
        assert sent_ok_in(out["start"], first)[0] < sent_ok_in(first - 86400, first)[0]


async def test_a_longer_uptime_window_contains_the_shorter_one_and_the_report_follows_the_same_window(repo):
    await seed_probes(repo)
    sent = {}
    for days in (1, 7, 30, 90):
        rows = await daily_availability(repo, NOW - days * 86400, NY, now=NOW)
        sent[days] = sum(r["sent"] for r in rows)
    assert sent[90] >= sent[30] >= sent[7] >= sent[1] > 0
    for days, window in ((7, "7d"), (30, "30d"), (90, "90d")):
        uptime = await build_uptime(repo, None, window, now=NOW, tz=NY)
        report = await build_report(repo, None, days, now=NOW, tz=NY, window_end=uptime["end"])
        assert (report["window_start"], report["window_end"]) == (uptime["start"], uptime["end"])
        flags = report["availability"]["partial"]
        if days <= 30:  # (the report lists the days with probes: the seed's 31 days)
            assert flags[0] and flags[-1] and sum(report["availability"]["hours"]) == pytest.approx(days * 24, abs=0.03)
    plain = await build_report(repo, None, 7, now=NOW, tz=NY)
    assert plain["window_end"] - plain["window_start"] == 7 * 86400 and plain["window_end"] == NOW


async def test_the_loss_grid_holds_exactly_the_hours_of_its_window(repo):
    await seed_probes(repo)
    grid = await build_loss_grid(repo, 7, now=NOW, tz=NY)
    assert grid["end"] - grid["start"] == 7 * 86400 and grid["end"] == FLOOR + HOUR
    assert sum(not x for row in grid["outside"] for x in row) == 7 * 24
    assert grid["outside"][0][:14] == [True] * 14 and grid["outside"][0][14] is False  # from 14:00, the window's start
    assert grid["outside"][-1][:14] == [False] * 14 and grid["outside"][-1][14:] == [True] * 10  # to the hour in progress
    sent, _ = sent_ok_in(grid["start"], grid["end"])
    assert sum(map(sum, grid["sent"])) == sent


async def test_with_an_app_window_every_hour_the_apps_cover_every_range_exactly(repo):
    """Once the router's 24-hour list is kept every hour (traffic_app_hours, schema v21), the windows ending at the
    window's end, 24 h before it, ... tile any rolling range: nothing is left "not broken down"."""
    hour_down = 10**8
    rows, windows = [], []
    for h in range(FLOOR - 35 * 24 * HOUR, FLOOR, HOUR):
        rows += [TrafficHour("", h, 10**6, hour_down), TrafficHour(GAME, h, 10**6, hour_down)]
    await repo.upsert_traffic_hours("router", rows, NOW)
    for end in range(FLOOR - 33 * 24 * HOUR, FLOOR + HOUR, HOUR):  # every round's list, each the 24 hours before it
        windows = [TrafficApps(GAME, end, (("Sony PlayStation", 24 * 10**6, 24 * hour_down),)),
                   TrafficApps("", end, (("Sony PlayStation", 24 * 10**6, 24 * hour_down),))]
        await repo.store_traffic_apps("router", windows, end + 300, lambda w: None)
    namer = traffic.Namer("full", inventory())
    for name, hours in RANGES.items():
        body = await traffic.summary(repo, "router", hours, namer, now=NOW, tz=NY)
        assert body["apps_unattributed"] == {"up": 0, "down": 0}, name
        assert body["apps_covered"]["hours"] == hours, name
        assert body["apps_covered"]["windows"] == [FLOOR - k * 24 * HOUR for k in range(hours // 24)], name
        assert sum(a["down"] for a in body["apps"]) == body["totals"]["down"] == hours * hour_down, name
        detail = await traffic.client_detail(repo, "router", GAME, hours, namer, now=NOW, tz=NY)
        assert detail["apps_unattributed"] == {"up": 0, "down": 0}, name


async def test_all_holds_90d_and_is_cut_at_the_oldest_probe_and_every_cell_adds_up_to_the_window(repo):
    """The seed holds 31 days of probes: "all" starts at the oldest (to the minute), is cut in days (31 days is under 90),
    contains the 30-day and 90-day windows' probes, and its cells' hours add up to the window."""
    await seed_probes(repo)
    everything = await build_uptime(repo, None, "all", now=NOW, tz=NY)
    oldest = min(burst_times())
    assert everything["cell"] == "day" and everything["start"] == oldest and everything["end"] - everything["start"] > 30 * 86400
    assert sum(everything["hours"]) == pytest.approx((everything["end"] - everything["start"]) / 3600, abs=0.01)
    sent, ok = sent_ok_in(everything["start"], everything["end"])
    assert everything["targets"][0]["availability_pct"] == round(ok * 100.0 / sent, 3)
    quarter = await build_uptime(repo, None, "90d", now=NOW, tz=NY)
    month = await build_uptime(repo, None, "30d", now=NOW, tz=NY)
    assert quarter["targets"][0]["availability_pct"] == everything["targets"][0]["availability_pct"]  # all the data is in 90 days
    assert sent_ok_in(month["start"], month["end"])[0] <= sent
    report = await build_report(repo, None, 31, now=NOW, tz=NY, window_end=everything["end"])
    assert report["window_end"] == everything["end"] and report["window_start"] <= everything["start"]


async def test_the_folded_loss_grid_holds_exactly_the_probes_of_its_window(repo):
    await seed_probes(repo)
    grid = await build_loss_grid(repo, 60, now=NOW, tz=NY)
    assert grid["fold"] == "weekday" and grid["end"] - grid["start"] == 60 * 86400 and grid["end"] == FLOOR + HOUR
    sent, _ = sent_ok_in(grid["start"], grid["end"])
    assert sum(map(sum, grid["sent"])) == sent == sent_ok_in(0, grid["end"])[0]  # the seed (31 days) is all inside it
    assert sum(map(sum, grid["lost"])) == sent - sent_ok_in(grid["start"], grid["end"])[1]
