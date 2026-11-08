"""reports.build_report beyond the recorded golden scenario: empty data, ordering and the worst-hours list."""

from types import SimpleNamespace

import pytest

from pengumon.reports import reliability
from pengumon.reports.reliability import build_report

NOW = 1_760_000_000.0


class Repo:
    def __init__(self, daily=(), hourly=(), incidents=()):
        self.daily, self.hourly, self.incidents = list(daily), list(hourly), list(incidents)

    async def get_incidents_since(self, start_time):
        return self.incidents


@pytest.fixture(autouse=True)
def fixed_rows(monkeypatch):
    """The report's two rollup queries (pengumon.reports.queries) answer with the Repo's rows."""
    async def daily(repo, *args, **kwargs):
        return repo.daily

    async def hourly(repo, *args, **kwargs):
        return repo.hourly

    monkeypatch.setattr(reliability, "daily_availability", daily)
    monkeypatch.setattr(reliability, "hourly_loss", hourly)


def cfg(*ids):
    return SimpleNamespace(targets=[SimpleNamespace(id=i, name=i.upper()) for i in ids], dns_probes=[])


async def test_a_report_without_data_is_empty_but_complete():
    report = await build_report(Repo(), None, 7, now=NOW)
    assert report == {
        "days": 7, "tz_offset_minutes": 0, "generated_at": NOW,
        "availability": {"days": [], "hops": []},
        "incidents": {"count": 0, "minutes": 0.0, "by_cause": [], "excluded_expected": 0,
                      "outages": {"count": 0, "down_minutes": 0.0, "longest_minutes": 0.0, "maintenance": 0}},
        "loss_by_hour": [], "worst_hours": [],
    }


async def test_hops_follow_the_config_order_and_unknown_ones_come_last():
    daily = [{"target_id": t, "day": "2025-10-08", "sent": 100, "ok": ok} for t, ok in (("zzz", 50), ("b", 100), ("a", 99))]
    hops = (await build_report(Repo(daily=daily), cfg("a", "b"), 1, now=NOW))["availability"]["hops"]
    assert [(h["target_id"], h["name"], h["availability_pct"]) for h in hops] == [
        ("a", "A", 99.0), ("b", "B", 100.0), ("zzz", "zzz", 50.0)]


async def test_a_day_without_probes_for_a_hop_is_null_and_availability_ignores_it():
    daily = [{"target_id": "a", "day": "d1", "sent": 10, "ok": 10}, {"target_id": "b", "day": "d2", "sent": 10, "ok": 5}]
    report = await build_report(Repo(daily=daily), cfg("a", "b"), 2, now=NOW)
    assert report["availability"]["days"] == ["d1", "d2"]
    assert [(h["target_id"], h["per_day"], h["availability_pct"]) for h in report["availability"]["hops"]] == [
        ("a", [100.0, None], 100.0), ("b", [None, 50.0], 50.0)]


async def test_worst_hours_are_the_five_lossiest_with_ties_by_hour_and_none_without_loss():
    hourly = [{"hour": h, "sent": 1000, "lost": lost} for h, lost in
              ((0, 0), (1, 5), (2, 9), (3, 5), (4, 9), (5, 1), (6, 2), (7, 3), (8, 0))]
    hourly.append({"hour": 9, "sent": 0, "lost": 0})
    report = await build_report(Repo(hourly=hourly), None, 1, now=NOW)
    assert [h["hour"] for h in report["worst_hours"]] == [2, 4, 1, 3, 7]
    assert report["loss_by_hour"][-1] == {"hour": 9, "sent": 0, "lost": 0, "loss_pct": 0.0}
    assert report["worst_hours"][0]["loss_pct"] == 0.9
    quiet = await build_report(Repo(hourly=[{"hour": 3, "sent": 10, "lost": 0}]), None, 1, now=NOW)
    assert quiet["worst_hours"] == []


