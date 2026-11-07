"""The settings registry's contract (pengumon.settings.registry): every entry names a real field, every field of a
stored section is on the page or deliberately left off, the page order and the words are complete, and nothing in
the registry repeats a default or a range (those live on the pydantic fields only)."""

import dataclasses

import pytest

from pengumon.config import AppConfig
from pengumon.settings import env as settings_env
from pengumon.settings import registry
from pengumon.settings.registry import LISTS, NOT_ON_PAGE, SECRET_SLOTS, SECTION_IDS, SETTINGS, Setting

# What the settings inventory marks Expose or Advanced, as registry keys, plus the judging
# window (H8, a setting since 0.24: classifier.window_probes). prober.interval_seconds is a bootstrap key (design
# section 1), so it is not here.
INVENTORY_EXPOSED = {
    # config.yaml keys
    "server.log_level", "prober.timeout_seconds",
    *(f"prober.hop_status.{f}" for f in ("down_loss_fraction", "degraded_loss_fraction", "local_latency_ms",
                                         "local_jitter_ms", "gateway_latency_ms", "gateway_jitter_ms",
                                         "public_latency_ms", "public_jitter_ms", "dns_latency_ms", "dns_jitter_ms")),
    *(f"classifier.{f}" for f in ("local_latency_spike_threshold_ms", "local_jitter_spike_threshold_ms",
                                  "gateway_latency_spike_threshold_ms", "gateway_jitter_spike_threshold_ms",
                                  "public_latency_spike_threshold_ms", "public_jitter_spike_threshold_ms",
                                  "dns_timeout_threshold_ms", "consecutive_failures_for_incident",
                                  "consecutive_failures_for_latency_incident", "busy_load_pct", "path_trace_target")),
    "database.retention_days", "database.rollup_retention_days",
    "line.down_mbps", "line.up_mbps", "line.isp_name", "privacy.client_details",
    "service_checks.interval_seconds", "service_checks.failures_for_incident", "service_checks.notify_after_seconds",
    "notifications.public_url", "dashboard.order", "dashboard.hidden", "discovery.background", "discovery.asn_lookup",
    # environment variables that become settings
    "server.log_levels", "display.grafana_url",
    # hard-coded values H1-H3, H5, H7, H11, H13, H14, H18, H22-H24, H26-H30, H32, H33, H42
    "classifier.down_loss_pct", "classifier.degraded_loss_pct", "classifier.dns_degraded_loss_pct",
    "classifier.dns_cooldown_seconds", "classifier.consecutive_successes_for_recovery",
    "classifier.isp_latency_spike_threshold_ms", "uptime.outage_dead_minutes", "uptime.degraded_loss_pct",
    "reports.material_loss_points", "prober.dns_timeout_seconds", "router_levels.wan_critical_pct",
    "router_levels.cpu_warning_pct", "router_levels.mem_warning_pct", "router_levels.mem_critical_pct",
    *(f"line_signal.{f}" for f in ("ds_power_warning_abs_dbmv", "ds_snr_warning_db", "ds_snr_critical_db",
                                   "us_power_warning_dbmv", "us_power_critical_dbmv", "us_power_low_warning_dbmv",
                                   "us_power_low_critical_dbmv", "uncorrectable_ratio_warning",
                                   "uncorrectable_ratio_critical", "uncorrectable_warning_per_minute",
                                   "uncorrectable_critical_per_minute")),
    "alerts.repeat_window_seconds", "alerts.min_severity", "alerts.delay_seconds", "alerts.quiet_hours.enabled",
    "alerts.quiet_hours.start", "alerts.quiet_hours.end", "alerts.quiet_hours.critical_still",
    "discovery.daily_check_time", "display.default_latency_range",
    # themes (0.24)
    "display.theme", "display.effects", "dashboard.colours",
    # the judging window (0.24)
    "classifier.window_probes",
}
INVENTORY_LISTS = {"devices", "services", "webhooks", "dns_probes", "themes"}
INVENTORY_SECRETS = {"ntfy.url", "ntfy.token"}


def test_every_registry_key_resolves_to_a_field():
    for setting in SETTINGS:
        model, name, info = setting.resolve()
        assert name in model.model_fields and info is model.model_fields[name], setting.key


def test_a_key_that_names_no_field_is_refused():
    with pytest.raises(KeyError):
        registry._resolve("classifier.no_such_level")
    with pytest.raises(KeyError):
        registry._resolve("classifier.busy_load_pct.deeper")


def test_every_field_of_a_stored_section_is_on_the_page_or_listed_as_left_off():
    """Fails when a section model gains a field and nobody decided whether the page shows it."""
    assert registry.unplaced_fields() == [], "add these to the registry, LISTS or NOT_ON_PAGE"
    leaves = registry.stored_leaf_paths()
    assert set(NOT_ON_PAGE) <= set(leaves)  # nothing listed points at a field that is gone
    assert {spec.path for spec in LISTS.values()} <= set(leaves)


