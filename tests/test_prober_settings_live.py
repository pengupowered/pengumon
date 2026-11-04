"""The prober's settings: a hop's own "warn above" level (colour only), the DNS lookups list and the timeouts applied
without a restart, and the line and router levels the probing component puts in force."""

import socket
import time

import pytest

from pengumon.config import AppConfig
from pengumon.devices import monitors
from pengumon.devices.levels import RouterLevelsConfig
from pengumon.engine import probing
from pengumon.engine.prober import NetworkProber
from pengumon.engine.prober_settings import DnsProbeConfig, HopStatusConfig
from pengumon.lines import docsis
from pengumon.lines.levels import LineSignalConfig
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.settings.document import SettingsDocument
from pengumon.settings.hub import Change, SettingsHub


def _feed(prober, target_id, latencies):
    for value in latencies:
        prober._update_target_stats(ProbeSample(target_id=target_id, success=value is not None, latency_ms=value))
    return prober.stats[target_id].status


def _hop(category, warn=None, tid="far"):
    return TargetConfig(id=tid, name="Far", category=category, address="192.0.2.9", warn_latency_ms=warn)


# --- the per-hop warn level ---------------------------------------------------------------------------------------

def test_a_public_hop_turns_amber_above_its_own_level_instead_of_the_roles():
    prober = NetworkProber()
    prober.register_target(_hop("public", warn=150.0))
    assert _feed(prober, "far", [120.0] * 5) == "up"          # above the role's 60 ms, below its own 150
    assert _feed(prober, "far", [200.0] * 20) == "degraded"   # above its own


def test_the_override_keeps_the_roles_jitter_limit():
    prober = NetworkProber()
    prober.register_target(_hop("public", warn=500.0))
    assert _feed(prober, "far", [100.0, 140.0] * 5) == "degraded"  # jitter 40 > the role's 25


def test_an_isp_hop_without_role_limits_gets_a_latency_only_limit():
    def isp(latencies, category="isp"):
        prober = NetworkProber()
        prober.register_target(_hop(category, warn=50.0))
        return _feed(prober, "far", latencies)

    assert isp([40.0, 45.0] * 3) == "up"
    assert isp([90.0, 10.0] * 5) == "up"       # avg 50 is not above 50, and the jitter (80) is not judged
    assert isp([120.0] * 3) == "degraded"
    assert isp([120.0] * 3, category="custom") == "degraded"


def test_without_an_override_isp_hops_stay_judged_on_loss_only():
    prober = NetworkProber()
    prober.register_target(_hop("isp"))
    assert _feed(prober, "far", [900.0] * 5) == "up"


def test_loss_still_wins_over_the_override():
    prober = NetworkProber()
    prober.register_target(_hop("public", warn=5000.0))
    assert _feed(prober, "far", [None] * 5 + [10.0] * 5) == "down"


def test_the_override_follows_the_target_each_round_and_on_a_path_edit():
    prober = NetworkProber()
    prober.register_target(_hop("public", warn=150.0))
    assert _feed(prober, "far", [120.0] * 10) == "up"
    prober.register_target(_hop("public"))  # the probe round re-registers every target with its current config
    assert _feed(prober, "far", [120.0]) == "degraded"
    prober.set_targets([_hop("public", warn=150.0)])
    assert _feed(prober, "far", [120.0]) == "up"
    prober.set_targets([_hop("public")])
    assert _feed(prober, "far", [120.0]) == "degraded"


def test_the_override_is_colour_only_the_classifier_still_judges_the_role_average():
    """The classifier reads recent_latencies, which the override does not touch (the owner's decision 1)."""
    from pengumon.engine.classifier.metrics import round_metrics
    prober = NetworkProber()
    prober.register_target(_hop("public", warn=500.0))
    _feed(prober, "far", [120.0] * 10)
    assert prober.stats["far"].status == "up"
    assert round_metrics(prober.stats, []).public.avg_latency_ms == 120.0


# --- the DNS lookups list -----------------------------------------------------------------------------------------

def _dns(tid, **kw):
    return DnsProbeConfig(id=tid, name=kw.pop("name", tid.upper()), **kw)


