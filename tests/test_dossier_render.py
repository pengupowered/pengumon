"""reports.render_dossier_markdown and the pure decisions of the dossier, on hand-built DossierFacts."""

import time
from dataclasses import replace

import pytest

from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.lines.docsis import channel_finding
from pengumon.reports.dossier import DossierFacts, hop_note, render_dossier_markdown
from pengumon.reports.speed import summarize
from platform_helpers import skip_without_tzset

NOW = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC


@pytest.fixture(autouse=True)
def utc_clock(monkeypatch):
    skip_without_tzset()
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def chan(i=1, direction="downstream", power=1.0, snr=38.0, unc=0):
    return DocsisChannel(channel_id=i, direction=direction, frequency_mhz=600.0 + i, power_dbmv=power, snr_db=snr,
                         modulation="QAM256", correcteds=7, uncorrectables=unc)


def hop(tid, lost=0, sent=1000):
    return {"target_id": tid, "sent": sent, "lost": lost, "loss_pct": round(lost / sent * 100.0, 2) if sent else 0.0,
            "avg_ms": 11.0, "max_ms": 40.0}


def facts(**changes) -> DossierFacts:
    base = DossierFacts(
        hours=24, now=NOW, total_incidents=0, total_dur_sec=0, total_dur_min=0.0, excluded=0, busy_pct=60.0,
        under_load=0, by_cause=[], hop_stats=[], primary=None, later_lossy=[], labels={}, names={},
        max_lan_loss=None, docsis_snapshot=None, snap_ok=False, has_window_snaps=False,
        unc_increase=0, degraded=[], rf_evidence=False, local_name="the local router",
        gw_name="the gateway")
    return replace(base, **changes)


def healthy_snapshot(**kw):
    return DocsisSignalSnapshot(timestamp=NOW - 60, status="healthy", downstream_power_avg=1.0, downstream_snr_avg=38.0,
                                upstream_power_avg=42.0, uncorrectables_total=1234, channels=[chan()], **kw)


def lines(f):
    return render_dossier_markdown(f).split("\n")


def test_an_empty_dossier_says_so_everywhere():
    text = render_dossier_markdown(facts())
    assert text.startswith("# Broadband Packet Loss Evidence Report\n**Generated:** 2025-10-09 08:53:20 UTC | "
                           "**Observation window:** past 24 hours\n**Local hardware:** no local or gateway probes")
    assert "- **Recorded incidents:** 0" in text and "- No single failing hop was isolated" in text
    assert "- No modem data: modem credentials are not configured in Pengumon." in text
    assert "- No modem data is available, so nothing can be said about the RF side." in text
    assert "No single failing hop could be isolated from the probe data." in text
    assert text.endswith("```")
    assert "By cause" not in text and "excluded" not in text and "event log" not in text
    assert "Speed" not in text                                           # no speed tests in the window: no section


def test_summary_lines_follow_the_counts():
    f = facts(total_incidents=3, total_dur_sec=185.0, total_dur_min=3.1, excluded=2, under_load=1, busy_pct=70.0,
              by_cause=[("isp_loss", 2, 1.5), ("dns_resolver_failure", 1, 1.6)])
    text = render_dossier_markdown(f)
    assert "- **Cumulative duration:** 3 minutes 5 seconds" in text
    assert "- **By cause:** isp_loss: 2 (1 minute 30 seconds), dns_resolver_failure: 1 (1 minute 36 seconds)" in text
    assert "- 1 of these occurred while the line was at 70% load or more." in text
    assert "- 2 events during heavy line load were excluded (expected)." in text
    assert "totaling 3 minutes 5 seconds (2 further events during heavy line load were excluded as expected)." in text


