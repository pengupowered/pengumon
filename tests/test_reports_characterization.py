"""Characterization of reports.build_dossier (and one build_report scenario), recorded before the dossier was split
into fact collection and rendering.

Every scenario feeds build_dossier a fake repository and line history returning fixed data (the four methods it
calls), a fixed `now` and a UTC clock (the Markdown formats times with datetime.fromtimestamp), and stores the whole
payload with the Markdown split into lines, plus the calls. The result is compared with
tests/golden/dossier_characterization/<scenario>.json, one file per scenario.

Regenerate only on purpose: PENGUMON_REGEN_GOLDEN=1 python -m pytest tests/test_reports_characterization.py
"""

import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.reports.dossier import build_dossier
from pengumon.reports.reliability import build_report

from golden_files import check_golden
from platform_helpers import skip_without_tzset

NOW = 1_760_000_000.0  # 2025-10-09 08:53:20 UTC


# --- data helpers ------------------------------------------------------------------------------------------

def target(tid, name, category, address=""):
    return SimpleNamespace(id=tid, name=name, category=category, address=address)


def cfg(targets=None, dns=None, busy: Optional[float] = None):
    config = SimpleNamespace(
        targets=targets if targets is not None else [
            target("router", "Local Router", "local", "192.168.1.1"),
            target("gateway", "Modem Gateway", "gateway", "192.168.100.1"),
            target("isp", "ISP Hub", "isp", "198.51.100.5"),
            target("cloudflare", "Cloudflare 1.1.1.1", "public", "1.1.1.1"),
        ],
        dns_probes=dns if dns is not None else [SimpleNamespace(id="dns_google", name="Google DNS")],
    )
    if busy is not None:
        config.classifier = SimpleNamespace(busy_load_pct=busy)
    return config


def hop(tid, sent=1000, lost=0, avg=10.0, mx=30.0):
    return {"target_id": tid, "sent": sent, "lost": lost, "loss_pct": round(lost / sent * 100.0, 2) if sent else 0.0,
            "avg_ms": avg, "max_ms": mx}


def hops(**losses):
    """The standard four hops plus the DNS probe with the given loss percentages (per 1000 probes)."""
    return [hop(t, lost=int(losses.get(t, 0) * 10)) for t in ("router", "gateway", "isp", "cloudflare", "dns_google")]


def incident_row(cause="unknown_outage", duration=60.0, expected=False, load=None):
    return {"id": 1, "start_time": NOW - 3600, "end_time": None, "duration_seconds": duration, "cause_code": cause,
            "title": "t", "description": "d", "severity": "warning", "affected_targets": "[]", "load_pct": load,
            "expected": 1 if expected else 0, "kind": "isp_loss"}


def ch(i, direction="downstream", power=1.0, snr=38.0, unc=0, freq=600.0, mod="QAM256", cor=100, recent=0):
    return DocsisChannel(channel_id=i, direction=direction, frequency_mhz=freq, power_dbmv=power, snr_db=snr,
                         modulation=mod, correcteds=cor, uncorrectables=unc, uncorrectables_recent=recent)


def snap(ts=NOW - 60, status="healthy", channels=(), total=5000):
    return DocsisSignalSnapshot(timestamp=ts, downstream_power_avg=1.2, downstream_snr_avg=37.8, upstream_power_avg=42.5,
                                uncorrectables_total=total, channels=list(channels), status=status)