async def test_incident_totals_exclude_expected_ones_and_order_causes_by_count_then_name():
    """By cause category, the kind first: an access_line_drop of upstream latency is the provider's network (isp), a
    gateway incident of an older build (double_nat_congestion) the gateway."""
    rows = [{"cause_code": c, "kind": k, "duration_seconds": d, "expected": e} for c, k, d, e in (
        ("double_nat_congestion", "gateway_loss", 60.0, 0), ("access_line_drop", "isp_loss", 120.0, 0),
        ("gateway_unreachable", "gateway_down", None, 0), ("access_line_drop", "upstream_latency", 30.0, 0),
        ("access_line_drop", "isp_outage", 30.0, 0), ("dns_resolver_failure", "dns_loss", 900.0, 1))]
    inc = (await build_report(Repo(incidents=rows), None, 1, now=NOW))["incidents"]
    assert inc["count"] == 5 and inc["excluded_expected"] == 1 and inc["minutes"] == 4.0
    assert inc["by_cause"] == [{"cause": "gateway", "count": 2, "minutes": 1.0}, {"cause": "line", "count": 2, "minutes": 2.5},
                               {"cause": "isp", "count": 1, "minutes": 0.5}]


async def test_an_open_incident_counts_as_downtime_until_now():
    rows = [{"cause_code": "unknown_outage", "start_time": NOW - 1800, "duration_seconds": None, "expected": 0, "kind": "gateway_down"},
            {"cause_code": "unknown_outage", "start_time": NOW - 7200, "duration_seconds": 60, "expected": 0, "kind": "gateway_down"}]
    incidents = (await build_report(Repo(incidents=rows), None, 7, now=NOW))["incidents"]
    assert incidents["minutes"] == 31.0 and incidents["by_cause"][0]["minutes"] == 31.0


def drop_row(iid, start, end, kind="isp_outage", cause_code="access_line_drop", severity="critical", expected=0):
    return {"id": iid, "cause_code": cause_code, "kind": kind, "start_time": start, "end_time": end,
            "duration_seconds": None if end is None else end - start, "severity": severity, "expected": expected,
            "title": "Line outage", "description": "d"}


async def test_the_report_counts_outages_beside_the_incident_figures():
    """Three drops two minutes apart are one outage (down: the critical ones, overlaps once); a lone drop a day later is
    another; the incident count, minutes and causes stay per incident."""
    t = (NOW // 86400 - 2) * 86400 + 3 * 3600   # 03:00 UTC two days back: inside the default maintenance hours (UTC)
    rows = [drop_row(1, t, t + 60), drop_row(2, t + 180, t + 240, "isp_loss", severity="warning"),
            drop_row(3, t + 300, t + 420), drop_row(4, NOW - 5000, NOW - 4900),
            drop_row(5, t + 400, t + 450, expected=1)]
    config = SimpleNamespace(targets=[], dns_probes=[], incidents=None, general=None)
    inc = (await build_report(Repo(incidents=rows), config, 7, now=NOW))["incidents"]
    assert inc["count"] == 4 and inc["excluded_expected"] == 1                       # per incident, as before
    assert inc["outages"] == {"count": 2, "down_minutes": round((60 + 120 + 100) / 60, 1),
                              "longest_minutes": 7.0, "maintenance": 1}


async def test_outages_follow_the_gap_setting():
    t = NOW - 5000
    rows = [drop_row(1, t, t + 60), drop_row(2, t + 180, t + 240)]
    joined = await build_report(Repo(incidents=rows), SimpleNamespace(targets=[], dns_probes=[]), 1, now=NOW)
    assert joined["incidents"]["outages"]["count"] == 1
    off = SimpleNamespace(targets=[], dns_probes=[], incidents=SimpleNamespace(
        group_gap_minutes=0, maintenance_start="00:00", maintenance_end="06:00"), general=None)
    assert (await build_report(Repo(incidents=rows), off, 1, now=NOW))["incidents"]["outages"]["count"] == 2
