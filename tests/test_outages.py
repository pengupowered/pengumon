"""Outages (incidents/outages.py): the grouping rule, on the scrubbed rows of a real event (one provider event that
was seven incidents) and on small cases for each part of the rule: the gap and its boundary, the linked places, what is
never grouped, open members, the down time, the maintenance label, and the speed."""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from pengumon.config import AppConfig
from pengumon.incidents.outages import GROUPABLE_PLACES, PATH_PLACES, Rules, group_outages, linked, outage_ids
from pengumon.incidents.settings import IncidentsConfig
from pengumon.timeofday import zone

NY = zone("America/New_York")
RULES = Rules(600.0, "00:00", "06:00", NY)
NOW = 1794200000.0   # after the golden event: nothing in it is open


def row(id_, start, end, kind="isp_outage", severity="critical", cause_code="access_line_drop", expected=False,
        title="Example ISP hub outage", description="Packets to the hub are lost"):
    return {"id": id_, "start_time": start, "end_time": end, "kind": kind, "severity": severity,
            "cause_code": cause_code, "expected": expected, "title": title, "description": description}


# The rows of 2026-11-08 03:12-03:37 (America/New_York), 2026-11-06 and 2026-10-26, scrubbed: times, kinds, severities
# and cause codes as recorded; titles, descriptions and target names generic.
def golden_rows():
    hub = "Hop 3: Example ISP hub"
    out, warn = f"Line or {hub} outage", f"Line or {hub} packet loss"
    return [
        row(630, 1794125546.31, 1794125639.748027, title=out),
        row(631, 1794126027.5, 1794126034.5102804, "isp_loss", "warning", title=warn),
        row(632, 1794126114.7, 1794126146.04677, title=out),
        row(633, 1794126301.47, 1794126347.4956355, "isp_outage", "critical", "internet_backbone_transit_loss",
            title="ISP backbone outage", description="The gateway and the hub answer, public servers do not"),
        row(634, 1794126361.03, 1794126372.0897717, "isp_loss", "warning", title=warn),
        row(635, 1794126386.47, 1794126944.3420088, title=out),
        row(636, 1794126956.81, 1794127058.723357, title=out),
        row(627, 1793978508.65, 1793978519.2001703, "isp_loss", "warning", title=warn),
        row(601, 1793070850.57, 1793070860.5641558, "gateway_latency", "warning", "double_nat_congestion",
            title="Hop 2: Example modem latency delay"),
        row(602, 1793070910.54, 1793070919.54187, "gateway_latency", "warning", "double_nat_congestion",
            title="Hop 2: Example modem latency delay"),
    ]


def by_id(outages):
    return {o["id"]: o for o in outages}


def test_the_real_event_is_one_outage_and_the_rest_stand_alone():
    outages = by_id(group_outages(golden_rows(), RULES, NOW))
    assert sorted(outages) == [601, 627, 630]
    event = outages[630]
    assert event["incident_ids"] == [630, 631, 632, 633, 634, 635, 636]
    assert event["start_time"] == 1794125546.31 and event["end_time"] == 1794127058.723357
    assert datetime.fromtimestamp(event["start_time"], NY).strftime("%H:%M:%S") == "03:12:26"
    assert (event["drops"], event["critical_drops"], event["severity"]) == (7, 5, "critical")
    assert round(event["down_seconds"]) == 831 and int(event["down_seconds"]) == 830   # the five critical drops
    assert round(event["duration_seconds"]) == 1512
    assert round(event["affected_seconds"]) == 831 + 7 + 11   # + the two warnings; they overlap nothing
    assert event["places"] == ["line", "isp"]
    assert event["title"] == "Line or Hop 3: Example ISP hub outage" and event["cause_code"] == "access_line_drop"
    assert event["likely_maintenance"] is True
    assert (event["ongoing"], event["settling"], event["end_time"] is None) == (False, False, False)
    assert outages[627]["drops"] == 1 and outages[627]["likely_maintenance"] is False   # 10:21 a.m.
    assert outages[601]["incident_ids"] == [601, 602] and outages[601]["places"] == ["gateway"]
    assert outages[601]["down_seconds"] == 0.0   # two warnings: nothing was down


def test_a_shorter_gap_splits_the_event_where_the_gap_is_longer():
    outages = by_id(group_outages(golden_rows(), Rules(300.0, "00:00", "06:00", NY), NOW))
    assert outages[630]["incident_ids"] == [630]            # 630 -> 631 is 6.5 minutes
    assert outages[631]["incident_ids"] == [631, 632, 633, 634, 635, 636]
    assert outages[631]["likely_maintenance"] is True