class FakeRepository:
    """The repository methods build_dossier and build_report call, and the line history's (the modem's stored
    readings, pengumon.lines.docsis.DocsisHistory), returning fixed data: one object, so `calls` keeps their order."""

    def __init__(self, incidents=(), hop_stats=(), snapshot=None, window_snaps=(), daily=(), hourly=()):
        self.incidents, self.hop_stats, self.snapshot = list(incidents), list(hop_stats), snapshot
        self.window_snaps = list(window_snaps)
        self.daily, self.hourly = list(daily), list(hourly)
        self.calls: List[list] = []

    async def get_incidents_since(self, start_time):
        self.calls.append(["get_incidents_since", start_time])
        return [dict(r) for r in self.incidents]

    async def get_hop_stats(self, start_time):
        self.calls.append(["get_hop_stats", start_time])
        return [dict(h) for h in self.hop_stats]

    async def get_latest_docsis_snapshot(self):
        self.calls.append(["get_latest_docsis_snapshot"])
        return self.snapshot

    async def get_docsis_snapshots_in_range(self, start_time, end_time, padding_seconds=120.0):
        self.calls.append(["get_docsis_snapshots_in_range", start_time, end_time, padding_seconds])
        return list(self.window_snaps)

    async def get_daily_availability(self, start_time, tz_offset_minutes=0):
        self.calls.append(["get_daily_availability", start_time, tz_offset_minutes])
        return [dict(r) for r in self.daily]

    async def get_hourly_loss(self, start_time, tz_offset_minutes=0):
        self.calls.append(["get_hourly_loss", start_time, tz_offset_minutes])
        return [dict(r) for r in self.hourly]


# --- scenarios: name -> (config, repository, hours) -------------------------------------------------------------

SCENARIOS: Dict[str, Any] = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def no_config_nothing():
    return None, FakeRepository(), 24


@scenario
def default_order_with_unknown_hop():
    stats = [hop("zzz_unlisted", lost=1), hop("google_dns", lost=40), hop("local_router"), hop("isp_gateway", sent=0),
             hop("cloudflare_dns", lost=30)]
    # The hop order is the config order (reports.DEFAULT_ORDER is gone): the ids the old default listed, in its order
    ids = ["local_router", "isp_gateway", "isp_hop_1", "isp_hop_2", "isp_dns", "cloudflare_dns",
           "google_dns", "quad9_dns", "dns_system_google", "dns_direct_cloudflare"]
    return cfg(targets=[target(i, i, "custom") for i in ids], dns=[]), \
        FakeRepository(hop_stats=stats, snapshot=snap(channels=[ch(1)])), 48


@scenario
def quiet_week():
    snaps = [snap(NOW - 7200, total=100), snap(NOW - 3600, total=100), snap(NOW - 60, total=100)]
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=snaps[-1], window_snaps=snaps), 168


@scenario
def excluded_and_under_load():
    rows = [incident_row("isp_loss", 120.0, load=75.0), incident_row("local_wifi_mesh_drop", None, load=70.0),
            incident_row("isp_loss", 30.5, load=None), incident_row("dns_resolver_failure", 600.0, load=12.0),
            incident_row("load_induced_latency", 900.0, expected=True, load=95.0),
            incident_row("load_induced_latency", 15.0, expected=True)]
    return cfg(busy=70.0), FakeRepository(incidents=rows, hop_stats=hops()), 24


@scenario
def default_busy_threshold_and_cause_ties():
    rows = [incident_row("b_cause", 60.0, load=60.0), incident_row("a_cause", 60.0, load=59.9),
            incident_row("c_cause", 120.0, load=None), incident_row("a_cause", 30.0, load=100.0)]
    return cfg(), FakeRepository(incidents=rows, hop_stats=hops()), 6


@scenario
def only_expected_incidents():
    rows = [incident_row("load_induced_latency", 900.0, expected=True)]
    return cfg(), FakeRepository(incidents=rows, hop_stats=hops()), 24


@scenario
def primary_hop_with_later_lossy_hops():
    return cfg(), FakeRepository(hop_stats=hops(gateway=0.2, isp=3.0, cloudflare=2.5, dns_google=0.1),
                                 snapshot=snap(channels=[ch(1)])), 24


@scenario
def primary_hop_alone():
    return cfg(), FakeRepository(hop_stats=hops(isp=4.0), snapshot=snap(channels=[ch(1)])), 24


