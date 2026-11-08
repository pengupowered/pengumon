"""Golden packs for the two example setups (design 2.5): wording, rules, ranking, event matching, the no-device
paragraph. A goes the whole way through build_ai_pack on a seeded store (the findings' wording and rules); B renders
hand-made facts (the renderer and the redactor)."""

import dataclasses
import time
from datetime import timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pengumon
from ai_pack_helpers import canary_facts
from ai_pack_seed import add_incident, seed
from golden_files import check_golden
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import DeviceSample, MeshCount
from pengumon.lines import docsis
from pengumon.lines.docsis_model import DocsisChannel
from pengumon.reports import ai_pack
from pengumon.reports.ai_findings import LocalClock
from pengumon.reports.ai_pack import render_pack
from pengumon.reports.contributions import build_ai_pack
from pengumon.timeofday import viewer_zone
from pengumon.settings.document import SettingsDocument

END = 1_793_000_040.0       # a fixed clock (a whole minute), so the golden does not move with the day
HOUR = 3600.0


def setup_a_config() -> AppConfig:
    """Example A: a cable gateway read every 10 minutes and a mesh router read every minute, on the standard path, with
    the household's zone set in Settings."""
    return AppConfig(general={"timezone": "America/New_York"},
        targets=[{"id": "router", "name": "Example-Mesh main", "category": "local", "address": "192.168.88.1"},
                 {"id": "line", "name": "Example gateway", "category": "gateway", "address": "192.168.100.1"},
                 {"id": "isp1", "name": "po-1.edge.example-isp.net", "category": "isp", "address": "198.51.100.1"},
                 {"id": "isp2", "name": "po-2.edge.example-isp.net", "category": "isp", "address": "198.51.100.2"},
                 {"id": "net1", "name": "Public resolver 1", "category": "public", "address": "192.0.2.1"}],
        dns_probes=[{"id": "dns1", "name": "Lookup 1", "domain": "example.com", "nameserver": "192.0.2.53"}],
        devices=[{"id": "modem", "driver": "xfinity_xb", "url": "http://modem.example", "poll_interval_seconds": 600},
                 {"id": "mesh-router", "driver": "asuswrt", "url": "http://router.example", "poll_interval_seconds": 60,
                  "options": {"mesh_node_labels": {"0000a1": "Example-Mesh main", "0000a2": "Example-Mesh hall"}}}],
        line={"down_mbps": 1000, "up_mbps": 50, "isp_name": "Example-ISP"})


async def _at(ts: float, step):
    """Awaits step() with the clock at ts (what the repository stamps a settings version with)."""
    real, time.time = time.time, lambda: ts
    try:
        return await step()
    finally:
        time.time = real