def test_the_primary_hop_is_named_with_its_label_and_later_loss_is_listed():
    stats = [hop("router"), hop("isp", lost=30), hop("cloud", lost=6)]
    primary = stats[1]
    f = facts(hop_stats=stats, primary=primary, later_lossy=[stats[2]], names={"router": "Router", "isp": "ISP Hub"},
              labels={"router": "Router (192.168.0.1)", "isp": "ISP Hub (198.51.100.4)", "cloud": "cloud"},
              max_lan_loss=0.0)
    text = render_dossier_markdown(f)
    assert "- **First hop with elevated loss:** ISP Hub (198.51.100.4) (3.0% loss)." in text
    assert "| Hop 1 | Router | 1000 | 0 | 0.0% | 11.0 ms | 40.0 ms | No material loss |" in text
    assert "| Hop 2 | ISP Hub | 1000 | 30 | 3.0% | 11.0 ms | 40.0 ms | First hop with elevated loss |" in text
    assert "| Hop 3 | cloud | 1000 | 6 | 0.6% | 11.0 ms | 40.0 ms | Elevated loss |" in text
    assert "also present at later hops (cloud), so it continues past that point." in text
    assert "- Loss first appears at ISP Hub (198.51.100.4)." in text
    assert "Loss is first elevated at ISP Hub (198.51.100.4): 3.0%." in text
    assert "(below the 0.5% threshold)" in text
    alone = render_dossier_markdown(replace(f, later_lossy=[]))
    assert "Later hops show no comparable loss." in alone and "also present at later hops" not in alone


def test_local_hardware_line_and_ticket_use_the_configured_names():
    f = facts(max_lan_loss=0.5, local_name="Orbi", gw_name="Hitron")
    text = render_dossier_markdown(f)
    assert "**Local hardware:** 0.5% worst loss across local and gateway hops (at or above the 0.5% threshold)" in text
    assert "Loss to Orbi and Hitron: at most 0.5%." in text


def test_modem_section_distinguishes_error_unconfigured_and_a_readable_modem():
    assert "The modem could not be read at the last poll." in render_dossier_markdown(
        facts(docsis_snapshot=DocsisSignalSnapshot(timestamp=NOW, status="error")))
    assert "credentials are not configured" in render_dossier_markdown(
        facts(docsis_snapshot=DocsisSignalSnapshot(timestamp=NOW, status="unconfigured")))
    snap = healthy_snapshot()
    text = render_dossier_markdown(facts(docsis_snapshot=snap, snap_ok=True, has_window_snaps=True, unc_increase=1500))
    assert "- Uncorrectable codewords since modem boot: 1,234" in text
    assert "- New uncorrectable codewords recorded in the window: 1,500" in text
    assert "- All bonded channels are within typical power and SNR ranges at the last poll." in text
    assert ("- The modem shows no RF impairment in the window: no channel outside typical levels and no new "
            "uncorrectable codewords.") in text
    assert "T3" not in text and "T4" not in text and "event log" not in text


def test_degraded_channels_are_tabled_worst_first_with_the_ticket_line():
    bad = chan(7, unc=900, snr=29.0)
    other = chan(9, direction="upstream", power=50.0, snr=None)
    snap = healthy_snapshot().model_copy(update={"channels": [bad, other, chan(1)]})
    f = facts(docsis_snapshot=snap, snap_ok=True, degraded=[(bad, "High uncorrectable count"), (other, "High upstream power")],
              rf_evidence=True, unc_increase=40)
    text = render_dossier_markdown(f)
    assert "| Downstream | `7` | 607.0 MHz | QAM256 | 1.0 dBmV | 29.0 dB | 7 | 900 | High uncorrectable count |" in text
    assert "| Upstream | `9` | 609.0 MHz | QAM256 | 50.0 dBmV | - | 7 | 0 | High upstream power |" in text
    assert "2 of 3 channels are outside typical levels; the most affected is downstream channel 7." in text
    assert "- The modem reports RF-level signs: 2 channel(s) outside typical levels; 40 new uncorrectable codewords " \
           "in the window." in text
    assert "Modem: downstream channel 7 at 607.0 MHz is outside typical levels (2 channel(s) in total)." in text
    assert "Modem: 40 new uncorrectable codewords in the window." in text
    assert "Request: please check the line (tap, drop, connectors) for RF impairment." in text