def test_a_section_that_gains_a_field_without_an_entry_is_named():
    from pydantic import create_model

    from pengumon.engine.classifier_settings import ClassifierConfig
    from pengumon.lines.levels import LineSignalConfig
    grown_classifier = create_model("GrownClassifier", __base__=ClassifierConfig, brand_new_level_ms=(float, 5.0))
    grown_signal = create_model("GrownSignal", __base__=LineSignalConfig, us_new_db=(float, 1.0))
    grown = create_model("GrownConfig", __base__=AppConfig, classifier=(grown_classifier, grown_classifier()),
                         line_signal=(grown_signal, grown_signal()), brand_new_section=(int, 1))
    assert registry.unplaced_fields(grown) == ["classifier.brand_new_level_ms", "line_signal.us_new_db",
                                               "brand_new_section"]


def test_the_registry_covers_every_key_the_inventory_exposes():
    assert INVENTORY_EXPOSED - set(registry.BY_KEY) == set()
    assert set(LISTS) == INVENTORY_LISTS
    assert {s.slot for s in SECRET_SLOTS if not s.pattern} == INVENTORY_SECRETS


def test_bootstrap_keys_and_the_dead_key_are_not_on_the_page():
    for key in ("server.host", "server.port", "database.db_path", "prober.interval_seconds",
                "prober.ping_count_per_probe"):
        assert key in NOT_ON_PAGE and key not in registry.BY_KEY


def test_keys_are_unique_and_sections_known_and_ordered():
    keys = [s.key for s in SETTINGS]
    assert len(keys) == len(set(keys))
    for setting in SETTINGS:
        assert setting.section in SECTION_IDS, setting.key
        assert set(setting.also_in) <= set(SECTION_IDS) and setting.section not in setting.also_in
        assert setting.label.strip() and setting.effect.strip(), setting.key
        assert setting.advanced == (setting.section == "advanced")
        assert setting.related is None or setting.related in registry.BY_KEY, setting.key
    for section in SECTION_IDS:
        orders = [s.order for s in SETTINGS if s.section == section]
        assert orders == list(range(1, len(orders) + 1)), section
    assert [s.id for s in registry.SECTIONS] == ["outage", "alerts", "line", "devices", "probing", "retention",
                                                 "access", "privacy", "discovery", "display", "advanced"]


def test_asn_lookup_is_one_value_shown_in_privacy_and_discovery():
    assert [s.key for s in SETTINGS].count("discovery.asn_lookup") == 1
    assert "discovery.asn_lookup" in [s.key for s in registry.in_section("privacy")]
    assert "discovery.asn_lookup" in [s.key for s in registry.in_section("discovery")]


def test_every_env_name_is_one_of_the_six_override_variables():
    five = {"PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_GRAFANA_URL", "PENGUMON_NOTIFY_URL",
            "PENGUMON_NOTIFY_TOKEN", "PENGUMON_VIEW_WITHOUT_TOKEN"}
    used = {s.env for s in SETTINGS if s.env} | {s.env for s in SECRET_SLOTS if s.env}
    assert used == five == set(settings_env.OVERRIDE_VARIABLES)
    assert {s.key: s.env for s in SETTINGS if s.env} == settings_env.SETTING_ENV


def test_no_setting_repeats_a_default_a_type_or_a_range():
    fields = {f.name for f in dataclasses.fields(Setting)}
    assert fields.isdisjoint({"default", "type", "min", "max", "ge", "gt", "le", "lt", "choices", "nullable",
                              "step", "max_length"})


def test_constraints_and_defaults_come_from_the_fields():
    retention = registry.get("database.retention_days")
    assert retention.default() == 7
    found = retention.constraints()
    assert (found["type"], found["min"], found["max"], found["step"], found["nullable"]) == ("integer", 1, 90, 1, False)
    assert registry.get("database.rollup_retention_days").constraints()["min"] == 30
    assert registry.get("privacy.client_details").constraints()["choices"] == ["token", "full", "masked"]
    isp = registry.get("classifier.isp_latency_spike_threshold_ms")
    assert isp.default() is None and isp.constraints()["nullable"] is True
    assert registry.get("alerts.quiet_hours.start").constraints()["type"] == "time"
    assert registry.get("general.timezone").constraints()["type"] == "timezone"
    assert registry.get("dashboard.order").constraints()["type"] == "layout"
    assert registry.get("discovery.background").constraints()["type"] == "boolean"
    hop = registry.get("prober.hop_status.down_loss_fraction")
    assert hop.scale == 100 and hop.unit == "%" and hop.constraints()["max"] == 1
    assert registry.get("classifier.consecutive_failures_for_incident").rounds is True


def test_the_page_never_shows_a_secret_as_a_setting():
    assert not any("token" in s.key or s.key.endswith(".url") and s.key != "display.grafana_url"
                   and s.key != "notifications.public_url" for s in SETTINGS)
    assert all(s.export for s in SETTINGS)