async def seed_setup_a(repo, config, end: float) -> None:
    """The day before `end`: a Line incident carried on, with a codeword burst in the reading after it, a mesh node
    at 0 clients, a settings save 5 minutes before it and another 20 minutes after it (which does not count for it);
    an incident at ISP hop 1 only, 20 minutes after a save (stale: it does not count) and just after a reading with a
    burst (which, read before it, does not count for it) and with a smaller count in the reading after it (which
    does); a busy-line incident (expected). At the last modem reading a downstream channel's SNR and an upstream
    channel's power sit at their healthy limits, the SNR lower earlier in the day."""
    ids = await seed(repo, config, end)
    line_at, isp_at, busy_at = end - 3 * HOUR, end - 6 * HOUR, end - 9 * HOUR
    await add_incident(repo, config, ids, line_at, 30, "line")
    await add_incident(repo, config, ids, isp_at, 20, "isp1", carried=False, kind="isp_loss",
                       cause_code="internet_backbone_transit_loss")
    await add_incident(repo, config, ids, busy_at, 0, "net1", carried=False, kind="busy_latency", expected=True,
                       cause_code="load_induced_latency")
    await repo.initialize_settings(SettingsDocument(), "import", [])
    await _at(isp_at - 1200, lambda: repo.save_settings(1, {"classifier.busy_load_pct": 60}, {}, "ui",
                                                         lambda old, new: "busy level"))
    await _at(line_at - 300, lambda: repo.save_settings(2, {"classifier.busy_load_pct": 62}, {}, "ui",
                                                         lambda old, new: "busy level"))
    await _at(line_at + 1200, lambda: repo.save_settings(3, {"classifier.busy_load_pct": 65}, {}, "ui",
                                                          lambda old, new: "busy level"))
    # the modem: every 10 minutes, 2,310 new uncorrectable codewords in the reading after the Line incident
    total = 1000
    for k in range(145):
        ts = end - 24 * HOUR + 600 * k
        recent = (2310 if line_at < ts <= line_at + 600 else 940 if ts == isp_at - 600
                  else 120 if isp_at < ts <= isp_at + 600 else 0)
        total += recent
        limit = {144: (32.96, 49.04), 100: (32.4, 44.0)}.get(k, (38.0, 44.0))   # shown 33.0 dB and 49.0 dBmV
        ds = [DocsisChannel(channel_id=i, direction="downstream", frequency_mhz=500.0 + 6 * i, power_dbmv=1.0,
                            snr_db=limit[0] if i == 1 else 38.0, uncorrectables=0, uncorrectables_recent=recent // 32)
              for i in range(1, 33)]
        us = [DocsisChannel(channel_id=100 + i, direction="upstream", frequency_mhz=30.0 + 6 * i,
                            power_dbmv=limit[1] if i == 0 else 44.0) for i in range(4)]
        snapshot = docsis.summarize(ds + us, ts)
        snapshot.uncorrectables_total, snapshot.uncorrectables_recent = total, recent
        await repo.insert_line_sample(docsis.line_sample("modem", snapshot))
    # the router: every minute; the line busy around the busy incident; the hall node empty during the Line one
    for k in range(24 * 60 + 1):
        ts = end - 24 * HOUR + 60 * k
        busy = busy_at - 120 <= ts <= busy_at + 120
        await repo.insert_device_sample(DeviceSample(
            device_id="mesh-router", ts=ts, status="healthy", wan_rx_bytes=k * 40_000_000, wan_tx_bytes=k * 4_000_000,
            wan_rx_mbps=900.0 if busy else 5.0, wan_tx_mbps=1.0, cpu_pct=6.0, mem_pct=41.0, clients_online=14,
            clients_wired=2, clients_wifi_2g=3, clients_wifi_5g=9, clients_wifi_other=0))
        hall = 0 if line_at - 60 <= ts <= line_at + 120 else 5
        await repo.insert_mesh_counts([MeshCount("mesh-router", ts, "0000a1", 9), MeshCount("mesh-router", ts, "0000a2", hall)])


async def setup_a_pack(tmp_path) -> dict:
    repo = DatabaseRepository(db_path=str(tmp_path / "setup_a.db"))
    real_time, real_version = time.time, pengumon.__version__
    time.time = lambda: END + 30           # the clock the app reads: the seeded minutes end at END
    pengumon.__version__ = "0.22.0"        # the header names the version: the golden does not follow the bumps
    try:
        await repo.initialize()
        config = setup_a_config()
        await seed_setup_a(repo, config, END)
        return await build_ai_pack(repo, config, scope="window", hours=24, incident_id=None, level="full",
                                   detail="short", devices=tuple(config.devices), live=None, tz_offset_minutes=-300)
    finally:
        time.time, pengumon.__version__ = real_time, real_version
        await repo.close()


async def scenarios(tmp_path):
    # A: modem + router + mesh, 24 h, Full, through build_ai_pack
    a = await setup_a_pack(tmp_path)
    # B: no device, one incident (with the 10-second rows), Vague
    b = render_pack(canary_facts(week=True, with_modules=False, scope_kind="incident"), "vague", detail="detailed")
    return {
        "setup_a": {k: a[k] for k in ("markdown", "modules", "trimmed", "roles", "redactions")},
        "setup_b": {"markdown": b.markdown, "modules": b.modules, "trimmed": b.trimmed, "roles": b.roles,
                    "swept": b.swept, "hits": b.hits},
    }


async def test_the_golden_packs_do_not_change(tmp_path):
    packs = await scenarios(tmp_path)
    check_golden("ai_pack", packs)
    a, b = packs["setup_a"]["markdown"], packs["setup_b"]["markdown"]
    # loss starting at the Gateway is the gateway or its cable, not the line, and with a modem restart the unit: the
    # fixed text and the DOCSIS text
    assert "*gateway* = loss starts at the Gateway while the Router answers: the gateway or the link to it (a modem " \
           "restart then: the unit), not the line" in a and "strong evidence" not in a
    assert ("Loss starting at the Gateway is the modem unit or the cable from the router to it, not the coaxial line; a "
            "modem restart at that time means the unit.") in a and "(unless it restarted)" not in a
    # the event log only where it can tell something apart, and never a restart the Gateway's pings rule out
    assert ("A Gateway that answered every probe during the loss did not restart: do not ask about one." in a
            and "with no codeword burst or signal fault then" in a)
    # the monitoring computer's own link: ruled out by a clean Router hop (A), worth asking about otherwise (B)
    assert ai_pack.LINK_CLEAN_TEXT in a and "worth asking the user" in b and ai_pack.LINK_CLEAN_TEXT not in b
    # ... slow minutes alone are enough to ask (B's Router hop without its lossy minutes); a hop that is a DNS server,
    # and a lookup answered before the first provider router, are marked in the path
    facts = canary_facts()
    hops = list(facts.hops)
    hops[0], hops[2] = dataclasses.replace(hops[0], lossy_minutes=0), dataclasses.replace(hops[2], dns_server=True)
    hops[4] = dataclasses.replace(hops[4], normal_ms=1.0)
    md = render_pack(dataclasses.replace(facts, hops=tuple(hops)), "vague").markdown
    assert "The Router hop had 12 slow minutes in this window" in md
    assert "| ISP hop 1 | provider DNS server |" in md and "answered in the home (faster than ISP hop 1)" in md
    # the cable line's restarts and readings it did not answer are in its Findings line, zeros too
    assert "- Cable line: 3,370 new uncorrectable codewords in 3 of 144 readings; 0 modem restarts; answered every " \
           "reading." in a
    # new uncorrectables coincide with an incident inside the interval they were counted in, a count below a burst too
    assert "+120 counted between 21:34 and 21:44, with incident #2 (21:34:00) in that interval" in a
    assert "| low (one hop only) | uncorrectable codewords +120 |" in a
    # a channel shown at a healthy limit (SNR or power) is worth watching, not low; with the window's worst value
    assert ("2 channels at a healthy limit at the last reading, worth watching, not a fault: DS 506 MHz, SNR 33.0 dB "
            "(lowest in the window 32.4 dB), no new uncorrectables; US 30 MHz, power 49.0 dBmV (highest in the window "
            "49.0 dBmV).") in a and "outside typical levels" not in a


def test_times_are_in_the_households_zone_across_a_clock_change():
    """Settings' zone first, then the browser's, then the offset, else UTC, said so; a named zone's daylight-saving
    change is named in the header and next to an incident just after it, also as "Day n"; a time in the hour that
    happened twice says which pass it is in."""
    ny, change = ZoneInfo("America/New_York"), 1793512800.0      # 2026-11-01 06:00 UTC: 02:00 EDT -> 01:00 EST
    settings = lambda name: SimpleNamespace(general=SimpleNamespace(timezone=name))   # noqa: E731
    assert [viewer_zone(settings("Etc/UTC"), "America/New_York", -300)[1:], viewer_zone(settings("Etc/UTC"), None, -300)[1:],
            viewer_zone(settings("UTC"), None, 0)[1:], viewer_zone(settings("Europe/Berlin"), "America/New_York", 0)[1:]] == [
        ("named", "America/New_York"), ("offset", ""), ("utc", "UTC"), ("named", "Europe/Berlin")]
    assert [LocalClock(ny).hour(change + h * 3600) for h in (-6, 6)] == [20, 7]          # EDT before, EST after
    facts = canary_facts()
    facts = dataclasses.replace(facts, scope=dataclasses.replace(facts.scope, tz=ny, zone="America/New_York",
                                                                 zone_kind="named"),
                                incidents=(dataclasses.replace(facts.incidents[1], start=change - 1800),
                                           dataclasses.replace(facts.incidents[0], start=change + 29), facts.incidents[2]))
    full, anon = render_pack(facts, "full").markdown, render_pack(facts, "anonymized").markdown
    assert ("local time (America/New_York; clocks went back one hour on Sun 11-01 at 02:00, to 01:00, so 01:00 to 02:00 "
            "that night happened twice: times in it are marked EDT (first) or EST (second)).") in full
    assert "| Sun 01:30:00 EDT |" in full and "| Sun 01:00:29 EST |" in full and "starts 29 s after the clocks went back" in full
    assert "America/" not in anon and "clocks went back one hour on Day 5 at 02:00, to 01:00" in anon
    assert "| Day 5 01:30:00 (1st) |" in anon and "| Day 5 01:00:29 (2nd) |" in anon
    utc = dataclasses.replace(facts, scope=dataclasses.replace(facts.scope, tz=timezone.utc, zone="", zone_kind=""))
    assert ai_pack.UTC_TEXT in render_pack(utc, "vague").markdown


async def test_build_ai_pack_end_to_end_with_no_device_at_vague_in_the_incident_scope(tmp_path):
    """Golden scenario B's path: the stored facts (reports.ai_findings), no device, live=None, one incident, Vague."""
    import pytest
    pytest.importorskip("pengumon.reports.ai_findings", reason="needs workstream A1's facts")
    from ai_pack_seed import add_incident, make_config, seed, week_end
    from pengumon.db.repository import DatabaseRepository
    from pengumon.reports.contributions import build_ai_pack as build

    repo = DatabaseRepository(db_path=str(tmp_path / "pack.db"))
    await repo.initialize()
    config = make_config()
    config.devices = []
    end = week_end()
    ids = await seed(repo, config, end)
    iid = await add_incident(repo, config, ids, end - 7200, 30, "line")
    import time
    real_time = time.time
    time.time = lambda: end + 300        # the stored week is seeded relative to the clock the app reads
    try:
        out = await build(repo, config, scope="incident", hours=24, incident_id=iid, level="vague", detail="short",
                          devices=(), live=None)
    finally:
        time.time = real_time
        await repo.close()
    md = out["markdown"]
    assert out["modules"] == [] and "What Pengumon cannot see here" in md and "Incident " in md
    assert "10-second rows" in md and "Gateway, carried on" in md
    assert "Example-Mesh" not in md and "192.168.88" not in md and "Example gateway" not in md
    assert out["redactions"].keys().isdisjoint({k for k in out["redactions"] if k.startswith("scan:")})
    import json
    json.dumps(out, allow_nan=False)


def test_a_section_over_its_budget_keeps_its_first_table_rows_and_their_lead_in():
    # the cable line's codeword bullets once pushed out the one channel outside typical levels, and left
    # "1 channel outside typical levels at the last reading:" introducing nothing
    from ai_pack_helpers import Section
    facts = canary_facts(week=True)
    docsis_in = facts.modules[0]
    rows = [["DS", str(600 + 9 * n), "-1.0", f"{27 + n * 0.1:.1f}"] for n in range(5)]
    section = Section(headline="Cable line: bursts of uncorrectable codewords.",
                      findings=["Downstream SNR 25.0-38.6 dB.", "Upstream power 41.0-44.2 dBmV."]
                      + [f"Reading {n}: +{900 + n:,} new uncorrectable codewords, with an incident in that interval."
                         for n in range(12)]
                      + ["5 channels outside typical levels at the last reading:"],
                      table=(["Dir", "Freq MHz", "Power dBmV", "SNR dB"], rows))
    small = dataclasses.replace(docsis_in, budget_chars=1100, section=section)
    md = render_pack(dataclasses.replace(facts, modules=(small,) + facts.modules[1:]), "full").markdown
    part = md[md.index("### Cable line (DOCSIS)"):md.index("###", md.index("### Cable line (DOCSIS)") + 4)]
    assert "5 channels outside typical levels at the last reading (the first 3 of 5):" in part
    assert all(f"| {r[1]} |" in part for r in rows[:3]) and f"| {rows[3][1]} |" not in part
    assert "Reading 11:" not in part                      # the extra bullets went before the first rows
    assert part.count("\n- ") >= 3 and "Downstream SNR 25.0-38.6 dB." in part


def test_a_timed_list_over_its_budget_gets_shorter_before_any_bullet_goes():
    # the cable line's restart count once ended a bullet the trimmer dropped whole; a long timed list now shrinks to
    # its largest few first, and only then does anything go
    from ai_pack_helpers import Section
    facts = canary_facts(week=True)
    times = [f"+{900 + n:,} at 2{n}:00" for n in range(6)]
    versions = [f"New uncorrectable codewords: {'; '.join(times[:n])}; {6 - n} more." for n in (4, 3, 2)]
    full = "New uncorrectable codewords: " + "; ".join(times) + "."
    findings = ["Downstream SNR 25.0-38.6 dB.", full, "Upstream power 41.0-44.2 dBmV, the last bullet."]

    def part(section, budget):
        module = dataclasses.replace(facts.modules[0], budget_chars=budget, section=section)
        pack = render_pack(dataclasses.replace(facts, modules=(module,) + facts.modules[1:]), "full")
        md = pack.markdown
        return md[md.index("### Cable line (DOCSIS)"):md.index("###", md.index("### Cable line (DOCSIS)") + 4)], pack
    budget = len(part(Section(findings=findings[:1] + versions[-1:] + findings[2:]), 10 ** 6)[0])
    text, pack = part(Section(findings=findings, shorter={1: tuple(versions)}), budget)
    assert versions[-1] in text and "the last bullet." in text and full not in text
    assert "docsis: bullets shortened 1" in pack.trimmed

