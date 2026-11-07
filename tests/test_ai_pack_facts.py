"""The AI pack's core facts (reports/ai_findings.py): the rules on a small seeded week, and the worst case of design 4.3
(test 5), where nothing may grow with the data beyond its cap, rendered with every module under the hard cap and
trimmed in the design's order above it. The wording and the redaction are the golden's and the redaction tests."""

import sqlite3
import time
from types import SimpleNamespace

import pytest

from pengumon.db.repository import DatabaseRepository
from pengumon.reports.ai_findings import (
    RULES, Baseline, Shape, collect_pack_facts, confidence, hop_window_vs_normal, latency_start, match_events,
    path_shape,
)
from pengumon.reports.queries import MinuteRow, minute_row_chunks
from pengumon.reports import ai_pack
from pengumon.reports.ai_pack import ModuleInput, render_pack, view_of
from pengumon.capabilities.contrib import ModuleSection
from pengumon.reports.contributions import activate, driver_note
from pengumon.config import AppConfig
from datetime import timezone

from ai_pack_seed import DAY, add_incident, make_config, seed, week_end

BURST = SimpleNamespace(code="line.error_burst", phrase="codewords +{value:,.0f}", supports=frozenset({"line"}),
                        reach_seconds=1800.0)


def event(code, at, value=None, subject=""):
    return SimpleNamespace(code=code, start=at, end=at, subject=subject, value=value)


async def open_repo(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "pack.db"))
    await repo.initialize()
    return repo