def test_gap_zero_turns_grouping_off_and_every_groupable_incident_is_alone():
    outages = group_outages(golden_rows(), Rules(0.0, "00:00", "06:00", NY), NOW)
    assert len(outages) == 10 and all(o["drops"] == 1 for o in outages)
    assert not any(o["settling"] for o in outages)
    assert outage_ids(golden_rows(), Rules(0.0), NOW) == {i: i for i in (630, 631, 632, 633, 634, 635, 636, 627, 601, 602)}


def test_the_gap_boundary_joins_at_exactly_the_gap_and_not_a_moment_later():
    first = row(1, 1000.0, 1100.0)
    assert [o["drops"] for o in group_outages([first, row(2, 1100.0 + 600.0, 1800.0)], RULES, NOW)] == [2]
    assert [o["drops"] for o in group_outages([first, row(2, 1100.0 + 600.001, 1800.0)], RULES, NOW)] == [1, 1]


def test_the_gap_is_from_the_outages_latest_end_not_the_previous_incident():
    long_one = row(1, 1000.0, 5000.0)                       # ends last
    inside = row(2, 1100.0, 1200.0, cause_code="access_line_drop")   # starts inside it, ends early
    later = row(3, 5000.0 + 599.0, 5900.0)
    assert [o["incident_ids"] for o in group_outages([long_one, inside, later], RULES, NOW)] == [[1, 2, 3]]


def test_places_join_only_when_they_are_neighbors_on_the_path():
    router = row(2, 1100.0, 1200.0, "local_down", "critical", "local_wifi_mesh_drop")
    line = row(1, 1000.0, 1050.0)
    isp = row(3, 1100.0, 1200.0, "backbone_loss", "warning", "internet_backbone_transit_loss")
    gateway = row(4, 1100.0, 1200.0, "gateway_down", "critical", "gateway_unreachable")
    assert [o["incident_ids"] for o in group_outages([line, router], RULES, NOW)] == [[1], [2]]   # line is 2 from router
    assert [o["incident_ids"] for o in group_outages([line, isp], RULES, NOW)] == [[1, 3]]
    assert [o["incident_ids"] for o in group_outages([line, gateway], RULES, NOW)] == [[1, 4]]
    assert [o["incident_ids"] for o in group_outages([router, gateway], RULES, NOW)] == [[2, 4]]
    # linked to ANY member: the router drop joins once a gateway drop is in
    assert [o["incident_ids"] for o in group_outages([line, gateway, row(5, 1300.0, 1400.0, "local_down", "critical",
                                                                       "local_wifi_mesh_drop")], RULES, NOW)] == [[1, 4, 5]]


def test_the_link_table():
    path = PATH_PLACES
    for i, a in enumerate(path):
        for j, b in enumerate(path):
            assert linked(a, b) == (abs(i - j) <= 1), (a, b)
    assert {p for p in path if linked("dns", p)} == {"line", "isp", "internet"} and linked("dns", "dns")
    assert all(linked(p, "dns") == linked("dns", p) for p in path)
    assert GROUPABLE_PLACES == set(path) | {"dns"}


def test_dns_joins_the_line_but_not_the_router():
    dns = row(2, 1100.0, 1200.0, "dns_outage", "critical", "dns_resolver_failure")
    assert [o["incident_ids"] for o in group_outages([row(1, 1000.0, 1050.0), dns], RULES, NOW)] == [[1, 2]]
    router = row(1, 1000.0, 1050.0, "local_down", "critical", "local_wifi_mesh_drop")
    assert [o["incident_ids"] for o in group_outages([router, dns], RULES, NOW)] == [[1], [2]]


def test_load_unknown_service_and_expected_incidents_are_never_grouped():
    rows = [row(1, 1000.0, 1050.0),
            row(2, 1060.0, 1100.0, "busy_latency", "warning", "load_induced_latency"),
            row(3, 1060.0, 1100.0, "nominal", "warning", "unknown_outage"),
            row(4, 1060.0, 1100.0, "service_down", "warning", "service_unavailable"),
            row(5, 1060.0, 1100.0, expected=True),
            row(6, 1110.0, 1150.0)]
    outages = group_outages(rows, RULES, NOW)
    assert [o["incident_ids"] for o in outages] == [[1, 6]]
    assert outage_ids(rows, RULES, NOW) == {1: 1, 6: 1}


