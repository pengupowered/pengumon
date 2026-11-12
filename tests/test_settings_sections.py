"""The section models' ranges, cross-field rules and new fields (the Settings contract): bad values are refused
with a plain message that never repeats a URL, and every new default equals the constant the code uses today, so
this branch alone changes nothing."""

import pytest
from pydantic import ValidationError

from pengumon.config import AppConfig, DatabaseConfig, ServerConfig
from pengumon.devices import monitors
from pengumon.devices.levels import RouterLevelsConfig
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.discovery.settings import DiscoverySettings
from pengumon.engine.prober_settings import DnsProbeConfig, HopStatusConfig, ProberConfig
from pengumon.lines import docsis
from pengumon.lines.levels import LineSignalConfig
from pengumon.models.probes import TargetConfig
from pengumon.notify.notifier import MIN_INTERVAL_SECONDS
from pengumon.notify.settings import AlertsConfig, NotificationsConfig, QuietHours, WebhookConfig, webhook_id
from pengumon.reports import common as reports_common
from pengumon.reports import uptime
from pengumon.reports.settings import ReportsConfig, UptimeConfig
from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.settings.general import GeneralConfig, check_timezone, server_timezone
from pengumon.web.settings import DisplayConfig


@pytest.mark.parametrize("model, values", [
    (DatabaseConfig, {"retention_days": 0}),
    (DatabaseConfig, {"retention_days": -1}),
    (DatabaseConfig, {"retention_days": 91}),
    (DatabaseConfig, {"rollup_retention_days": 29}),
    (DatabaseConfig, {"retention_days": 60, "rollup_retention_days": 40}),  # rollups shorter than raw
    (ClassifierConfig, {"busy_load_pct": 0}),
    (ClassifierConfig, {"busy_load_pct": 101}),
    (ClassifierConfig, {"consecutive_failures_for_incident": 0}),
    (ClassifierConfig, {"consecutive_failures_for_latency_incident": 0}),
    (ClassifierConfig, {"consecutive_successes_for_recovery": 0}),
    (ClassifierConfig, {"local_latency_spike_threshold_ms": -5}),
    (ClassifierConfig, {"public_jitter_spike_threshold_ms": 0}),
    (ClassifierConfig, {"isp_latency_spike_threshold_ms": 0}),
    (ClassifierConfig, {"dns_timeout_threshold_ms": 5}),
    (ClassifierConfig, {"degraded_loss_pct": 40, "down_loss_pct": 40}),  # degraded must be below down
    (ClassifierConfig, {"degraded_loss_pct": 0}),
    (ClassifierConfig, {"down_loss_pct": 101}),
    (ClassifierConfig, {"dns_cooldown_seconds": -1}),
    (HopStatusConfig, {"public_latency_ms": 20000}),
    (ProberConfig, {"timeout_seconds": 0}),
    (ProberConfig, {"timeout_seconds": 1.5}),
    (ProberConfig, {"interval_seconds": 0.5, "timeout_seconds": 0.8}),  # longer than the round
    (ProberConfig, {"dns_timeout_seconds": 6}),
    (ServiceChecksConfig, {"interval_seconds": 0}),
    (ServiceChecksConfig, {"failures_for_incident": 0}),
    (ServiceChecksConfig, {"notify_after_seconds": -1}),
    (AlertsConfig, {"min_severity": "info"}),
    (AlertsConfig, {"delay_seconds": 3601}),
    (AlertsConfig, {"repeat_window_seconds": -1}),
    (QuietHours, {"start": "07:00", "end": "07:00"}),
    (QuietHours, {"start": "24:00"}),
    (QuietHours, {"end": "7am"}),
    (DiscoverySettings, {"daily_check_time": "25:10"}),
    (GeneralConfig, {"timezone": "Mars/Olympus_Mons"}),
    (GeneralConfig, {"timezone": "../../etc/passwd"}),
    (DisplayConfig, {"grafana_url": "ftp://grafana.example.com"}),
    (DisplayConfig, {"grafana_url": "https://grafana.example.com/?orgId=1"}),
    (DisplayConfig, {"default_latency_range": "7d"}),
    (ServerConfig, {"log_level": "loud"}),
    (ServerConfig, {"log_levels": "pengumon.devices"}),
    (LineSignalConfig, {"ds_snr_critical_db": 34}),  # above the warning level
    (LineSignalConfig, {"us_power_low_critical_dbmv": 39}),
    (LineSignalConfig, {"us_power_warning_dbmv": 52}),
    (LineSignalConfig, {"uncorrectable_ratio_warning": 1e-3}),
    (LineSignalConfig, {"uncorrectable_warning_per_minute": 600}),
    (LineSignalConfig, {"uncorrectable_ratio_warning": 0}),
    (RouterLevelsConfig, {"mem_warning_pct": 96}),
    (RouterLevelsConfig, {"cpu_warning_pct": 0}),
    (UptimeConfig, {"outage_dead_minutes": 0}),
    (UptimeConfig, {"degraded_loss_pct": 0}),
    (ReportsConfig, {"material_loss_points": 0}),
    (TargetConfig, {"id": "a", "name": "A", "address": "192.0.2.1", "warn_latency_ms": 0}),
    (TargetConfig, {"id": "a", "name": "A", "address": "192.0.2.1", "warn_latency_ms": 10001}),
    (DnsProbeConfig, {"id": "d", "name": "D", "record_type": "SRVX"}),
    (DnsProbeConfig, {"id": "d", "name": "D", "nameserver": "dns.example.com"}),
    (DnsProbeConfig, {"id": "bad id", "name": "D"}),
    (DnsProbeConfig, {"id": "d", "name": "D", "domain": "-oops"}),
    (ServiceCheckConfig, {"id": "s", "name": "S", "url": "http://198.51.100.7/h", "timeout_seconds": 0.1}),
    (ServiceCheckConfig, {"id": "s", "name": "S", "url": "ftp://198.51.100.7/h"}),
])
def test_out_of_range_values_are_refused(model, values):
    with pytest.raises(ValidationError):
        model(**values)