async def test_the_rules_on_a_seeded_week(tmp_path):
    repo, config, end = await open_repo(tmp_path), make_config(), week_end()
    slow_from = end - 3 * 3600
    ids = await seed(repo, config, end, slow=(("isp1", slow_from, slow_from + 30 * 60, 5),
                                              ("router", slow_from, slow_from + 30 * 60, 3),    # 3 x 1.5 ms: under 10 ms more
                                              ("net1", slow_from, slow_from + 30 * 60, 1.8)),   # 18 ms + 14: under 2 x
                     gaps=((end - 5 * 3600, end - 5 * 3600 + 600),))
    a = await add_incident(repo, config, ids, end - 2 * 3600, 30, "line")                          # carried, burst near
    b = await add_incident(repo, config, ids, end - 4 * 3600, 20, "isp1", carried=False, kind="isp_loss",
                           cause_code="internet_backbone_transit_loss")                                # one hop only
    c = await add_incident(repo, config, ids, end - 6 * 3600, 40, "line")                           # carried, nothing near
    d = await add_incident(repo, config, ids, end - 6 * DAY, 30, "line", raw=False)                 # samples gone
    g = await add_incident(repo, config, ids, end - 7 * 3600, 0, "line", carried=False, silent=("isp2",))  # no loss; isp2 ignores ping
    e = await add_incident(repo, config, ids, end - 1 * 3600, 25, "isp1", carried=False, kind="isp_loss", expected=True,
                           cause_code="internet_backbone_transit_loss")
    app = await add_incident(repo, config, ids, end - 8 * 3600, 0, "net1", carried=False, kind="service_down",
                             cause_code="service_unavailable")      # a host app's, by its cause alone
    with sqlite3.connect(repo.db_path) as con:   # stored without a loss span, as an incident on latency is
        con.execute("UPDATE incidents SET loss_seconds = NULL, loss_start = NULL WHERE id = ?", (g,))

    f = await collect_pack_facts(repo, config, start=end - 7 * DAY, end=end, now=end, events=[event("line.error_burst", end - 2 * 3600 - 900, 312),   # 15 min before a: inside its reach
                                         event("line.error_burst", end - 5 * 3600, 90)],            # an hour after c: outside it
                                 event_kinds=[BURST])
    by = {i.id: i for i in f.incidents}

    # path shape, confidence, ranking
    # loss from the gateway's home-side address on (a gateway verdict): a codeword burst on the line coincides but does
    # not support it, since those probes never use the line; the same burst supports a line verdict
    assert (by[a].cause, by[a].shape.kind, by[a].shape.first_hop, by[a].confidence, by[a].confidence_reason) == (
        "gateway", "carried", "line", "medium", "uncorroborated")
    assert [(m.code, m.value, m.corroborates) for m in by[a].events] == [("line.error_burst", 312, False)]
    burst = [event("line.error_burst", by[a].start, 312)]
    for cause, agrees in (("line", True), ("gateway", False)):
        matched = match_events((by[a].start, by[a].start + 30), burst, {BURST.code: BURST}, cause, [by[a].start], 0.0,
                               BURST.reach_seconds, {})
        assert [m.corroborates for m in matched] == [agrees], cause
    assert confidence(resolution="1s", expected=False, shape=Shape("carried", "isp1", 30.0), first_category="isp",
                      cause="line", corroborated=True, kind="isp_loss") == ("high", "corroborated")
    assert (by[b].shape.kind, by[b].shape.first_hop, by[b].confidence_reason) == ("one_hop", "isp1", "one_hop")
    assert (by[c].shape.kind, by[c].confidence, by[c].events) == ("carried", "medium", [])
    assert (by[d].resolution, by[d].confidence, by[d].confidence_reason) == ("minute", "low", "minute_data")
    assert (by[g].shape.kind, by[g].shape.first_hop) == ("none", None)   # the silent hop is not the failing one
    assert by[e].confidence_reason == "expected"
    assert [i.id for i in sorted(f.incidents, key=lambda i: i.rank)] == [c, d, a, b, g, e]   # by seconds lost x shape weight (d has no loss span: its 33 s); the expected one last
    assert by[a].lost_for == 30 and f.patterns.count == 5 and f.expected_count == 1
    assert app not in by and f.service_count == 1 and by[g].lost_for == 0   # no probe lost on the path itself
    # 8 % lost at the Internet is real loss, whatever the Line before it lost: carried on, not "Line only"
    path = [("line", "gateway"), ("net1", "public")]
    assert path_shape(path, {"line": (100, 70), "net1": (100, 92)}, (), 0.5, 15.0).kind == "carried"
    assert path_shape(path, {"line": (100, 70), "net1": (100, 99)}, (), 0.5, 15.0).kind == "one_hop"
    # a verdict about lookups or latency agrees with "no hop isolated"; one about loss does not
    clean = Shape("none", None, 0.0)
    assert confidence(resolution="1s", expected=False, shape=clean, first_category=None, cause="dns",
                      corroborated=False, kind="dns_outage") == ("medium", "uncorroborated")
    # latency only: it rises where the first hop's peak is over the slow rule, and under a minute it is low
    walk = [("router", "local"), ("line", "gateway"), ("isp1", "isp"), ("net1", "public")]
    rise, before = latency_start(walk, {"router": 2.0, "line": 3.0, "isp1": 60.0, "net1": 70.0},
                                 {"router": 1.0, "line": 1.5, "isp1": 10.0, "net1": 12.0}, ())
    assert (rise.kind, rise.first_hop, before) == ("carried", "isp1", ["router", "line"])
    assert [confidence(resolution="1s", expected=False, shape=clean, first_category=None, cause="isp", corroborated=False,
                       kind="upstream_latency", duration=s, latency=rise, latency_category="isp")
            for s in (120, 9)] == [("medium", "uncorroborated"), ("low", "brief")]
    assert confidence(resolution="1s", expected=False, shape=clean, first_category=None, cause="gateway",
                      corroborated=False, kind="gateway_loss") == ("low", "no_hop")
    # a 1.9 ms hop at 2.4 ms is 1.26 x its normal but not slower: the rule needs 5 ms more too
    base = Baseline(1.9, 2.5, 2.2, 0.2, 0.0, 10080, False)
    assert hop_window_vs_normal([MinuteRow(m, "router", 60, 60, 2.4, 2.6, 0.2) for m in range(60)], base, 0.5).notable == []

    # a hop against its own normal, and a stretch with no probe at all
    day = await collect_pack_facts(repo, config, start=end - DAY, end=end, now=end)
    hop = {h.target_id: h for h in day.hops}
    assert 10 <= hop["isp1"].baseline.normal_ms <= 12 and hop["isp1"].window.slow_minutes == 30
    assert hop["isp1"].window.notable == ["slow_minutes"] and hop["router"].window.slow_minutes == hop["net1"].window.slow_minutes == 0 and hop["router"].window.notable == []
    assert {h.target_id: h for h in f.hops}["isp1"].window.notable == []   # 30 slow minutes in 7 days are not notable
    assert f.no_data == [(end - 5 * 3600, end - 5 * 3600 + 600)]

    # get_hop_stats(start, end_time) stops at end_time (the pack's windows end at their own end)
    sent = {h["target_id"]: h["sent"] for h in await repo.get_hop_stats(end - 7 * DAY, end - 6 * DAY)}
    assert sent["router"] == 1441 * 60 < {h["target_id"]: h["sent"] for h in await repo.get_hop_stats(end - 7 * DAY)}["router"]

    # the incident scope: that incident first, 10-second rows
    one = await collect_pack_facts(repo, config, start=by[b].start - 3600, end=by[b].start + 3600, now=end, scope="incident",
                                 incident_id=b)
    assert [cu.incident_id for cu in one.closeups] == [b] and one.closeups[0].rows_10s
    await repo.close()


