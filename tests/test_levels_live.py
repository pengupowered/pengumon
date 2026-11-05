"""The line, router, uptime and report levels are read where they are used, at the moment they are used: a saved
setting changes the next grading, strip or report with no restart. The defaults are pinned elsewhere (test_docsis,
test_grades, test_router, test_uptime and the report golden all run at the defaults)."""

from types import SimpleNamespace

from pengumon.analysis.incident import HopImpact, docsis_correlation
from pengumon.config import AppConfig
from pengumon.devices.levels import RouterLevelsConfig
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.grades import CRITICAL, GOOD, WARN
from pengumon.incidents.model import Incident
from pengumon.lines import docsis
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.lines.levels import LineSignalConfig
from pengumon.reports.common import find_primary_failing_hop, material_loss_points
from pengumon.reports.dossier import render_dossier_markdown
from pengumon.reports.settings import ReportsConfig, UptimeConfig
from pengumon.reports.uptime import block_status

from device_helpers import build_router
from test_dossier_render import facts, healthy_snapshot
from test_grades import router_result


def channel(direction="downstream", power=1.0, snr=38.0, unc=0):
    return DocsisChannel(channel_id=1, direction=direction, frequency_mhz=600.0, power_dbmv=power, snr_db=snr,
                         uncorrectables=unc)


# --- DOCSIS -------------------------------------------------------------------------------------------------------------

def test_the_snapshot_status_and_grades_follow_the_levels():
    reading = [channel(power=3.0, snr=34.0), channel("upstream", power=46.0)]
    assert docsis.summarize(reading, 1.0).status == "healthy"
    snapshot = docsis.summarize(reading, 1.0, LineSignalConfig(ds_snr_warning_db=35.0, ds_snr_critical_db=31.0))
    assert snapshot.status == "warning" and snapshot.grades["ds_snr"] == WARN
    snapshot = docsis.summarize(reading, 1.0, LineSignalConfig(us_power_warning_dbmv=44.0, us_power_critical_dbmv=45.0))
    assert snapshot.status == "critical" and snapshot.grades["us_power"] == CRITICAL
    assert snapshot.channels[1].grades["power"] == CRITICAL
    assert docsis.summarize(reading, 1.0, LineSignalConfig(ds_power_warning_abs_dbmv=2.0)).grades["ds_power"] == WARN


def test_the_ranges_follow_the_levels():
    ranges = docsis.ranges(LineSignalConfig(ds_power_warning_abs_dbmv=6.0, uncorrectable_warning_per_minute=30.0))
    assert ranges["ds_power"].as_dict()["good_low"] == -6.0 and ranges["ds_power"].as_dict()["good_high"] == 6.0
    assert ranges["uncorrectables"].as_dict()["good_high"] == 30.0


def test_judge_errors_follows_the_levels():
    assert docsis.judge_errors(100, 60.0, None, False)[0] == WARN      # 100 a minute: from 60 warn
    rate = LineSignalConfig(uncorrectable_warning_per_minute=200.0, uncorrectable_critical_per_minute=300.0)
    assert docsis.judge_errors(100, 60.0, None, False, rate)[0] == GOOD
    assert docsis.judge_errors(301, 60.0, None, False, rate)[0] == CRITICAL
    assert docsis.judge_errors(5, 60.0, 5e-6, False)[0] == WARN        # ratio from 1e-6 warns
    ratio = LineSignalConfig(uncorrectable_ratio_warning=1e-5, uncorrectable_ratio_critical=1e-3)
    grade, note = docsis.judge_errors(5, 60.0, 5e-6, False, ratio)
    assert grade == GOOD and "1e-05" in note
    assert docsis.judge_errors(5, 60.0, 5e-4, False, ratio)[0] == WARN      # critical only from 1e-3 (default 1e-4)
    assert docsis.judge_errors(5, 60.0, 2e-3, False, ratio)[0] == CRITICAL


def test_the_dossier_channel_rules_follow_the_levels():
    weak = channel("upstream", power=37.0)
    assert docsis.is_degraded(weak) and docsis.channel_finding(weak) == "Low upstream power"
    assert not docsis.is_degraded(weak, LineSignalConfig(us_power_low_warning_dbmv=36.0, us_power_low_critical_dbmv=34.0))
    snr, levels = channel(snr=34.0), LineSignalConfig(ds_snr_warning_db=35.0, ds_snr_critical_db=31.0)
    assert docsis.is_degraded(snr, levels) and docsis.channel_finding(snr, levels) == "Low SNR"


def test_the_dossier_quotes_the_warning_levels_in_force():
    text = render_dossier_markdown(facts(docsis_snapshot=healthy_snapshot(), snap_ok=True))
    assert "(typical range -8 to +8)" in text and "(typically above 33)" in text and "(typically 38 to 49)" in text
    levels = LineSignalConfig(ds_power_warning_abs_dbmv=6.5, ds_snr_warning_db=35.0, ds_snr_critical_db=31.0,
                              us_power_low_warning_dbmv=37.0, us_power_warning_dbmv=47.0)
    text = render_dossier_markdown(facts(docsis_snapshot=healthy_snapshot(), snap_ok=True, line_levels=levels))
    assert "(typical range -6.5 to +6.5)" in text and "(typically above 35)" in text and "(typically 37 to 47)" in text


# --- the router ---------------------------------------------------------------------------------------------------------

