"""The AsusWRT router's statistics pages (drivers/asuswrt_traffic.py, drivers/asuswrt.py): parsing, the request
allowlist, the Traffic Analyzer rounds and their request budget, the status pages, and the pause during speed tests.
Every page is a made-up fixture in the shape a real router answers with (tests/fixtures/asuswrt/); no router is
contacted."""

import inspect
import logging
import re
import time

import httpx
import pytest

from pengumon.capabilities import MAX_UPTIME_SECONDS, Capability, DriverError, TrafficReading
from pengumon.drivers import asuswrt, asuswrt_traffic as stats
from pengumon.drivers.asuswrt import ALLOWED_REQUESTS, AsusWrtDriver

from asuswrt_fake import APPGET, HOUR, T0, UPTIME_S, FakeAsus, fixture, statistics
from device_helpers import Clock, build_router

MACS = [f"02:00:00:00:00:0{i}" for i in range(1, 9)]


def make(clock, fake=None, **options):
    fake = fake or FakeAsus(clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    device = build_router(username="u", password="p", client=client, clock=clock, wall_clock=clock,
                          options=options or {})
    device.driver._token = "tok"
    return device, fake


async def run_polls(device, clock, seconds, step=10.0):
    """Polls every `step` seconds for `seconds`; returns the results the driver gave (its PollResults)."""
    results = []
    driver = device.driver
    for _ in range(int(seconds / step)):
        results.append(await driver.poll())
        clock.now += step
    return results


def traffic_of(results):
    return [r.readings[Capability.TRAFFIC_USAGE] for r in results if Capability.TRAFFIC_USAGE in r.readings]


# --- parsing --------------------------------------------------------------------------------------------------------

def test_a_figure_that_is_no_ascii_number_leaves_its_row_out_and_does_not_end_the_round():
    assert stats.app_rows([["web", "\u00b2", "1"], ["mail", "12", "34"]]) == (("mail", 12, 34),)
    assert stats.client_totals([["02:00:00:00:00:01", "\u0663", "1"]]) == {}
    assert stats.hour_series([["1" * 21, "1"], ["5", "6"]], T0, "all") == [stats.TrafficHour("all", 3600 * (int(T0 // 3600) - 1), 5, 6)]


def test_the_pages_parse_with_their_byte_order_mark_and_an_empty_answer_is_no_data_not_an_error():
    rows = stats.parse_statistics(fixture("getAppTraffic_all_detail_24.asp"))
    assert fixture("getAppTraffic_all_detail_24.asp").startswith("﻿") and len(rows) == 8
    assert stats.parse_statistics(fixture("empty_dura1.asp")) == []
    assert stats.parse_statistics(fixture("not_found_404.html")) is None  # not the page's shape
    assert stats.parse_statistics("﻿array_statistics = [[1, 2], [3") is None  # cut short
    assert stats.parse_statistics(statistics([])) == []


def test_slot_23_is_the_last_complete_hour_before_the_hour_of_date():
    rows = stats.parse_statistics(fixture("getWanTraffic_all_hour_24.asp"))
    for date in (T0 + 6 * HOUR, T0 + 6 * HOUR + 2400):  # 19:00:00 and 19:40 -> slot 23 is 18:00-19:00
        series = stats.hour_series(rows, date, "")
        assert len(series) == 24 and series[-1].hour == T0 + 5 * HOUR and series[0].hour == T0 - 18 * HOUR
        assert (series[-1].up, series[-1].down) == (144678858, 454485253)
        assert series[13].down == 40_000_000_000  # the morning's download


def test_bad_rows_are_skipped_and_never_crash_the_parse():
    rows = [[1, 2], [-5, 3], ["7", "8"], ["x", 1], [10 * 10**12 + 1, 0], None, [1], [True, 2], [4.0, 5]]
    series = stats.hour_series(rows, T0, "")
    assert [(s.up, s.down) for s in series] == [(1, 2), (7, 8), (4, 5)]
    assert [s.hour for s in series] == [T0 - 9 * HOUR, T0 - 7 * HOUR, T0 - HOUR]  # each keeps its own hour
    apps = stats.app_rows(stats.parse_statistics(fixture("getWanTraffic_all_detail_24.asp")) + [[None, 1, 2], ["a", -1, 2]])
    assert ("Dynamic Host Configuration Protocol (DH", 656, 378) in apps  # a name the router cut off is kept as is
    assert len(apps) == 8
    totals = stats.client_totals([["02:00:00:00:00:0a", 1, 2], ["not a mac", 1, 2], ["02-00-00-00-00-0B", "3", "4"]])
    assert totals == {"02:00:00:00:00:0A": ("02:00:00:00:00:0a", 1, 2), "02:00:00:00:00:0B": ("02-00-00-00-00-0B", 3, 4)}


def test_the_status_pages_and_the_uptime_hook():
    assert stats.wan_link_mbps(fixture("ajax_ethernet_ports.asp")) == 2500  # "WAN 0": "Q"
    assert stats.wan_link_mbps(fixture("ajax_ethernet_ports.asp").replace('"WAN 0": "Q"', '"WAN 0": "X"')) == 0
    assert stats.wan_link_mbps(fixture("ajax_ethernet_ports.asp").replace('"WAN 0": "Q"', '"WAN 0": "?"')) is None
    assert stats.wan_link_mbps(fixture("not_found_404.html")) is None
    assert stats.cpu_temp_c(fixture("ajax_coretmp.asp")) == 64.1
    assert stats.cpu_temp_c("﻿curr_cpuTemp = fanctrl_info[0];") is None  # unset JavaScript
    assert stats.cpu_temp_c('curr_cpuTemp = "512";') is None  # not a temperature
    assert stats.uptime_seconds(fixture("appget_uptime.json")) == UPTIME_S
    assert stats.uptime_seconds('{"uptime":""}') is None


# --- the request allowlist ------------------------------------------------------------------------------------------

EXPECTED_REQUESTS = {
    ("POST", "login.cgi"), ("POST", "appGet.cgi"), ("GET", "update.cgi"), ("GET", "getWanTraffic.asp"),
    ("GET", "getAppTraffic.asp"), ("GET", "ajax_ethernet_ports.asp"), ("GET", "ajax_coretmp.asp"),
}


async def test_the_driver_makes_only_the_allowed_read_only_requests():
    """The list is exactly the read-only hooks and pages; every request a day of polling makes is on it; anything else
    is refused before it is sent; and no forbidden name appears in the driver at all."""
    assert ALLOWED_REQUESTS == EXPECTED_REQUESTS
    clock = Clock(T0 + 30)
    device, fake = make(clock)
    device.driver._token = None  # a login too
    await run_polls(device, clock, 2 * HOUR)
    made = {(m, p) for m, p, _ in fake.requests}
    assert made <= ALLOWED_REQUESTS and {("GET", "getWanTraffic.asp"), ("GET", "getAppTraffic.asp")} <= made
    with pytest.raises(DriverError, match="refused to send"):
        await device.driver._request("GET", "apply.cgi")
    source = inspect.getsource(asuswrt) + inspect.getsource(stats)
    for forbidden in ("applyapp", "set_", "netool", "cleanLog", "nvram_set", "syscmd"):
        assert not re.search(rf"[\"'/]{forbidden}", source), forbidden


# --- the rounds -----------------------------------------------------------------------------------------------------

async def test_the_first_round_backfills_24_hours_a_few_requests_per_poll():
    clock = Clock(T0 + 19 * 60)
    device, fake = make(clock)
    results = await run_polls(device, clock, 120)
    readings = traffic_of(results)
    hours = [h for r in readings for h in r.hours]
    wan = [h for h in hours if h.client == ""]
    assert len(wan) == 24 and wan[-1].hour == T0 - HOUR
    # every client with traffic: its hourly series and its apps; the client without any (0, 0) needs neither
    assert {h.client for h in hours if h.client} == set(MACS) - {MACS[5]}
    apps = {a.client: a for r in readings for a in r.apps}
    assert apps[MACS[2]].apps[0] == ("Sony PlayStation", 4_000_000_000, 196_000_000_000)
    assert set(apps) == set(MACS) | {""} and apps[MACS[5]].apps == ()  # no traffic: an empty window, no request
    assert apps[""].window_end == T0
    traffic_requests = [p for p in fake.paths() if p.startswith("get")]
    assert len(traffic_requests) == 3 + 2 * 7  # the WAN series, the totals, every client's apps; 2 per client
    assert len(readings) >= (3 + 2 * 7) // stats.REQUESTS_PER_POLL  # spread over several polls


async def test_a_poll_makes_at_most_a_few_extra_requests():
    """The appGet request, then at most REQUESTS_PER_POLL statistics pages (status pages and traffic share them)."""
    clock = Clock(T0 + 19 * 60)
    device, fake = make(clock)
    extra = []
    for _ in range(30):
        before = len(fake.requests)
        await device.driver.poll()
        extra.append(sum(1 for _, p, _ in fake.requests[before:] if p.endswith(".asp")))
        clock.now += 10
    assert max(extra) == stats.REQUESTS_PER_POLL == 3 and sum(extra) == 2 * 5 + 3 + 2 * 7


async def test_a_round_whose_newest_hour_is_unchanged_stops_after_one_request():
    """With a 60 s interval, the minutes in which the router has nothing new cost one request each."""
    clock = Clock(T0 + 10 * 60)
    device, fake = make(clock, traffic_interval_seconds=60)
    await run_polls(device, clock, 300)  # the first round is done by then
    before = len(fake.requests)
    first = fake.count("getWanTraffic.asp", client="all", mode="hour")
    results = await run_polls(device, clock, 600)
    assert fake.count("getWanTraffic.asp", client="all", mode="hour") - first == 10
    assert [p for p in fake.paths()[before:] if p.startswith("get")] == ["getWanTraffic.asp"] * 10
    assert traffic_of(results) == []


async def test_a_new_hour_fetches_only_the_clients_whose_totals_changed():
    clock = Clock(T0 + 10 * 60)
    fake = FakeAsus(clock, changing=(MACS[2], MACS[6]))
    device, _ = make(clock, fake)
    first = {a.client: a for r in traffic_of(await run_polls(device, clock, 300)) for a in r.apps}
    fake.requests.clear()
    clock.now = T0 + HOUR + 5 * 60 - 10  # the next round is due at 5 past the hour
    results = await run_polls(device, clock, 120)
    clients = sorted({q["client"] for _, p, q in fake.requests if p == "getWanTraffic.asp" and q["client"] != "all"})
    assert clients == [MACS[2], MACS[6]]
    assert fake.count("getWanTraffic.asp", client="all", mode="hour") == 1 and fake.count("getAppTraffic.asp") == 1
    hours = [h for r in traffic_of(results) for h in r.hours]
    assert {h.client for h in hours} == {"", MACS[2], MACS[6]} and max(h.hour for h in hours) == T0
    # every client with traffic has this round's window: the unchanged ones the apps read last time, at no request
    apps = {a.client: a for r in traffic_of(results) for a in r.apps}
    assert set(apps) == set(first) and {a.window_end for a in apps.values()} == {T0 + HOUR}
    assert apps[MACS[5]].apps == ()  # (no traffic: an empty window, as in every round)
    assert all(apps[m].apps == first[m].apps for m in MACS if m not in (MACS[2], MACS[5], MACS[6]))


async def test_the_hour_is_read_again_once_the_router_publishes_it():
    """Read a minute after the hour, before the router refreshed: the newest slot is still empty. The next minute it
    is not, so it is read again and the stored hour is corrected (the store replaces it)."""
    clock = Clock(T0 + 10 * 60)
    fake = FakeAsus(clock, refresh_s=90.0)
    device, _ = make(clock, fake, traffic_interval_seconds=60)
    await run_polls(device, clock, 300)
    clock.now = T0 + HOUR + 30
    early = traffic_of(await run_polls(device, clock, 60))
    late = traffic_of(await run_polls(device, clock, 120))
    newest = [h for r in early for h in r.hours if h.client == "" and h.hour == T0]
    assert newest and (newest[0].up, newest[0].down) == (0, 0)
    corrected = [h for r in late for h in r.hours if h.client == "" and h.hour == T0]
    assert corrected and corrected[-1].down == 454485253


@pytest.mark.parametrize("trouble", ["transport", "refused"])
async def test_an_error_stops_the_round_and_the_round_is_redone_in_full(trouble):
    clock = Clock(T0 + 10 * 60)
    device, fake = make(clock)
    await device.driver.poll()  # the round starts: the WAN series and two more requests
    if trouble == "transport":
        fake.fail = httpx.ConnectTimeout("timeout")
    else:
        fake.status = 401
    clock.now += 10
    result = await device.driver.poll()  # the poll itself works; the round stops at once
    assert result.readings[Capability.WAN_COUNTERS] and not device.driver._traffic.busy
    fake.requests.clear()
    clock.now += 10
    await device.driver.poll()
    assert [p for p in fake.paths() if p.startswith("get")] == []  # not at the next poll: RETRY_SECONDS later
    clock.now += stats.RETRY_SECONDS
    await run_polls(device, clock, 120)
    # nothing of the stopped round counted as seen: the newest hour, the totals and every client again
    assert fake.count("getWanTraffic.asp", client="all", mode="hour") == 1
    assert len({q["client"] for _, p, q in fake.requests if p == "getWanTraffic.asp"}) == 8


async def test_a_router_without_the_traffic_analyzer_says_so_once_and_is_asked_again_a_day_later():
    clock = Clock(T0 + 10 * 60)
    fake = FakeAsus(clock, pages={"getWanTraffic.asp": None})
    device, _ = make(clock, fake)
    results = await run_polls(device, clock, 2 * HOUR)
    reasons = [r.missing[Capability.TRAFFIC_USAGE] for r in results if Capability.TRAFFIC_USAGE in r.missing]
    assert reasons == [stats.NOT_OFFERED]
    assert fake.count("getWanTraffic.asp") == 1
    clock.now += stats.UNSUPPORTED_RECHECK_SECONDS
    await run_polls(device, clock, 20)
    assert fake.count("getWanTraffic.asp") == 2


async def test_a_traffic_analyzer_with_no_data_says_so_once_per_change():
    clock = Clock(T0 + 10 * 60)
    fake = FakeAsus(clock, pages={"getWanTraffic.asp": fixture("empty_dura1.asp")})
    device, _ = make(clock, fake)
    results = await run_polls(device, clock, 3 * HOUR)
    reasons = [r.missing[Capability.TRAFFIC_USAGE] for r in results if Capability.TRAFFIC_USAGE in r.missing]
    assert reasons == [stats.NO_DATA]  # once, though every round finds it so
    assert fake.count("getWanTraffic.asp") == 4  # one request per round: the first and three on the hour
    assert fake.count("getAppTraffic.asp") == 0


async def test_traffic_can_be_switched_off_and_is_then_not_offered():
    clock = Clock(T0)
    device, fake = make(clock, traffic_usage=False)
    await run_polls(device, clock, 120)
    assert Capability.TRAFFIC_USAGE not in device.driver.offers and Capability.TRAFFIC_USAGE in AsusWrtDriver.offers
    assert fake.count("getWanTraffic.asp") == 0 and device.driver.traffic_interval_seconds is None


def test_the_interval_has_a_floor_of_a_minute():
    with pytest.raises(ValueError):
        AsusWrtDriver.options_model.model_validate({"traffic_interval_seconds": 59})
    assert AsusWrtDriver.options_model.model_validate({}).traffic_interval_seconds == 3600


async def test_nothing_is_read_during_a_speed_test():
    clock = Clock(T0 + 10 * 60)
    device, fake = make(clock)
    running = {"on": True}
    device.driver.pause_reads = lambda: running["on"]
    await run_polls(device, clock, 120)
    assert [p for p in fake.paths() if p.endswith(".asp")] == []
    running["on"] = False
    await device.driver.read_wan_counters()  # a test reading the counters: the pages wait SPEED_TEST_QUIET_SECONDS
    clock.now += 10
    await device.driver.poll()
    assert [p for p in fake.paths() if p.endswith(".asp")] == []
    clock.now += stats.SPEED_TEST_QUIET_SECONDS
    await run_polls(device, clock, 60)
    assert fake.count("getWanTraffic.asp") > 0


async def test_the_registry_hands_the_speed_test_flag_to_the_driver():
    from pengumon.devices.registry import DeviceRegistry
    device, _ = make(Clock(T0))
    registry = DeviceRegistry([device])
    assert device.driver.pause_reads() is False
    registry.speed_test_running = lambda: True
    assert device.driver.pause_reads() is True


# --- the status pages and the uptime --------------------------------------------------------------------------------

async def test_uptime_link_and_temperature_come_with_the_client_list_not_every_poll():
    clock = Clock(T0 + 10 * 60)
    device, fake = make(clock, traffic_usage=False)
    results = await run_polls(device, clock, 120)
    systems = [r.readings[Capability.ROUTER_SYSTEM] for r in results]
    assert (systems[0].uptime_s, systems[0].wan_link_mbps, systems[0].cpu_temp_c) == (UPTIME_S, 2500, 64.1)
    assert all(s.uptime_s is None and s.wan_link_mbps is None for s in systems[1:6])
    assert systems[6].uptime_s == UPTIME_S  # the next client-list read, a minute later
    assert fake.count("ajax_ethernet_ports.asp") == 2 and fake.count("ajax_coretmp.asp") == 2
    # a page is given up after PAGE_TIMEOUT_SECONDS (the polls' own request keeps the client's timeout)
    assert fake.timeouts["ajax_coretmp.asp"] == stats.PAGE_TIMEOUT_SECONDS == 2.0 and fake.timeouts["appGet.cgi"] == 5.0


async def test_a_status_page_the_router_lacks_is_a_quiet_none_asked_again_a_day_later(caplog):
    clock = Clock(T0)
    fake = FakeAsus(Clock(T0), pages={"ajax_coretmp.asp": None})
    fake.clock = clock
    device, _ = make(clock, fake, traffic_usage=False)
    results = await run_polls(device, clock, 600)
    assert all(r.readings[Capability.ROUTER_SYSTEM].cpu_temp_c is None for r in results)
    assert not any(Capability.ROUTER_SYSTEM in r.missing for r in results)
    assert fake.count("ajax_coretmp.asp") == 1 and fake.count("ajax_ethernet_ports.asp") == 10
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


# --- the request budget ---------------------------------------------------------------------------------------------

async def budget(interval: float, changing):
    """Requests per hour of steady polling (10 s polls, client list every 60 s): the hour after the first round."""
    clock = Clock(T0 + 30 * 60)
    fake = FakeAsus(clock, changing=changing)
    device, _ = make(clock, fake, traffic_interval_seconds=interval)
    await run_polls(device, clock, 30 * 60)  # the first round and its backfill are done
    fake.requests.clear()
    await run_polls(device, clock, HOUR)
    paths = fake.paths()
    return {"appGet": paths.count("appGet.cgi"), "status pages": sum(p.startswith("ajax_") for p in paths),
            "traffic": sum(p.startswith("get") for p in paths)}


async def test_the_request_budget_per_hour(capsys):
    """Measured with the fixtures (8 clients, 7 with traffic). Printed with -s for the docs."""
    quiet = await budget(3600, ())
    busy = await budget(3600, tuple(MACS))
    minute = await budget(60, tuple(MACS))
    with capsys.disabled():
        print(f"\nper hour: default, no client changed {quiet}; default, all changed {busy}; 60 s, all changed {minute}")
    assert quiet == {"appGet": 360, "status pages": 120, "traffic": 3}
    assert busy == {"appGet": 360, "status pages": 120, "traffic": 3 + 2 * 7}
    # at 60 s: one request a minute; when the hour turns, the totals and the apps twice (the router's newest slot is
    # empty for a minute, then published), and every changed client once
    assert minute == {"appGet": 360, "status pages": 120, "traffic": 60 + 2 + 2 + 2 * 7}


# --- hostile answers (0.27 security review) -------------------------------------------------------------------------
# A router (or whatever answers at its address) can send anything: each answer below must cost little time, never raise
# out of the parse or the poll, and the parts that are too large or too many are left out.

MIB = stats.PAGE_MAX_BYTES


def timed(fn, *args):
    started = time.perf_counter()
    value = fn(*args)
    return value, time.perf_counter() - started


def filled(unit: str, head: str = "") -> str:
    """`head`, then `unit` again and again up to PAGE_MAX_BYTES."""
    return head + unit * ((MIB - len(head)) // len(unit))


HOSTILE = [
    ("ports: an object opened again and again", stats.wan_link_mbps, filled("get_wan_lan_status = {")),
    ("ports: the assignment and spaces", stats.wan_link_mbps, filled(" ", "get_wan_lan_status")),
    ("statistics: the assignment again and again", stats.parse_statistics, filled("array_statistics = ")),
    ("statistics: blank lines", stats.parse_statistics, filled(" \n")),
    ("statistics: an assignment and spaces", stats.parse_statistics, filled(" ", "array_statistics = ") + "x"),
    ("statistics: lines of spaces", stats.parse_statistics, filled(" " * 1000 + "x\n")),
    ("temperature: a long number", stats.cpu_temp_c, filled("1", "curr_cpuTemp = ")),
    ("temperature: the assignment again and again", stats.cpu_temp_c, filled("curr_cpuTemp = ")),
    ("uptime: the hook opened again and again", stats.uptime_seconds, filled('"uptime":"')),
    ("uptime: brackets and digits", stats.uptime_seconds, filled("( 1", '{"uptime":"')),
    ("uptime: a long number", stats.uptime_seconds, filled("1", '{"uptime":"(') + ' secs since boot)"}'),
]


@pytest.mark.parametrize("name, fn, body", HOSTILE, ids=[h[0] for h in HOSTILE])
def test_a_page_as_large_as_the_cap_parses_in_well_under_a_second(name, fn, body):
    """Before 0.27's fix, `get_wan_lan_status\\s*=\\s*(\\{.*\\})` backtracked over the rest of the body at each `{`:
    176 KB took 0.6 s, a 1 MiB page about 20 s, with the event loop frozen. Every pattern now runs in linear time."""
    value, seconds = timed(fn, body)
    assert value is None and seconds < 1.0, (name, seconds)


def test_deeply_nested_json_is_no_data_not_an_error():
    """Deep nesting raises RecursionError (not a ValueError) in the JSON decoder: caught like any bad page."""
    deep = "[" * 200_000 + "]" * 200_000
    assert stats.parse_statistics(f"array_statistics = {deep};") is None
    assert stats.wan_link_mbps('get_wan_lan_status = {"portSpeed": ' + deep + "};") is None
    assert stats.wan_link_mbps("get_wan_lan_status = " + '{"a":' * 200_000) is None


async def test_a_deeply_nested_page_costs_the_round_never_the_poll():
    """TrafficRounds.step never raises for the router's answers, and the status pages give None."""
    clock = Clock(T0 + 10 * 60)
    deep = "[" * 200_000 + "]" * 200_000
    fake = FakeAsus(clock, pages={"getWanTraffic.asp": f"array_statistics = {deep};",
                                  "ajax_ethernet_ports.asp": 'get_wan_lan_status = {"portSpeed": ' + deep + "};"})
    device, _ = make(clock, fake)
    results = await run_polls(device, clock, 120)
    assert all(Capability.WAN_COUNTERS in r.readings for r in results) and not traffic_of(results)
    assert results[0].readings[Capability.ROUTER_SYSTEM].wan_link_mbps is None
    assert fake.count("getWanTraffic.asp") == 1  # the round stopped; asked again RETRY_SECONDS later


def test_the_ports_object_is_read_where_it_starts_whatever_follows():
    page = fixture("ajax_ethernet_ports.asp") + '\nvar other = {"x": 1};\n'  # a second object after it
    assert stats.wan_link_mbps(page) == 2500


def test_rows_are_capped_so_a_round_always_ends():
    hours = stats.hour_series([[1, 2]] * 1000, T0, "")
    assert len(hours) == stats.MAX_HOUR_SLOTS == 48 and hours[-1].hour == T0 - HOUR  # the newest slots are kept
    apps = stats.app_rows([[f"app {i}", 1, 2] for i in range(5000)])
    assert len(apps) == stats.MAX_ROWS == 1024 and apps[0][0] == "app 0"
    totals = stats.client_totals([[f"02:00:00:00:{i // 256:02x}:{i % 256:02x}", 1, 2] for i in range(5000)])
    assert len(totals) == stats.MAX_ROWS


def test_an_uptime_must_be_one():
    hook = '{"uptime":"Tue, 10 Nov 2026 13:10:00 -0500(%s secs since boot)"}'
    assert stats.uptime_seconds(hook % "0") == 0
    assert stats.uptime_seconds(hook % MAX_UPTIME_SECONDS) == MAX_UPTIME_SECONDS
    assert stats.uptime_seconds(hook % (MAX_UPTIME_SECONDS + 1)) is None  # over 20 years
    assert stats.uptime_seconds(hook % ("9" * 11)) is None
    assert stats.uptime_seconds(hook % ("9" * 5000)) is None  # int() refuses over 4300 digits: no ValueError


async def test_an_absurd_uptime_is_left_out_and_the_poll_still_works():
    clock = Clock(T0 + 10 * 60)
    device, _ = make(clock, traffic_usage=False)
    old = APPGET["uptime()"]
    APPGET["uptime()"] = {"uptime": "Tue, 10 Nov 2026 13:10:00 -0500(" + "9" * 5000 + " secs since boot)"}
    try:
        result = await device.driver.poll()
    finally:
        APPGET["uptime()"] = old
    system = result.readings[Capability.ROUTER_SYSTEM]
    assert system.uptime_s is None and system.wan_link_mbps == 2500


def streamed(chunks: int, pulled: list, chunk: bytes = b"x" * 65536) -> httpx.Response:
    """A 200 whose body is `chunks` chunks of 64 KiB, counting in `pulled` how many were taken from it."""
    async def body():
        for _ in range(chunks):
            pulled.append(1)
            yield chunk
    return httpx.Response(200, content=body(), headers={"content-type": "application/json"})


async def test_a_page_longer_than_the_cap_is_not_read_past_it_and_says_so_once(caplog):
    clock = Clock(T0 + 10 * 60)
    fake = FakeAsus(clock)
    pulled = {"getWanTraffic.asp": [], "ajax_ethernet_ports.asp": []}

    def router(request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path in pulled:
            fake.requests.append((request.method, path, dict(request.url.params)))
            return streamed(1024, pulled[path])  # 64 MiB
        return fake(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(router))
    device = build_router(username="u", password="p", client=client, clock=clock, wall_clock=clock, options={})
    device.driver._token = "tok"
    with caplog.at_level(logging.INFO, logger="pengumon.drivers.asuswrt"):
        results = await run_polls(device, clock, 3 * HOUR)
    reasons = [r.missing[Capability.TRAFFIC_USAGE] for r in results if Capability.TRAFFIC_USAGE in r.missing]
    assert reasons == [stats.TOO_BIG] and "1 MiB" in stats.TOO_BIG  # said once, though every round finds it so
    assert fake.count("getWanTraffic.asp") == 4 and fake.count("getAppTraffic.asp") == 0  # once a round, no further
    assert not traffic_of(results)
    assert all(r.readings[Capability.ROUTER_SYSTEM].wan_link_mbps is None for r in results)
    assert sum("ajax_ethernet_ports.asp with more than" in r.getMessage() for r in caplog.records) == 1
    # each read stopped at the cap: 16 chunks of 64 KiB and the one that went past it
    reads = fake.count("getWanTraffic.asp") + fake.count("ajax_ethernet_ports.asp")
    assert len(pulled["getWanTraffic.asp"]) + len(pulled["ajax_ethernet_ports.asp"]) <= reads * (MIB // 65536 + 1)
