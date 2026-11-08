"""The live-apply table (design 9.1): every registry key and every list is saved with a non-default value through
PUT /api/settings on the running app (the real lifespan, the real components), and an observer reads the effect from
the running parts with no restart. A key or list without an entry fails, so a new setting can't skip the check.

The observers read the part that uses the value. Where that part keeps the live config object (the classifier, the
prober's hop colors, the reports) the in-place copy is the whole apply; the retention days, the log levels and the
Grafana links have appliers (settings/component.py); the probe timeouts and the DNS lookups have the probing
component's (engine/probing.py), observed on the prober; the line and router levels are read from the live config by
the device monitors at each poll (devices/monitors.py watch_levels), observed on the levels a monitor would use.
the alert rules, the notifier's channels, the host-app checks and the route checks are observed on the notifier, the
service checker and the drift checker (their appliers: notify.reconfigure, services.checks, discovery.drift)."""

import asyncio
import logging
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import main, security
from pengumon.state import STATE_ATTR
from pengumon.db import retention
from pengumon.devices import monitors
from pengumon.engine import probing
from pengumon.lines import docsis
from pengumon.observability import grafana
from pengumon.reports.common import material_loss_points
from pengumon.reports.uptime import uptime_levels
from pengumon.services import component as services_component
from pengumon.settings import registry
from pengumon.settings.importer import get_path
from pengumon.timeofday import clock_text, zone


async def _idle(*args, **kwargs):
    await asyncio.sleep(3600)


def cfg(key):
    return lambda st: get_path(st.config, key)


def classifier(name):
    return lambda st: getattr(st.classifier.config, name)


def speed(name):
    """The speed test section the running scheduler reads at every tick (the live config object)."""
    return lambda st: getattr(st.speedtest.cfg, name)


def hop(name):
    return lambda st: getattr(st.prober.status_thresholds, name)


def _monitor(kind, config):
    """A monitor handed the running app's config, as the device supervisor hands it to every device it builds."""
    monitor = kind(messages=monitors.UnconfiguredMessages(disabled="", missing_credentials=""))
    monitor.watch_levels(config)
    return monitor


def line_level(name):
    """The level a line monitor of this app grades by (its config's line_signal, read at each poll)."""
    return lambda st: getattr(_monitor(monitors.LineMonitor, st.config).levels, name)


def router_level(name):
    """The level a router monitor of this app grades by (its config's router_levels, read at each poll)."""
    ranges = {"cpu_warning_pct": ("cpu", "good_high"), "mem_warning_pct": ("mem", "good_high"),
              "mem_critical_pct": ("mem", "critical_high"), "wan_critical_pct": ("wan_rx", "critical_high")}
    metric, edge = ranges[name]
    return lambda st: _monitor(monitors.RouterMonitor, st.config).ranges[metric].as_dict()[edge]


def uptime(name):
    """The level build_uptime uses for a request on this app (it gets state.config)."""
    return lambda st: getattr(uptime_levels(st.config), name)


def policy(name):
    """The rule the notifier's AlertPolicy holds (replaced by notify.reconfigure)."""
    return lambda st: getattr(st.notifier.policy, name)


def quiet(name):
    return lambda st: getattr(st.notifier.policy.quiet, name)


def checker(name):
    """The level the running service checker reads (its section, updated in place; services.checks applier)."""
    return lambda st: getattr(st.service_checker.config, name)


def daily_check(st):
    """The time of day the drift checker's daily loop works out for its next check."""
    return clock_text(st.drift.next_daily(time.time()), zone(st.config.general.timezone))


def timezones(st):
    """The zone the quiet hours (the notifier's policy) and the daily route check use; None when they disagree."""
    zone_name = st.notifier.policy.timezone
    return zone_name if st.drift._timezone() == zone_name else None


def root_level(st):
    return logging.getLevelName(logging.getLogger().level).lower()