@scenario
def later_hops_at_the_threshold():
    stats = [hop("router"), hop("gateway"), hop("isp", lost=30), hop("cloudflare", lost=5),
             {"target_id": "dns_google", "sent": 0, "lost": 0, "loss_pct": 9.9, "avg_ms": 0, "max_ms": 0}]
    return cfg(), FakeRepository(hop_stats=stats), 24


@scenario
def local_and_gateway_without_probes():
    stats = [hop("router", sent=0), hop("gateway", sent=0), hop("isp", lost=20)]
    return cfg(), FakeRepository(hop_stats=stats), 24


@scenario
def lan_loss_exactly_at_threshold():
    return cfg(), FakeRepository(hop_stats=hops(gateway=0.5)), 24


@scenario
def lan_loss_below_threshold_and_empty_hops():
    stats = [hop("router", lost=3), hop("gateway", sent=0), hop("isp", lost=4), hop("cloudflare", sent=0),
             hop("dns_google", lost=0)]
    return cfg(), FakeRepository(hop_stats=stats), 24


@scenario
def hop_labels_with_and_without_address():
    targets = [target("router", "Local Router", "local", ""), target("gateway", "Gateway 192.168.100.1", "gateway", "192.168.100.1"),
               target("isp", "ISP Hub", "isp", "198.51.100.5"), target("cloudflare", "Cloudflare", "public", "1.1.1.1")]
    return cfg(targets=targets, dns=[]), FakeRepository(hop_stats=[hop("router", lost=9), hop("gateway", lost=19),
                                                                    hop("isp", lost=29), hop("cloudflare", lost=39)]), 24


@scenario
def no_local_targets_in_config():
    targets = [target("isp", "ISP Hub", "isp", "198.51.100.5"), target("cloudflare", "Cloudflare", "public", "1.1.1.1")]
    return cfg(targets=targets, dns=[]), FakeRepository(hop_stats=[hop("isp", lost=20), hop("cloudflare", lost=30)]), 24


@scenario
def docsis_unconfigured():
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=snap(status="unconfigured")), 24


@scenario
def docsis_error_with_error_snapshots_in_window():
    window = [snap(NOW - 7000, total=10), snap(NOW - 5000, status="error"), snap(NOW - 3000, total=40),
              snap(NOW - 1000, status="unconfigured")]
    return cfg(), FakeRepository(hop_stats=hops(isp=1.0), snapshot=snap(status="error"), window_snaps=window), 24


@scenario
def docsis_error_only():
    # what the collector stores when the modem cannot be read: nothing else in the window
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=snap(status="error", total=0)), 24


@scenario
def primary_hop_is_a_dns_probe_without_address():
    stats = [hop("router"), hop("gateway"), hop("isp"), hop("cloudflare"), hop("dns_google", lost=60)]
    return cfg(), FakeRepository(hop_stats=stats), 24


@scenario
def docsis_all_channels_typical():
    s = snap(channels=[ch(1), ch(2, snr=36.0)])
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=s, window_snaps=[snap(NOW - 600), s]), 24


@scenario
def docsis_counter_reset_and_error_snapshot_in_window():
    s = snap(total=300, channels=[ch(1)])
    window = [snap(NOW - 9000, total=1000), snap(NOW - 8000, total=1500), snap(NOW - 7000, total=50),
              snap(NOW - 6000, status="error", total=0), snap(NOW - 5000, total=250), s]
    return cfg(), FakeRepository(hop_stats=hops(isp=2.0), snapshot=s, window_snaps=window), 12