def test_rendering_does_not_modify_the_facts():
    f = facts(hop_stats=[hop("a", lost=10)], primary=hop("a", lost=10), labels={"a": "A"})
    before = repr(f)
    render_dossier_markdown(f)
    assert repr(f) == before


@pytest.mark.parametrize("channel, finding", [
    (chan(unc=501), "High uncorrectable count"),
    (chan(unc=500), "Uncorrectables present"),
    (chan(unc=101, snr=40.0), "Uncorrectables present"),
    (chan(unc=900, snr=20.0), "High uncorrectable count"),
    (chan(snr=32.9), "Low SNR"),
    (chan(direction="upstream", power=49.1, snr=40.0), "High upstream power"),
    (chan(direction="upstream", power=37.9, snr=40.0), "Low upstream power"),
    (chan(direction="upstream", power=50.0, snr=30.0), "Low SNR"),
    (chan(power=-8.5, snr=40.0), "Power level out of range"),
    (chan(unc=0, snr=None, power=9.0), "Power level out of range"),
])
def test_channel_finding_takes_the_first_matching_reason(channel, finding):
    assert channel_finding(channel) == finding


def test_hop_note():
    primary = hop("b", lost=30)
    assert hop_note(primary, primary) == "First hop with elevated loss"
    assert hop_note(hop("c", lost=5), primary) == "Elevated loss"      # exactly 0.5 %
    assert hop_note(hop("c", lost=4), primary) == "No material loss"
    assert hop_note(hop("b", lost=30), None) == "Elevated loss"


def test_the_dossier_says_which_side_stopped_answering_and_never_calls_gateway_loss_a_line_fault():
    """The gateway is probed on its home-side address, which does not use the line: loss starting there is the
    gateway's, and the line check is asked for only on the modem's RF signs. Loss starting at the provider's first
    router while the gateway answers is the line or that router."""
    gw = hop("gateway", lost=50)
    at_gateway = render_dossier_markdown(facts(hop_stats=[gw], primary=gw, primary_place="gateway",
                                               labels={"gateway": "Modem"}, by_cause=[("gateway", 2, 3.0)]))
    assert "the gateway itself stopped answering" in at_gateway and "not answering on its home side: 2 (3 minutes)" in at_gateway
    assert "the gateway answered" not in at_gateway and "check the line" not in at_gateway
    first = hop("isp", lost=50)
    at_isp = render_dossier_markdown(facts(hop_stats=[first], primary=first, primary_place="first_isp",
                                           labels={"isp": "Hub"}, rf_evidence=True, unc_increase=40))
    assert "the provider's first router, while the gateway answers: the line or that router" in at_isp
    assert "point to the line itself" in at_isp and "Request: please check the line" in at_isp


def test_the_dossier_reads_as_words_in_the_viewers_zone():
    from zoneinfo import ZoneInfo
    gone = hop("a")
    f = facts(zone=ZoneInfo("America/New_York"), hours=1, total_incidents=1, total_dur_sec=0, excluded=1,
              hop_stats=[gone], names={"a": "Hop 3: Example ISP"})
    text = render_dossier_markdown(f)
    assert "**Generated:** 2025-10-09 04:53:20 EDT | **Observation window:** past 1 hour" in text
    assert "- **Cumulative duration:** none" in text
    assert "- 1 event during heavy line load was excluded (expected)." in text
    assert "| Hop 1 | Example ISP |" in text and "Hop 3" not in text
    assert "recorded 1 incident totaling none" in text


def test_the_dossier_and_the_ai_pack_judge_a_channel_by_one_set_of_findings(monkeypatch):
    """docsis.channel_findings is the one judgment: the dossier's reason is its first, the pack's text every one of
    them worst first, and both follow its named thresholds."""
    from pengumon.lines import docsis
    from pengumon.lines.docsis_contrib import _finding
    worst = chan(unc=900, snr=29.0)
    assert channel_finding(worst) == "High uncorrectable count"
    assert _finding(worst, docsis.DEFAULT_LEVELS) == ("SNR below 30 dB (errors likely); high uncorrectable count since "
                                                      "the modem started")
    some = chan(unc=300)
    assert channel_finding(some) == "Uncorrectables present" and docsis.is_degraded(some)
    monkeypatch.setattr(docsis, "UNCORRECTABLES_HIGH", 200)
    assert channel_finding(some) == "High uncorrectable count"
    assert _finding(some, docsis.DEFAULT_LEVELS) == "High uncorrectable count since the modem started"
    monkeypatch.setattr(docsis, "UNCORRECTABLES_LISTED", 400)
    assert not docsis.is_degraded(chan(unc=150)) and channel_finding(chan(unc=150)) == "Power level out of range"