def devices_level(st):
    return f"pengumon.devices={logging.getLevelName(logging.getLogger('pengumon.devices').level).lower()}"


def grafana_base(st):
    links = grafana.links()
    return links[0]["url"].split("/d/")[0] if links else None


# key -> (value to save, observer of the running app); values chosen so that, saved one after another, every
# cross-field rule holds
VALUES = {
    "classifier.degraded_loss_pct": (20, classifier("degraded_loss_pct")),
    "classifier.down_loss_pct": (50, classifier("down_loss_pct")),
    "classifier.consecutive_failures_for_incident": (5, classifier("consecutive_failures_for_incident")),
    "classifier.consecutive_failures_for_latency_incident": (12, classifier(
        "consecutive_failures_for_latency_incident")),
    "classifier.consecutive_successes_for_recovery": (4, classifier("consecutive_successes_for_recovery")),
    "classifier.local_latency_spike_threshold_ms": (30, classifier("local_latency_spike_threshold_ms")),
    "classifier.gateway_latency_spike_threshold_ms": (40, classifier("gateway_latency_spike_threshold_ms")),
    "classifier.isp_latency_spike_threshold_ms": (120, classifier("isp_latency_spike_threshold_ms")),
    "classifier.public_latency_spike_threshold_ms": (90, classifier("public_latency_spike_threshold_ms")),
    "classifier.local_jitter_spike_threshold_ms": (18, classifier("local_jitter_spike_threshold_ms")),
    "classifier.gateway_jitter_spike_threshold_ms": (22, classifier("gateway_jitter_spike_threshold_ms")),
    "classifier.public_jitter_spike_threshold_ms": (50, classifier("public_jitter_spike_threshold_ms")),
    "classifier.dns_timeout_threshold_ms": (400, classifier("dns_timeout_threshold_ms")),
    # the classifier reads its config each round; the prober's hop colors get it through the probing.window applier
    "classifier.window_probes": (20, lambda st: st.prober.window),
    "notifications.public_url": ("https://pengumon.example.com", lambda st: st.notifier.public_url),
    "alerts.min_severity": ("critical", policy("min_severity")),
    "alerts.delay_seconds": (120, policy("delay_seconds")),
    "alerts.repeat_window_seconds": (600, policy("repeat_window_seconds")),
    "alerts.quiet_hours.enabled": (True, quiet("enabled")),
    "alerts.quiet_hours.start": ("23:00", quiet("start")),
    "alerts.quiet_hours.end": ("06:30", quiet("end")),
    "alerts.quiet_hours.critical_still": (False, quiet("critical_still")),
    "service_checks.notify_after_seconds": (120, checker("notify_after_seconds")),
    "line.isp_name": ("Example ISP", cfg("line.isp_name")),
    "line.down_mbps": (500, lambda st: st.classifier.load.down_mbps),  # the line load reads config.line live
    "line.up_mbps": (20, lambda st: st.classifier.load.up_mbps),
    "classifier.busy_load_pct": (70, classifier("busy_load_pct")),
    "service_checks.interval_seconds": (15, checker("interval_seconds")),
    "service_checks.failures_for_incident": (4, checker("failures_for_incident")),
    "classifier.path_trace_target": ("198.51.100.7", classifier("path_trace_target")),
    "database.retention_days": (14, lambda st: st.repository.retention_days),
    "database.rollup_retention_days": (500, lambda st: st.repository.rollup_retention_days),
    "privacy.client_details": ("masked", cfg("privacy.client_details")),
    "discovery.background": (False, lambda st: st.drift.enabled),
    "discovery.asn_lookup": (True, lambda st: st.drift.options.asn_lookup),
    "discovery.daily_check_time": ("04:15", daily_check),
    "general.timezone": ("Europe/Berlin", timezones),
    "display.default_latency_range": ("1h", cfg("display.default_latency_range")),
    "dashboard.order": (["matrix", "line"], cfg("dashboard.order")),
    "dashboard.hidden": (["report"], cfg("dashboard.hidden")),
    "dashboard.colors": ({"latency": {"theme": "nord", "custom": {}}}, lambda st: st.config.dashboard.model_dump()["colors"]),
    "display.theme": ("nord", cfg("display.theme")),
    "display.effects": (False, cfg("display.effects")),
    "display.grafana_url": ("https://grafana.example.com:3000", grafana_base),
    "prober.hop_status.down_loss_fraction": (0.5, hop("down_loss_fraction")),
    "prober.hop_status.degraded_loss_fraction": (0.2, hop("degraded_loss_fraction")),
    "prober.hop_status.local_latency_ms": (22, hop("local_latency_ms")),
    "prober.hop_status.local_jitter_ms": (12, hop("local_jitter_ms")),
    "prober.hop_status.gateway_latency_ms": (33, hop("gateway_latency_ms")),
    "prober.hop_status.gateway_jitter_ms": (16, hop("gateway_jitter_ms")),
    "prober.hop_status.public_latency_ms": (66, hop("public_latency_ms")),
    "prober.hop_status.public_jitter_ms": (26, hop("public_jitter_ms")),
    "prober.hop_status.dns_latency_ms": (110, hop("dns_latency_ms")),
    "prober.hop_status.dns_jitter_ms": (44, hop("dns_jitter_ms")),
    "classifier.dns_degraded_loss_pct": (30, classifier("dns_degraded_loss_pct")),
    "classifier.dns_cooldown_seconds": (60, classifier("dns_cooldown_seconds")),
    "prober.timeout_seconds": (0.8, lambda st: st.prober.ping_timeout),
    "prober.dns_timeout_seconds": (2.0, lambda st: st.prober.dns_timeout),
    "uptime.outage_dead_minutes": (3, uptime("outage_dead_minutes")),
    "uptime.degraded_loss_pct": (2.0, uptime("degraded_loss_pct")),
    "reports.material_loss_points": (1.0, lambda st: material_loss_points(st.config)),
    "line_signal.ds_power_warning_abs_dbmv": (9, line_level("ds_power_warning_abs_dbmv")),
    "line_signal.ds_snr_warning_db": (34, line_level("ds_snr_warning_db")),
    "line_signal.ds_snr_critical_db": (31, line_level("ds_snr_critical_db")),
    "line_signal.us_power_warning_dbmv": (50, line_level("us_power_warning_dbmv")),
    "line_signal.us_power_critical_dbmv": (52, line_level("us_power_critical_dbmv")),
    "line_signal.us_power_low_warning_dbmv": (39, line_level("us_power_low_warning_dbmv")),
    "line_signal.us_power_low_critical_dbmv": (36, line_level("us_power_low_critical_dbmv")),
    "line_signal.uncorrectable_ratio_warning": (2e-6, line_level("uncorrectable_ratio_warning")),
    "line_signal.uncorrectable_ratio_critical": (2e-4, line_level("uncorrectable_ratio_critical")),
    "line_signal.uncorrectable_warning_per_minute": (70, line_level("uncorrectable_warning_per_minute")),
    "line_signal.uncorrectable_critical_per_minute": (600, line_level("uncorrectable_critical_per_minute")),
    "router_levels.cpu_warning_pct": (80, router_level("cpu_warning_pct")),
    "router_levels.mem_warning_pct": (80, router_level("mem_warning_pct")),
    "router_levels.mem_critical_pct": (90, router_level("mem_critical_pct")),
    "router_levels.wan_critical_pct": (96, router_level("wan_critical_pct")),
    "server.log_level": ("warning", root_level),
    "server.log_levels": ("pengumon.devices=debug", devices_level),
    "access.allow_viewers": (True, lambda st: main.security_config.view_without_token),
    "mcp.enabled": (True, cfg("mcp.enabled")),
    "speedtest.enabled": (True, speed("enabled")),
    "speedtest.provider": ("mlab", speed("provider")),
    "speedtest.librespeed_url": ("http://speed.example.com", speed("librespeed_url")),
    "speedtest.interval_hours": (6, speed("interval_hours")),
    "speedtest.window_start": ("01:00", speed("window_start")),
    "speedtest.window_end": ("05:30", speed("window_end")),
    "speedtest.download_mb": (50, speed("download_mb")),
    "speedtest.upload_mb": (25, speed("upload_mb")),
    "speedtest.monthly_cap_mb": (5000, speed("monthly_cap_mb")),
    "speedtest.busy_load_pct": (35, speed("busy_load_pct")),
    "speedtest.slow_alert_pct": (40, speed("slow_alert_pct")),
}
# values a key can only be saved with (the window's two ends go together)
WITH = {"speedtest.window_start": {"speedtest.window_end": "04:00"}}