async def test_the_router_grades_follow_the_levels_and_the_busy_level_live():
    busy, config = {"pct": 60.0}, AppConfig()
    monitor = build_router().monitor
    monitor.watch_load(SimpleNamespace(down_mbps=1000.0, up_mbps=40.0), lambda: busy["pct"])
    monitor.watch_levels(config)
    await monitor.on_result(1000.0, router_result(cpu=80.0, rx_bytes=0, tx_bytes=0))
    # 10 s later: download 300 Mbps (30 %) and upload 30 Mbps (75 %)
    snapshot = await monitor.on_result(1010.0, router_result(cpu=80.0, rx_bytes=375_000_000, tx_bytes=37_500_000))
    assert (snapshot.grades["cpu"], snapshot.grades["wan_tx"]) == (GOOD, WARN)
    config.router_levels = RouterLevelsConfig(cpu_warning_pct=70.0, wan_critical_pct=74.0)  # a save: read at the next poll
    busy["pct"] = 80.0  # the classifier's busy level changed: the callable reads it at the next poll
    snapshot = await monitor.on_result(1020.0, router_result(cpu=80.0, rx_bytes=750_000_000, tx_bytes=75_000_000))
    assert (snapshot.grades["cpu"], snapshot.grades["wan_tx"]) == (WARN, CRITICAL)
    assert snapshot.status == "warning"
    assert monitor.ranges["wan_rx"].as_dict()["good_high"] == 80.0


async def test_the_router_memory_levels_follow_the_settings():
    monitor, config = build_router().monitor, AppConfig()
    monitor.watch_levels(config)
    assert (await monitor.on_result(1000.0, router_result(mem=90.0))).grades["mem"] == WARN
    config.router_levels.mem_warning_pct, config.router_levels.mem_critical_pct = 91.0, 92.0  # in place, as a save
    assert (await monitor.on_result(1010.0, router_result(mem=90.0))).grades["mem"] == GOOD
    assert (await monitor.on_result(1020.0, router_result(mem=92.5))).grades["mem"] == CRITICAL


async def test_the_line_load_component_hands_the_monitor_a_live_busy_level():
    from pengumon.engine import line_load
    config = AppConfig()
    seen = []

    class Monitor:
        def watch_load(self, load, busy):
            seen.append(busy)

    class Devices(list):
        def primary(self, capability):
            return None

        def wan_rates_source(self):
            return None

    class Repo:
        async def get_meta(self, key):
            return "{}"

    ctx = SimpleNamespace(config=config, state=SimpleNamespace(repository=Repo(), devices=Devices(
        [SimpleNamespace(monitor=Monitor())])))
    await line_load.register(ctx)
    assert seen[0]() == 60.0
    config.classifier.busy_load_pct = 75.0
    assert seen[0]() == 75.0


# --- uptime and reports ----------------------------------------------------------------------------------------------

def test_block_status_follows_the_uptime_levels():
    assert block_status(900, 891, 0) == "degraded" and block_status(900, 780, 2) == "outage"
    levels = UptimeConfig(outage_dead_minutes=3, degraded_loss_pct=2.0)
    assert block_status(900, 891, 0, levels) == "ok"
    assert block_status(900, 882, 0, levels) == "degraded"
    assert block_status(900, 780, 2, levels) == "degraded" and block_status(900, 720, 3, levels) == "outage"


async def test_build_uptime_reads_the_live_uptime_section(tmp_path):
    from test_uptime import NOW, block_start, make_repo, minute_samples
    from pengumon.reports.uptime import build_uptime

    repo = await make_repo(tmp_path)
    await repo.insert_samples(minute_samples("t", block_start(0), lost_seconds=60, minutes=2))  # 2 dead minutes
    config = AppConfig()
    cells = (await build_uptime(repo, config, now=NOW))["targets"][0]["cells"]
    assert cells[-1] == "outage"
    config.uptime.outage_dead_minutes = 3  # what a save does: the live section changes in place
    cells = (await build_uptime(repo, config, now=NOW))["targets"][0]["cells"]
    assert cells[-1] == "degraded"
    await repo.close()


def hop(tid, loss, sent=1000):
    return {"target_id": tid, "sent": sent, "loss_pct": loss}


def test_the_failing_hop_rule_follows_the_material_level():
    hops = [hop("a", 0.0), hop("b", 0.6), hop("c", 1.7)]
    assert find_primary_failing_hop(hops)["target_id"] == "b"
    assert find_primary_failing_hop(hops, (), 1.0)["target_id"] == "c"
    assert find_primary_failing_hop(hops, (), 2.0) is None
    config = AppConfig()
    assert material_loss_points(config) == 0.5 and material_loss_points(None) == 0.5
    config.reports.material_loss_points = 1.0
    assert material_loss_points(config) == 1.0
    assert material_loss_points(SimpleNamespace()) == ReportsConfig().material_loss_points


def test_the_dossier_states_the_material_level_in_force():
    text = render_dossier_markdown(facts(material=1.5))
    assert "exceeds that of the hops before it by 1.5 points or more" in text
    text = render_dossier_markdown(facts(max_lan_loss=1.2, material=1.5))
    assert "(below the 1.5% threshold)" in text


# --- the inspector --------------------------------------------------------------------------------------------------------

def _impact(category, loss):
    return HopImpact(target_id=category, name=category, category=category, sent=100, received=100 - int(loss),
                     loss_pct=loss, avg_ms=None, min_ms=None, max_ms=None, jitter_ms=None)


def test_the_inspector_judges_the_router_at_the_classifiers_degraded_level():
    incident = Incident(start_time=1000.0, end_time=1030.0, duration_seconds=30.0, cause_code="double_nat_congestion",
                        title="t", description="d", severity="critical", resolved=True, kind="gateway_down")
    hops = [_impact("local", 17.0), _impact("gateway", 60.0)]
    assert docsis_correlation(incident, hops, [], None).gateway_offline is False  # router lossy at 15 %
    assert docsis_correlation(incident, hops, [], None, local_loss_pct=20.0).gateway_offline is True
    assert ClassifierConfig().degraded_loss_pct == 15.0