async def test_nothing_grows_with_the_data_beyond_its_cap(tmp_path):
    """Design 4.3, the bad week: 300 incidents on 15 hops. The table, the close-ups and their rows stay at their caps,
    and the 7-day build stays quick (the target is about 1 s; the bound here only catches a quadratic step)."""
    repo, config, end = await open_repo(tmp_path), make_config(isp=8, public=3, dns=2), week_end()
    ids = await seed(repo, config, end)
    for k in range(300):
        await add_incident(repo, config, ids, end - 600 - k * 1900, 1800 if k == 7 else 5 + k % 60, "line" if k % 3 else "isp2", carried=k % 4 != 0)
    started = time.perf_counter()
    f = await collect_pack_facts(repo, config, start=end - 7 * DAY, end=end, now=end, detail="detailed")
    took = time.perf_counter() - started
    assert len(f.incidents) == 300 and len(f.hops) == 15
    assert len(f.listed) == RULES.incident_rows["detailed"] and sum(n for _, n in f.more) == 300 - len(f.listed)
    assert len(f.closeups) == RULES.closeups["detailed"]
    assert all(len(c.minutes) <= RULES.closeup_max_rows and len(c.columns) <= RULES.closeup_columns for c in f.closeups)
    assert any(c.minutes_cut for c in f.closeups)   # the 30-minute one has more minutes than a close-up shows
    assert sum(1 for i in f.incidents if i.hops is not None) == len(f.closeups)
    print(f"7-day build: {took:.2f} s")
    assert took < 5.0, f"7-day build took {took:.1f} s"
    await repo.close()

    # rendered with every module at its worst (more than its budget holds), the pack stays under the hard cap
    view = view_of(f, {t.id: t for t in list(config.targets) + list(config.dns_probes)}, worst_modules(),
                   "Example-ISP", timezone.utc, "Past 7 days", "0.22.0")
    pack = render_pack(view, "full", "detailed")
    print(f"worst case rendered: {pack.chars:,} characters, trimmed {pack.trimmed}")
    assert pack.chars <= ai_pack.HARD_CAP_CHARS and not any("still over" in t for t in pack.trimmed), pack.trimmed
    assert all(m["chars"] <= m["budget_chars"] for m in pack.modules), pack.modules
    # over the hard cap, it gives way in the design's order (4.1), and never a module's prompt text
    for cap, gone in ((pack.chars - 1, ["incident rows cut to 10"]),
                      (12_000, ["incident rows cut to 10", "close-ups cut to 1", "module tables dropped",
                                "still over the hard cap"])):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ai_pack, "HARD_CAP_CHARS", cap)
            small = render_pack(view, "full", "detailed")
        assert [t for t in small.trimmed if ": " not in t] == gone
        assert all(m.prompt_context.splitlines()[0] in small.markdown for m in view.modules)
    assert "## Incidents (the 10 most significant of 300" in small.markdown
    assert small.markdown.count("### Incident ") == 1