def _custom_theme() -> dict:
    """A custom theme made from the built-in Nord, as Settings' Duplicate makes one."""
    from pengumon.web.themes import builtin_themes
    nord = next(t for t in builtin_themes() if t.id == "nord").exported()
    return nord | {"id": "custom-test", "name": "Test"}


# list -> (items, secrets to save with them, observer, expected)
LISTS = {
    "devices": ([{"id": "wifi", "driver": "asuswrt", "url": "http://192.0.2.1", "enabled": False}], {},
                lambda st: ([d.id for d in st.devices], [d.id for d in st.config.devices]),  # the supervisor rebuilt the registry
                (["wifi"], ["wifi"])),
    "services": ([{"id": "s1", "name": "S1", "url": "http://192.0.2.10/health"}], {},
                 lambda st: ([s.id for s in st.service_checker.services], sorted(st.service_checker._applied)),
                 (["s1"], ["s1"])),  # the checker's set_services ran (services.checks)
    "webhooks": ([{"name": "Hook", "format": "slack"}], {"webhook.Hook.url": "https://hooks.example.com/x"},
                 lambda st: [(w.name, w.host) for w in st.notifier.webhooks],
                 [("Hook", "hooks.example.com")]),  # the notifier's sender, its URL from the slot (notify.reconfigure)
    "dns_probes": ([{"id": "dns_x", "name": "X", "domain": "example.com"}], {},
                   lambda st: [i for i, t in st.prober.stats.items() if t.category == "dns"], ["dns_x"]),
    "themes": ([_custom_theme()], {}, lambda st: [t.id for t in st.config.display.themes], ["custom-test"]),
}