def speed_rows(n=30, low=4, low_mbps=100.0, mbps=212.0):
    """`n` finished tests, `low` of them at `low_mbps` (the rest at `mbps`), 19 Mbps up, 10 ms idle and 50 ms loaded."""
    return [{"status": "ok", "down_mbps": low_mbps if k < low else mbps, "up_mbps": 19.0, "idle_ms": 10.0,
             "loaded_down_ms": 50.0, "loaded_up_ms": 30.0} for k in range(n)]


def test_the_speed_section_states_the_tests_against_the_plan():
    rows = speed_rows() + [{"status": "failed", "down_mbps": None, "up_mbps": None},
                           {"status": "skipped", "down_mbps": None, "up_mbps": None}]
    text = render_dossier_markdown(facts(speed=summarize(rows, 300.0, 20.0)))
    section = text.split("## Speed tests\n")[1].split("\n## ")[0]
    assert section.splitlines() == [
        "- **Tests in the window:** 32 tests (30 ok, 1 failed, 1 skipped)",
        "- Download: median 212 of 300 Mbps (71 %), lowest 100 Mbps",
        "- Upload: median 19 of 20 Mbps (95 %), lowest 19 Mbps",
        "- Measured by the test alone (one server; no router reading of the line)",
        "- 4 of 30 tests under 50 % of the plan speed",
        "- Latency under load: median 40 ms added, bufferbloat grade B"]
    assert text.index("## 3. Modem") < text.index("## Speed tests") < text.index("## 4. What the data shows")
    assert ("Speed tests (32 tests (30 ok, 1 failed, 1 skipped)): download: median 212 of 300 Mbps (71 %); upload: "
            "median 19 of 20 Mbps (95 %); 4 of 30 tests under 50 % of the plan speed.") in text.split("```text")[1]


def test_the_under_count_looks_at_both_directions_and_each_test_counts_once():
    rows = speed_rows(10, low=0) + [{"status": "ok", "down_mbps": 100.0, "up_mbps": 5.0},   # both under: one test
                                    {"status": "ok", "down_mbps": 300.0, "up_mbps": 9.0},   # upload alone
                                    {"status": "ok", "down_mbps": 150.0, "up_mbps": 10.0}]  # exactly 50 %: not under
    s = summarize(rows, 300.0, 20.0)
    assert (s.measured, s.under) == (13, 2)
    assert summarize(speed_rows(), None, None).under == 0                      # no plan: nothing to be under


def test_without_a_plan_speed_the_section_says_so_and_a_partial_test_still_counts():
    rows = speed_rows(3, low=0) + [{"status": "partial", "down_mbps": 90.0, "up_mbps": None}]
    text = render_dossier_markdown(facts(speed=summarize(rows, None, None)))
    assert "- **Tests in the window:** 4 tests (3 ok, 1 partial)" in text
    assert "- Download: median 212 Mbps, lowest 90 Mbps" in text and "of the plan speed" not in text
    assert "No plan speed is set in Pengumon (Settings > Your line)" in text


def test_tests_that_measured_nothing_say_so():
    text = render_dossier_markdown(facts(speed=summarize([{"status": "skipped", "down_mbps": None, "up_mbps": None}],
                                                         300.0, 20.0)))
    assert "- **Tests in the window:** 1 test (1 skipped)" in text and "- No test measured a speed." in text
    assert "Speed tests (" not in text.split("```text")[1]                      # nothing to put in the ticket