def worst_modules():
    """Every built-in contribution with its prompt text and driver note, and a section far larger than its budget."""
    devices = [{"id": "modem", "driver": "xfinity_xb", "url": "http://modem.example", "poll_interval_seconds": 600},
               {"id": "mesh-router", "driver": "asuswrt", "url": "http://router.example"}]
    config = AppConfig(devices=devices, line={"down_mbps": 300, "up_mbps": 20})
    act = activate(config)
    notes = {cfg.driver: (driver_note(cfg, n, "full", mesh=True), driver_note(cfg, n, "anonymized", mesh=True))
             for cfg, n in act.notes}
    note_of = {"docsis": notes["xfinity_xb"], "router_system": notes["asuswrt"]}
    out = []
    for c in act.contributions:
        section = ModuleSection("x" * 200, [f"Finding {n}: " + "y" * 140 for n in range(12)],
                                (["A", "B", "C", "D", "E"], [[f"row {n}", "z" * 30, "1", "2", "3"] for n in range(40)]),
                                {"Column": {}})
        out.append(ModuleInput(c.id, c.title, c.layer, c.budget_chars, c.prompt_context(config), section,
                               note_of.get(c.id)))
    return tuple(out)


async def test_the_minute_rows_come_in_chunks_without_a_gap_or_a_repeat(tmp_path):
    repo, config, end = await open_repo(tmp_path), make_config(), week_end()
    await seed(repo, config, end)
    whole = [r for ch in await _chunks(repo, end, 10**6) for r in ch]
    pieces = await _chunks(repo, end, 1000)
    assert len(pieces) == 11 and len(whole) > 10_000   # 10,081 minutes in 1000-minute chunks
    assert [r for ch in pieces for r in ch] == whole   # same rows, same (minute, target) order


async def _chunks(repo, end, chunk_minutes):
    return [ch async for ch in minute_row_chunks(repo, end - 7 * DAY, end, chunk_minutes)]


def test_with_no_incident_the_events_of_a_timed_kind_say_when_in_the_day():
    """A mesh node's falls to 0 clients in a week with no incident: "none of 6 during an incident" says nothing, when
    in the day they fell ties them to the user's evenings. A kind not so marked keeps the incident wording."""
    from pengumon.capabilities.contrib import EventKind
    from pengumon.reports.ai_findings import LocalClock, event_summaries
    from pengumon.reports.ai_pack import module_findings
    kinds = {"mesh.node_empty": EventKind("mesh.node_empty", "{subject} at 0 clients", frozenset({"router"}), 120.0,
                                          "mesh nodes falling to 0 clients", by_time_of_day=True),
             "line.errors": EventKind("line.errors", "codewords +{value:,.0f}", frozenset(), 1.0, "smaller counts")}
    midnight = 1_700_006_400.0          # a UTC midnight
    events = [event("mesh.node_empty", midnight + h * 3600, subject="mesh_node:3") for h in (19, 20, 20.5, 21, 22, 3)]
    events.append(event("line.errors", midnight + 3600, 2.0))
    summaries = event_summaries(events, kinds, {c: "module" for c in kinds}, [], midnight - DAY, midnight + DAY,
                                LocalClock(timezone.utc), 6)
    assert module_findings(SimpleNamespace(event_summaries=summaries, incidents=[]), list(kinds)) == [
        "Mesh nodes falling to 0 clients: 5 of 6 between 18:00 and 00:00, 1 between 00:00 and 06:00.",
        "Smaller counts: not during an incident."]