# secret slot -> (value, observer, expected): the ntfy slots reach the notifier (notify.reconfigure)
SECRETS = {
    "ntfy.url": ("https://ntfy.example.com/pengumon-test", lambda st: st.notifier.url,
                 "https://ntfy.example.com/pengumon-test"),
    "ntfy.token": ("tk_made_up_for_the_test", lambda st: st.notifier._token, "tk_made_up_for_the_test"),
}


def test_every_registry_key_and_list_has_an_observer():
    assert set(VALUES) == set(registry.BY_KEY)
    assert set(LISTS) == set(registry.LISTS)
    for key, (value, _) in VALUES.items():
        assert value != registry.get(key).default(), key


@pytest.fixture
def _restored():
    level, devices = logging.getLogger().level, logging.getLogger("pengumon.devices").level
    httpx = logging.getLogger("httpx").level
    yield
    logging.getLogger().setLevel(level)
    logging.getLogger("pengumon.devices").setLevel(devices)
    logging.getLogger("httpx").setLevel(httpx)
    grafana.configure_url(None)


def _start(tmp_path, monkeypatch, extra: str = "") -> list:
    """The real lifespan's environment: a config file in tmp_path, the workers idle. Returns the list that receives
    the probe loop's live parts when it starts."""
    config = tmp_path / "config.yaml"
    config.write_text("database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
                      "targets:\n  - {id: gw, name: Gateway, address: 192.168.88.1, category: gateway}\n" + extra,
                      encoding="utf-8")
    monkeypatch.setenv("PENGUMON_CONFIG", str(config))
    for name in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_GRAFANA_URL", "PENGUMON_ACCESS_TOKEN",
                 "PENGUMON_NOTIFY_URL", "PENGUMON_NOTIFY_TOKEN", "PENGUMON_VIEW_WITHOUT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main.security_config, "access_token", "")
    monkeypatch.setattr(main.security_config, "view_without_token", False)
    monkeypatch.setattr(main.security_config, "allowed_hosts", [])
    monkeypatch.setattr(main.security_config, "allowed_origins", [])
    parts: list = []

    async def loop(*args, live_parts=(), **kwargs):
        parts.extend(live_parts)
        await asyncio.sleep(3600)

    monkeypatch.setattr(probing, "probe_background_worker", loop)
    monkeypatch.setattr(retention, "cleanup_background_worker", _idle)
    monkeypatch.setattr(services_component, "collector_background_worker", _idle)
    return parts


