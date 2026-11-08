"""Outages in the AI pack: the section after the incident table, its numbers pointing into that table, the redaction
levels, and the facts the core decides from the stored incidents."""

import dataclasses
from types import SimpleNamespace

from ai_pack_helpers import canary_facts, ts
from ai_pack_seed import DAY, add_incident, make_config, seed, week_end
from pengumon.db.repository import DatabaseRepository
from pengumon.reports.ai_findings import collect_pack_facts
from pengumon.reports.ai_pack import OutageFact, render_pack, view_of
from datetime import timezone

JOINED = OutageFact(1, ts(10, 30, 21, 14, 3), 1500.0, 840.0, 2, 2, ("line", "isp"), True, (1, 2))


def with_outages(*outages, gap=10, **kw):
    return dataclasses.replace(canary_facts(**kw), outages=tuple(outages), outage_gap_minutes=gap)


def section(md):
    return md.split("## Outages", 1)[1].split("\n## ", 1)[0]


def test_an_outage_that_joined_incidents_is_listed_with_their_numbers_and_the_label():
    md = render_pack(with_outages(JOINED, OutageFact(3, ts(10, 31, 9, 5), 300.0, 0.0, 1, 0, ("gateway",), False, (3,)))
                     , "full").markdown
    text = section(md)
    assert text.startswith(" (2 outages, 1 of them joining several incidents)")
    assert "within 10 min are one outage" in text and "a guess from the hour alone" in text and "changes no figure" in text
    assert "| 2 (2 critical) | line, isp | yes | #1, #2 |" in text
    assert text.count("\n| ") == 2                                   # the header and one outage: not the lone drop
    assert md.index("## Incidents") < md.index("## Outages") < md.index("## Close-ups")


def test_without_a_joined_outage_the_section_says_each_incident_stands_alone():
    md = render_pack(with_outages(OutageFact(1, ts(10, 30, 21, 14, 3), 31.0, 31.0, 1, 1, ("gateway",), False, (1,))),
                     "full").markdown
    assert "Each incident above is an outage of its own" in section(md) and "within 10 min" in section(md)
    assert "| Drops |" not in md


def test_grouping_off_is_said_and_no_outages_adds_no_section():
    off = render_pack(with_outages(OutageFact(1, ts(10, 30, 21, 14, 3), 31.0, 31.0, 1, 1, ("gateway",), False, (1,)),
                                   gap=0), "full").markdown
    assert "Grouping is off" in section(off)
    assert "## Outages" not in render_pack(canary_facts(), "full").markdown


def test_incidents_missing_from_a_capped_table_are_counted_not_numbered():
    far = OutageFact(9, ts(10, 30, 21, 14, 3), 600.0, 600.0, 3, 3, ("isp",), False, (1, 40, 41))
    text = section(render_pack(with_outages(far), "full").markdown)
    assert "#1 and 2 not in the table" in text


def test_the_anonymized_pack_gives_the_outage_a_relative_time_and_no_date():
    md = render_pack(with_outages(JOINED), "anonymized").markdown
    text = section(md)
    assert "2026" not in text and "Oct" not in text and "Day " in text
    assert "| 2 (2 critical) | line, isp | yes |" in text


async def test_the_core_groups_the_stored_incidents_into_outages(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "pack.db"))
    await repo.initialize()
    config, end = make_config(isp=2, public=1, dns=1), week_end()
    ids = await seed(repo, config, end)
    first = await add_incident(repo, config, ids, end - 7200, 30, "isp1", kind="isp_loss")
    second = await add_incident(repo, config, ids, end - 7200 + 200, 30, "isp1", kind="isp_loss")
    lone = await add_incident(repo, config, ids, end - 3 * 3600, 30, "isp1", kind="isp_loss")
    f = await collect_pack_facts(repo, config, start=end - DAY, end=end, now=end)
    assert [(o["id"], o["incident_ids"]) for o in f.outages] == [(lone, [lone]), (first, [first, second])]
    assert f.outage_gap_minutes == 10
    view = view_of(f, {t.id: t for t in list(config.targets) + list(config.dns_probes)}, [], "Example-ISP",
                   timezone.utc, "Past 24 hours", "0.22.0")
    assert [(o.id, o.drops) for o in view.outages] == [(lone, 1), (first, 2)]
    config.incidents = SimpleNamespace(group_gap_minutes=0, maintenance_start=None, maintenance_end=None)
    off = await collect_pack_facts(repo, config, start=end - DAY, end=end, now=end)
    assert off.outage_gap_minutes == 0 and [o["drops"] for o in off.outages] == [1, 1, 1]
    await repo.close()