def test_set_dns_targets_keeps_unchanged_stats_and_resets_changed_lookups():
    prober = NetworkProber()
    prober.register_target(_hop("public", tid="pub"))
    for d in (_dns("a"), _dns("b", domain="example.com"), _dns("c", nameserver="192.0.2.53")):
        prober.register_dns_target(d)
    for tid in ("pub", "a", "b", "c"):
        _feed(prober, tid, [10.0] * 3)
        prober.last_samples[tid] = ProbeSample(target_id=tid, success=True, latency_ms=10.0)
    prober.set_dns_targets([
        _dns("c", nameserver="192.0.2.54"),                       # another nameserver: starts over
        _dns("a", name="Renamed", enabled=False, color="#123456"),  # same lookup: keeps its statistics
        _dns("new", domain="example.org"),
        # "b" removed
    ])
    assert list(prober.stats) == ["pub", "c", "a", "new"]  # the ICMP targets first, then the list in order
    assert prober.stats["pub"].sent == 3
    a = prober.stats["a"]
    assert (a.sent, a.name, a.enabled, a.color) == (3, "Renamed", False, "#123456")
    assert prober.stats["c"].sent == 0 and prober.stats["c"].address == "google.com @192.0.2.54"
    assert prober.stats["new"].sent == 0
    assert set(prober.last_samples) == {"pub", "a"}


def test_a_changed_record_type_is_another_lookup():
    prober = NetworkProber()
    prober.register_dns_target(_dns("a", nameserver="192.0.2.53"))
    _feed(prober, "a", [10.0] * 3)
    prober.set_dns_targets([_dns("a", nameserver="192.0.2.53", record_type="AAAA")])
    assert prober.stats["a"].sent == 0
    prober.set_dns_targets([_dns("a", nameserver="192.0.2.53", record_type="AAAA", name="Same")])
    assert prober.stats["a"].name == "Same"


def test_a_probe_round_between_the_save_and_the_applier_does_not_hide_the_change():
    """The manager updates config.dns_probes in place before the applier runs; a probe round in between registers
    the edited lookup. Its old statistics must still start over."""
    prober = NetworkProber()
    prober.register_dns_target(_dns("a"))
    _feed(prober, "a", [10.0] * 3)
    edited = _dns("a", domain="example.org")
    prober.register_dns_target(edited)  # what execute_probe_round does with the live list
    prober.set_dns_targets([edited])
    assert prober.stats["a"].sent == 0


def test_a_kept_lookup_is_still_compared_at_the_next_change():
    prober = NetworkProber()
    prober.register_dns_target(_dns("a"))
    _feed(prober, "a", [10.0] * 3)
    prober.set_dns_targets([_dns("a", name="Renamed")])
    assert prober.stats["a"].sent == 3
    prober.set_dns_targets([_dns("a", domain="example.org")])
    assert prober.stats["a"].sent == 0


def test_a_dns_probe_dropped_by_a_path_edit_leaves_a_hop_of_that_id_alone():
    prober = NetworkProber()
    prober.register_dns_target(_dns("x"))
    prober.set_targets([_hop("public", tid="x")], dns_ids=[])  # the DNS probe is gone, a hop took its id
    _feed(prober, "x", [10.0] * 2)
    prober.set_dns_targets([_dns("a")])
    assert list(prober.stats) == ["x", "a"] and prober.stats["x"].sent == 2 and prober.stats["x"].category == "public"


def test_a_path_edit_after_a_dns_change_keeps_the_dns_stats():
    prober = NetworkProber()
    prober.register_target(_hop("public", tid="pub"))
    prober.register_dns_target(_dns("a"))
    prober.set_dns_targets([_dns("a"), _dns("b")])
    _feed(prober, "b", [10.0] * 2)
    prober.set_targets([_hop("gateway", tid="gw")], dns_ids=["a", "b"])
    assert list(prober.stats) == ["gw", "a", "b"] and prober.stats["b"].sent == 2


# --- the timeouts ----------------------------------------------------------------------------------------------------

async def test_a_dns_lookup_uses_its_own_timeout_when_set(monkeypatch):
    def slow(*args, **kwargs):
        time.sleep(0.3)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    probe = _dns("a")  # no nameserver: the system resolver path
    assert NetworkProber(ping_timeout=1.0).lookup_timeout == 1.0
    assert (await NetworkProber(ping_timeout=1.0).probe_dns(probe)).success is True
    quick = NetworkProber(ping_timeout=1.0, dns_timeout=0.05)
    assert quick.lookup_timeout == 0.05
    sample = await quick.probe_dns(probe)
    assert sample.success is False and sample.error == "DNS Resolution Timeout"


# --- the appliers ------------------------------------------------------------------------------------------------------

@pytest.fixture
def restore_levels():
    line, router = docsis.LEVELS.model_copy(), monitors.LEVELS.model_copy()
    yield
    docsis.use_levels(line)
    monitors.use_levels(router)