async def test_every_setting_applies_live_without_a_restart(tmp_path, monkeypatch, _restored):
    parts = _start(tmp_path, monkeypatch)
    async with main.lifespan(main.app):
        state = getattr(main.app.state, STATE_ATTR)
        state.secret_store.path = tmp_path / "secrets.json"  # never the data directory of the machine
        assert state.classifier.config is state.config.classifier
        assert state.prober.status_thresholds is state.config.prober.hop_status
        (revision_part,) = [p for p in parts if p.name == "settings_revision"]
        assert revision_part.value(None) == 1  # the live message tells open pages the revision
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
            for key, (value, observe) in VALUES.items():
                security.reset_rate_limits()
                res = await ac.put("/api/settings", json={"base_revision": state.settings.revision,
                                                          "values": {key: value, **WITH.get(key, {})}})
                assert res.status_code == 200, (key, res.text)
                assert res.json()["live_failed"] == [], key
                assert observe(state) == value, key
            for name, (items, secrets, observe, expected) in LISTS.items():
                security.reset_rate_limits()
                res = await ac.put("/api/settings", json={"base_revision": state.settings.revision,
                                                          "lists": {name: items}, "secrets": secrets})
                assert res.status_code == 200, (name, res.text)
                assert observe(state) == expected, name
            for slot, (value, observe, expected) in SECRETS.items():
                security.reset_rate_limits()
                res = await ac.put("/api/settings", json={"base_revision": state.settings.revision,
                                                          "secrets": {slot: value}})
                assert res.status_code == 200, (slot, res.text)
                assert res.json()["live_failed"] == [], slot
                assert observe(state) == expected, slot
        assert revision_part.value(None) == state.settings.revision == 1 + len(VALUES) + len(LISTS) + len(SECRETS)


async def test_a_config_value_out_of_the_new_ranges_does_not_stop_the_start(tmp_path, monkeypatch, _restored):
    _start(tmp_path, monkeypatch, "database: {retention_days: 0}\nclassifier: {busy_load_pct: 5}\n"
                                  "line: {isp_name: Example ISP}\n")
    async with main.lifespan(main.app):
        state = getattr(main.app.state, STATE_ATTR)
        assert state.repository.retention_days == 7 and state.config.classifier.busy_load_pct == 60
        assert state.config.line.isp_name == "Example ISP"
        assert [r["key"] for r in state.settings.import_report()] == ["classifier.busy_load_pct",
                                                                       "database.retention_days"]


def test_apply_levels_resets_a_logger_the_spec_no_longer_names(_restored):
    from pengumon.observability.logs import apply_levels
    apply_levels("warning", "pengumon.devices=debug,httpx=error")
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("pengumon.devices").level == logging.DEBUG
    apply_levels("info", None)
    assert logging.getLogger("pengumon.devices").level == logging.NOTSET
    assert logging.getLogger("httpx").level == logging.WARNING  # back to its default, not left at error