def test_an_open_member_makes_the_outage_ongoing_and_is_still_going():
    rows = [row(1, 1000.0, 1100.0), row(2, 5000.0, None)]
    far = group_outages(rows, RULES, 5100.0)
    assert [o["drops"] for o in far] == [1, 1]
    # an open member ends "now": a drop that starts a few minutes after now - gap is still joined
    rows = [row(1, 1000.0, None), row(2, 1000.0 + 7200.0, 1000.0 + 7300.0)]
    outages = group_outages(rows, RULES, 1000.0 + 7300.0)
    assert [o["incident_ids"] for o in outages] == [[1, 2]]
    assert outages[0]["ongoing"] is True and outages[0]["end_time"] is None and outages[0]["settling"] is False
    assert outages[0]["duration_seconds"] == 7300.0
    assert outages[0]["down_seconds"] == 7300.0   # the open critical drop counts until now


def test_an_outage_that_ended_less_than_a_gap_ago_is_settling():
    rows = [row(1, 1000.0, 1100.0)]
    assert group_outages(rows, RULES, 1100.0 + 599.0)[0]["settling"] is True
    assert group_outages(rows, RULES, 1100.0 + 600.0)[0]["settling"] is False
    assert group_outages(rows, Rules(0.0), 1100.0 + 1.0)[0]["settling"] is False   # grouping off: nothing can join


def test_down_time_counts_overlapping_critical_drops_once_and_ignores_warnings():
    rows = [row(1, 1000.0, 1100.0), row(2, 1050.0, 1200.0, "backbone_loss", "critical", "internet_backbone_transit_loss"),
            row(3, 1300.0, 1400.0, "isp_loss", "warning")]
    o = group_outages(rows, RULES, NOW)[0]
    assert o["down_seconds"] == 200.0 and o["affected_seconds"] == 200.0 + 100.0
    assert o["duration_seconds"] == 400.0 and o["critical_drops"] == 2 and o["severity"] == "critical"


def test_the_title_is_the_longest_critical_drop_else_the_longest_drop():
    rows = [row(1, 1000.0, 1100.0, title="short critical"), row(2, 1150.0, 1400.0, "isp_loss", "warning", title="long warning"),
            row(3, 1450.0, 1600.0, title="longest critical", cause_code="access_line_drop")]
    assert group_outages(rows, RULES, NOW)[0]["title"] == "longest critical"
    warnings = [row(1, 1000.0, 1100.0, "isp_loss", "warning", title="a"), row(2, 1150.0, 1400.0, "isp_loss", "warning", title="b")]
    assert group_outages(warnings, RULES, NOW)[0]["title"] == "b"


def test_places_are_listed_in_path_order():
    rows = [row(1, 1000.0, 1050.0, "backbone_loss", "warning", "internet_backbone_transit_loss"),
            row(2, 1060.0, 1100.0, "dns_loss", "warning", "dns_resolver_failure"),
            row(3, 1110.0, 1150.0, "isp_loss", "warning")]
    assert group_outages(rows, RULES, NOW)[0]["places"] == ["line", "isp", "dns"]