def test_the_messages_are_plain_and_never_echo_a_url():
    with pytest.raises(ValidationError) as e:
        DatabaseConfig(retention_days=0)
    assert "greater than or equal to 1" in str(e.value)
    secret = "https://hooks.example.com/abc?token=very-secret-value"
    with pytest.raises(ValidationError) as e:
        ServiceCheckConfig(id="s", name="S", url=secret.replace("https", "gopher"))
    assert "very-secret-value" not in str(e.value)
    with pytest.raises(ValidationError) as e:
        DisplayConfig(grafana_url=secret)
    assert "very-secret-value" not in str(e.value)


def test_wan_critical_must_stay_above_the_busy_level():
    with pytest.raises(ValidationError, match="wan_critical_pct must be above"):
        AppConfig(classifier={"busy_load_pct": 95}, router_levels={"wan_critical_pct": 95})
    assert AppConfig(classifier={"busy_load_pct": 80}, router_levels={"wan_critical_pct": 90})


def test_the_edges_of_each_range_are_accepted():
    assert DatabaseConfig(retention_days=1, rollup_retention_days=30).retention_days == 1
    assert DatabaseConfig(retention_days=90, rollup_retention_days=90).rollup_retention_days == 90
    assert ClassifierConfig(busy_load_pct=10, degraded_loss_pct=1, down_loss_pct=2).down_loss_pct == 2
    assert ProberConfig(timeout_seconds=0.2).timeout_seconds == 0.2
    assert QuietHours(start="23:30", end="06:00").end == "06:00"  # across midnight
    assert DiscoverySettings(daily_check_time=" 04:15 ").daily_check_time == "04:15"
    assert DiscoverySettings(daily_check_time="").daily_check_time is None


def test_a_short_round_keeps_the_default_timeout():
    """Tests run the round at 0.05 s; only a timeout someone set is held to the round."""
    assert ProberConfig(interval_seconds=0.05).timeout_seconds == 1.0
    assert ProberConfig(interval_seconds=1.0, timeout_seconds=1.0).timeout_seconds == 1.0


@pytest.mark.parametrize("seconds", [0, -1, 0.0, 61])
def test_a_probe_round_must_be_positive_and_at_most_a_minute_R4_L11(seconds):
    with pytest.raises(ValidationError):
        ProberConfig(interval_seconds=seconds)


def test_log_level_names_are_read_like_the_logging_setup_reads_them():
    assert ServerConfig(log_level="INFO").log_level == "info"
    assert ServerConfig(log_level=" Warn ").log_level == "warning"
    assert ServerConfig(log_levels=" pengumon.devices=debug ").log_levels == "pengumon.devices=debug"
    assert ServerConfig(log_levels="").log_levels is None


def test_timezones():
    assert check_timezone("UTC") == "UTC" and check_timezone("+01:00") == "+01:00"
    assert GeneralConfig(timezone="Europe/Berlin").timezone == "Europe/Berlin"
    assert server_timezone({"TZ": "America/Chicago"}) == "America/Chicago"
    assert server_timezone({"TZ": "Nowhere/Land"}, localtime=__import__("pathlib").Path("/nonexistent")) == "UTC"
    assert GeneralConfig().timezone == server_timezone()