@scenario
def docsis_every_channel_finding():
    channels = [
        ch(1, unc=900, snr=40.0),                         # High uncorrectable count
        ch(2, snr=30.5),                                  # Low SNR
        ch(3, direction="upstream", power=50.0, snr=None, mod=None, cor=None),   # High upstream power
        ch(4, direction="upstream", power=35.0, freq=0.0),     # Low upstream power
        ch(5, unc=250, snr=36.0),                         # Uncorrectables present
        ch(6, power=-8.5),                                # Power level out of range (downstream)
        ch(7, direction="upstream", power=43.0, snr=39.0),     # fine
        ch(8, snr=38.0, unc=100),                         # exactly 100: fine
        ch(9, power=7.0),                                 # exactly 7: fine
        ch(10, direction="upstream", power=48.0, snr=33.0),    # exactly at the limits: fine
        ch(11, direction="upstream", power=38.0, snr=None),    # exactly 38 dBmV: fine
        ch(12, unc=500, snr=36.0),                        # exactly 500: Uncorrectables present, not High
        ch(13, unc=150, snr=36.0, recent=9),              # recent errors outrank totals in the ordering
    ]
    s = snap(channels=channels, total=123456)
    window = [snap(NOW - 5000, total=100000), snap(NOW - 2500, total=120000), s]
    return cfg(), FakeRepository(incidents=[incident_row("xfinity_last_mile_coax_drop", 300.0)],
                                 hop_stats=hops(isp=5.0, cloudflare=5.0), snapshot=s,
                                 window_snaps=window), 24


@scenario
def docsis_worst_channel_without_frequency():
    s = snap(channels=[ch(1, unc=800, freq=0.0, snr=None)])
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=s), 24


@scenario
def docsis_degraded_channel_only():
    s = snap(channels=[ch(1), ch(2, snr=31.0)])
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=s, window_snaps=[s]), 24


@scenario
def docsis_healthy_without_channels():
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=snap(channels=[])), 24


@scenario
def docsis_new_uncorrectables_only():
    window = [snap(NOW - 3000, total=10), snap(NOW - 60, total=15, channels=[ch(1)])]
    return cfg(), FakeRepository(hop_stats=hops(), snapshot=window[-1], window_snaps=window), 24


@scenario
def zero_hours_and_everything_missing():
    return cfg(targets=[], dns=[]), FakeRepository(), 0


# --- recording ---------------------------------------------------------------------------------------------

def jsonable(value):
    return json.loads(json.dumps(value, default=lambda o: o.model_dump() if hasattr(o, "model_dump") else str(o)))


async def run_dossier(name: str) -> dict:
    config, repo, hours = SCENARIOS[name]()
    payload = await build_dossier(repo, config, hours, now=NOW, line=repo)
    markdown = payload["formatted_markdown"]
    out = jsonable({k: v for k, v in payload.items() if k != "formatted_markdown"})
    return {"key_order": list(payload), "calls": jsonable(repo.calls), "payload": out, "markdown": markdown.split("\n")}


def report_scenario():
    repo = FakeRepository(
        incidents=[incident_row("isp_loss", 120.0), incident_row("isp_loss", 60.0), incident_row("dns_resolver_failure", 300.0),
                   incident_row("load_induced_latency", 900.0, expected=True), incident_row("a_cause", None)],
        daily=[{"target_id": t, "day": d, "sent": s, "ok": ok} for t, d, s, ok in (
            ("isp", "2025-10-07", 86400, 86000), ("router", "2025-10-07", 86400, 86400), ("isp", "2025-10-08", 43200, 43200),
            ("router", "2025-10-08", 0, 0), ("zzz", "2025-10-08", 100, 50), ("cloudflare", "2025-10-08", 43200, 43100))],
        hourly=[{"hour": h, "sent": 3600 if h != 5 else 0, "lost": (h * 7) % 11 if h < 8 else 0} for h in range(24)])
    return cfg(), repo


async def run_report() -> dict:
    config, repo = report_scenario()
    payload = await build_report(repo, config, 3, now=NOW, tz_offset_minutes=-300)
    return {"key_order": list(payload), "calls": jsonable(repo.calls), "payload": jsonable(payload)}


async def build_golden() -> dict:
    golden = {name: await run_dossier(name) for name in SCENARIOS}
    golden["build_report"] = await run_report()
    return golden


@pytest.fixture
def utc_clock(monkeypatch):
    skip_without_tzset()
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_dossier_and_report_match_the_recorded_golden(utc_clock):
    check_golden("dossier_characterization", await build_golden())