def at(h, m=0, day=8):
    return datetime(2026, 11, day, h, m, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_likely_maintenance_needs_the_window_and_provider_hops_only():
    night = row(1, at(3, 12), at(3, 20))
    assert group_outages([night], RULES, NOW)[0]["likely_maintenance"] is True
    assert group_outages([row(1, at(12), at(12, 5))], RULES, NOW)[0]["likely_maintenance"] is False   # by day
    assert group_outages([row(1, at(6), at(6, 5))], RULES, NOW)[0]["likely_maintenance"] is False     # 06:00 is outside
    assert group_outages([row(1, at(0), at(0, 5))], RULES, NOW)[0]["likely_maintenance"] is True      # 00:00 is inside
    gateway = row(1, at(3, 12), at(3, 20), "gateway_down", "critical", "gateway_unreachable")
    assert group_outages([gateway], RULES, NOW)[0]["likely_maintenance"] is False   # not a provider hop
    mixed = [night, row(2, at(3, 25), at(3, 30), "gateway_down", "critical", "gateway_unreachable")]
    assert group_outages(mixed, RULES, NOW)[0]["likely_maintenance"] is False       # one member is not line or isp
    # only the start decides: begun at 05:58, it runs past the window
    assert group_outages([row(1, at(5, 58), at(6, 30))], RULES, NOW)[0]["likely_maintenance"] is True


def test_the_maintenance_window_may_cross_midnight_and_is_read_in_the_configured_zone():
    wrap = Rules(600.0, "22:00", "02:00", NY)
    assert group_outages([row(1, at(23), at(23, 5))], wrap, NOW)[0]["likely_maintenance"] is True
    assert group_outages([row(1, at(1), at(1, 5))], wrap, NOW)[0]["likely_maintenance"] is True
    assert group_outages([row(1, at(3), at(3, 5))], wrap, NOW)[0]["likely_maintenance"] is False
    utc = Rules(600.0, "00:00", "06:00", zone("UTC"))   # 03:12 in New York is 08:12 UTC
    assert group_outages([row(1, at(3, 12), at(3, 20))], utc, NOW)[0]["likely_maintenance"] is False


def test_no_maintenance_window_means_never():
    assert group_outages([row(1, at(3), at(3, 5))], Rules(600.0, None, None, NY), NOW)[0]["likely_maintenance"] is False


def test_the_label_never_changes_a_figure():
    day, night = row(1, at(12), at(12, 10)), row(1, at(3), at(3, 10))
    a, b = group_outages([day], RULES, NOW)[0], group_outages([night], RULES, NOW)[0]
    for key in ("duration_seconds", "down_seconds", "affected_seconds", "drops", "severity"):
        assert a[key] == b[key]


def test_rows_in_any_order_and_models_group_the_same():
    rows = golden_rows()
    assert group_outages(list(reversed(rows)), RULES, NOW) == group_outages(rows, RULES, NOW)


def test_incident_models_group_like_rows():
    from pengumon.incidents.model import Incident
    models = [Incident(id=r["id"], start_time=r["start_time"], end_time=r["end_time"], cause_code=r["cause_code"],
                       title=r["title"], description=r["description"], severity=r["severity"], kind=r["kind"])
              for r in golden_rows()]
    assert group_outages(models, RULES, NOW) == group_outages(golden_rows(), RULES, NOW)


def test_ten_thousand_incidents_group_quickly():
    rows = []
    for k in range(10_000):
        kind, code = (("isp_outage", "access_line_drop"), ("gateway_down", "gateway_unreachable"),
                      ("busy_latency", "load_induced_latency"), ("dns_loss", "dns_resolver_failure"))[k % 4]
        start = 1_700_000_000.0 + k * 420.0 + (k % 7) * 30.0
        rows.append(row(k + 1, start, start + 60.0 + k % 90, kind, "critical" if k % 3 else "warning", code))
    started = time.perf_counter()
    outages = group_outages(rows, RULES, 1_800_000_000.0)
    took = time.perf_counter() - started
    print(f"grouping 10,000 incidents: {took * 1000:.1f} ms into {len(outages)} outages")
    assert took < 0.5
    assert sum(o["drops"] for o in outages) == 7_500   # the load ones are in no outage
    # the same with grouping off (every groupable incident its own outage)
    started = time.perf_counter()
    group_outages(rows, Rules(0.0, "00:00", "06:00", NY), 1_800_000_000.0)
    print(f"grouping off: {(time.perf_counter() - started) * 1000:.1f} ms")


def test_rules_read_the_live_config():
    config = AppConfig()
    rules = Rules.from_config(config)
    assert (rules.gap_seconds, rules.maintenance_start, rules.maintenance_end) == (600.0, "00:00", "06:00")
    config.incidents.group_gap_minutes = 0
    assert Rules.from_config(config).grouping is False
    config.general.timezone = "America/New_York"
    assert Rules.from_config(config).in_maintenance_window(at(3, 12))
    assert Rules.from_config(None).gap_seconds == 600.0   # no config: the defaults


def test_the_incidents_settings_are_checked():
    assert IncidentsConfig().group_gap_minutes == 10
    for bad in ({"group_gap_minutes": -1}, {"group_gap_minutes": 121}, {"maintenance_start": None},
                {"maintenance_end": None}, {"maintenance_start": "03:00", "maintenance_end": "03:00"},
                {"maintenance_start": "25:00"}, {"nope": 1}):
        with pytest.raises(ValidationError):
            IncidentsConfig(**bad)
    off = IncidentsConfig(maintenance_start=None, maintenance_end=None)
    assert off.maintenance_start is None
    assert IncidentsConfig(group_gap_minutes=0).group_gap_minutes == 0