def _wired(config):
    hub = SettingsHub()
    prober = NetworkProber(ping_timeout=config.prober.timeout_seconds, status_thresholds=config.prober.hop_status,
                           dns_timeout=config.prober.dns_timeout_seconds)
    for d in config.dns_probes:
        prober.register_dns_target(d)
    probing.subscribe(hub, config, prober)
    return hub, prober


def _set(section, **values):
    """What the settings manager does before it publishes: the new values copied into the live section in place."""
    for name, value in values.items():
        setattr(section, name, value)


async def test_the_timeouts_apply_without_a_restart():
    config = AppConfig()
    hub, prober = _wired(config)
    _set(config.prober, timeout_seconds=0.5, dns_timeout_seconds=2.0)
    failed = await hub.publish(Change.of(keys=["prober.timeout_seconds", "prober.dns_timeout_seconds"]))
    assert failed == [] and (prober.ping_timeout, prober.dns_timeout, prober.lookup_timeout) == (0.5, 2.0, 2.0)
    _set(config.prober, dns_timeout_seconds=None)
    await hub.publish(Change.of(keys=["prober.dns_timeout_seconds"]))
    assert prober.lookup_timeout == 0.5


async def test_a_saved_dns_list_reaches_the_prober():
    config = AppConfig(dns_probes=[_dns("a"), _dns("b")])
    hub, prober = _wired(config)
    _feed(prober, "a", [10.0] * 4)
    config.dns_probes[:] = [_dns("a"), _dns("z", domain="example.org")]
    assert await hub.publish(Change.of(lists=["dns_probes"])) == []
    assert list(prober.stats) == ["a", "z"] and prober.stats["a"].sent == 4


async def test_the_line_and_router_levels_apply_in_place(restore_levels):
    config = AppConfig()
    held_line, held_router = docsis.LEVELS, monitors.LEVELS
    probing.use_levels(config)
    hub, _ = _wired(config)
    _set(config.line_signal, ds_snr_warning_db=36.0)
    _set(config.router_levels, cpu_warning_pct=70.0)
    assert await hub.publish(Change.of(keys=["line_signal.ds_snr_warning_db", "router_levels.cpu_warning_pct"])) == []
    assert docsis.LEVELS is held_line and docsis.LEVELS.ds_snr_warning_db == 36.0
    assert monitors.LEVELS is held_router and monitors.LEVELS.cpu_warning_pct == 70.0
    assert docsis.LEVELS is not config.line_signal  # a copy: the live config section is the manager's


async def test_the_appliers_subscribe_to_what_they_apply():
    config = AppConfig()
    hub, _ = _wired(config)
    assert hub.names == ["probing.timeouts", "probing.dns_probes", "probing.levels"]
    assert hub.subscribers(Change.of(keys=["prober.hop_status.local_latency_ms"])) == []  # live through the config
    assert hub.subscribers(Change.of(keys=["line_signal.ds_snr_critical_db"])) == ["probing.levels"]


async def test_the_hop_colours_are_live_through_the_shared_config():
    config = AppConfig()
    _, prober = _wired(config)
    prober.register_target(_hop("local", tid="lan"))
    assert prober.status_thresholds is config.prober.hop_status
    assert _feed(prober, "lan", [15.0] * 3) == "up"
    config.prober.hop_status.local_latency_ms = 10.0
    assert _feed(prober, "lan", [15.0]) == "degraded"


async def test_the_probing_component_binds_the_levels_and_subscribes(tmp_path, restore_levels, monkeypatch):
    from types import SimpleNamespace

    config = AppConfig(line_signal=LineSignalConfig(ds_snr_warning_db=35.0),
                       router_levels=RouterLevelsConfig(cpu_warning_pct=75.0))

    class Path:
        def __init__(self, config, repository):
            self.previews = None

        async def load(self):
            pass

    monkeypatch.setattr(probing, "PathManager", Path)
    hub = SettingsHub()
    ctx = SimpleNamespace(config=config, state=SimpleNamespace(repository=None), settings=hub)
    await probing.register(ctx)
    assert docsis.LEVELS.ds_snr_warning_db == 35.0 and monitors.LEVELS.cpu_warning_pct == 75.0
    assert "probing.timeouts" in hub.names
    assert ctx.state.prober.status_thresholds is config.prober.hop_status

    without_hub = SimpleNamespace(config=AppConfig(), state=SimpleNamespace(repository=None))
    await probing.register(without_hub)  # before the settings component exists (no Context.settings): no appliers