def test_webhooks_get_an_id_from_their_name():
    cfg = NotificationsConfig(webhooks=[
        {"url": "https://hooks.example.com/a", "name": "Home Assistant"},
        {"url": "https://hooks.example.com/b", "name": "Home/Assistant"},
        {"url": "https://hooks.example.com/c"},
        {"url": "https://hooks.example.com/d", "name": "fixed", "id": "my-id"},
    ])
    assert [h.id for h in cfg.webhooks] == ["Home-Assistant", "Home-Assistant-2", "webhook-3", "my-id"]
    assert webhook_id("x" * 70, {"x" * 64}) == "x" * 62 + "-2"
    with pytest.raises(ValidationError):
        WebhookConfig(url="https://hooks.example.com/a", id="not ok!")
    with pytest.raises(ValidationError, match="id is used twice"):
        NotificationsConfig(webhooks=[{"url": "https://a.example.com/", "name": "a", "id": "x"},
                                      {"url": "https://b.example.com/", "name": "b", "id": "x"}])


def test_dns_probe_fields_are_normalized():
    probe = DnsProbeConfig(id="dns_a", name="A", record_type="aaaa", nameserver=" ")
    assert probe.record_type == "AAAA" and probe.nameserver is None
    assert DnsProbeConfig(id="d", name="D", nameserver="2001:db8::53").nameserver == "2001:db8::53"


def test_target_warn_latency_is_optional_and_left_out_of_an_export_when_unset():
    hop = TargetConfig(id="far", name="Far", address="192.0.2.9", category="public")
    assert hop.warn_latency_ms is None and "warn_latency_ms" not in hop.model_dump(exclude_defaults=True)
    assert TargetConfig(id="far", name="Far", address="192.0.2.9", warn_latency_ms=150).warn_latency_ms == 150


def test_the_new_defaults_equal_the_constants_the_code_uses_today():
    signal = LineSignalConfig()
    assert (signal.us_power_critical_dbmv, signal.us_power_warning_dbmv, signal.us_power_low_warning_dbmv,
            signal.us_power_low_critical_dbmv, signal.ds_snr_critical_db, signal.ds_snr_warning_db,
            signal.ds_power_warning_abs_dbmv, signal.uncorrectable_ratio_warning,
            signal.uncorrectable_ratio_critical, signal.uncorrectable_warning_per_minute,
            signal.uncorrectable_critical_per_minute) == (
        51.0, 49.0, 38.0, 35.0, 30.0, 33.0, 8.0, 1e-6, 1e-4, 60.0, 500)  # the module constants these replaced
    router = RouterLevelsConfig()
    assert (router.cpu_warning_pct, router.mem_warning_pct, router.mem_critical_pct, router.wan_critical_pct) == (
        90.0, 85.0, 95.0, 95.0)
    assert UptimeConfig().outage_dead_minutes == 2
    assert UptimeConfig().degraded_loss_pct == 1.0
    assert ReportsConfig().material_loss_points == 0.5
    # the readers start at the defaults
    assert docsis.DEFAULT_LEVELS == signal and monitors.DEFAULT_LEVELS == router
    assert uptime.block_status(900, 891, 0) == "degraded" and reports_common.material_loss_points(None) == 0.5
    assert AlertsConfig().repeat_window_seconds == MIN_INTERVAL_SECONDS
    classifier = ClassifierConfig()  # the literals of engine/classifier (rules.py, metrics.py, tracker.py)
    assert (classifier.degraded_loss_pct, classifier.down_loss_pct, classifier.dns_degraded_loss_pct,
            classifier.dns_cooldown_seconds) == (15.0, 40.0, 25.0, 90.0)
    assert classifier.consecutive_successes_for_recovery == classifier.consecutive_failures_for_incident == 3
    assert classifier.isp_latency_spike_threshold_ms is None
    assert ProberConfig().dns_timeout_seconds is None


def test_the_config_example_and_an_empty_config_still_load():
    from pathlib import Path

    from pengumon.config import load_config
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    assert load_config(str(example)).database.retention_days == 7
    empty = AppConfig()
    assert empty.alerts.min_severity == "warning" and empty.display.default_latency_range == "live"


def test_a_shared_setting_name_is_mapped_by_its_full_key():
    from pengumon.settings.plain import plain
    text = plain("degraded_loss_pct must be below down_loss_pct", "classifier")
    assert text == "“Loss counts as degraded from” must be below “Loss counts as down from”"
