"""Characterization of GET /api/incidents/{id}/inspect, recorded before the inspector moved to pengumon.analysis.

Every scenario seeds its own temporary database with fixed absolute timestamps, points the API state at it and
stores the endpoint's JSON. The result is compared with tests/golden/inspect_characterization/<scenario>.json. The short-window
scenarios must stay byte-identical through the refactor; only the "long_*" scenarios (windows over 30 minutes) may
change, deliberately, when the inspector starts returning buckets.

The golden stores the incident as {id, kind, cause_code, start_time, end_time} and leaves out every "grades" and
"unerrored" field: the shared models own those (test_incident_cause.py, test_grades.py), so a change to them does not
rewrite this golden.

Regenerate only on purpose: PENGUMON_REGEN_GOLDEN=1 python -m pytest tests/test_inspect_characterization.py
"""

import hashlib
import json
import zlib
from types import SimpleNamespace
from typing import Callable, List, Optional

from httpx import ASGITransport, AsyncClient

from pengumon.db import probes as probes_module
from pengumon.db.repository import DatabaseRepository
from pengumon.api.state import STATE_ATTR, AppState
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.samples import device_sample
from pengumon.lines.docsis import line_sample
from pengumon.main import app
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample

from device_helpers import build_modem, build_router
from golden_files import check_golden

NOW = 1_760_000_000.0  # the repository's clock; sample data sits shortly before it, inside every retention window
T0 = NOW - 7200.0
COMPACT_ABOVE = 40  # scenarios with more samples than this store a digest instead of every row


# --- seeding helpers ---------------------------------------------------------------------------------------

def series(target: str, start: float, end: float, step: float = 1.0, base: float = 10.0,
           fail: Optional[Callable[[int, float], bool]] = None, jitter: Optional[float] = 0.5) -> List[ProbeSample]:
    """One probe every `step` seconds from start to end (inclusive); `fail(i, t)` marks lost probes."""
    out = []
    i = 0
    t = start
    # Targets answer a few milliseconds apart: rows are ordered by time alone, so equal timestamps would come back
    # in an order that depends on string hashing.
    offset = (zlib.crc32(target.encode()) % 1000) * 1e-5
    while t <= end:
        if fail and fail(i, t):
            out.append(ProbeSample(target_id=target, timestamp=t + offset, success=False, packet_loss_pct=100.0,
                                   error="timeout"))
        else:
            latency = base + ((i * 7) % 5) * 0.5
            out.append(ProbeSample(target_id=target, timestamp=t + offset, success=True, latency_ms=latency,
                                   packet_loss_pct=0.0, jitter_ms=None if jitter is None else jitter + (i % 3) * 0.25))
        i += 1
        t += step
    return out


def ch(channel_id: int, direction="downstream", snr=38.0, recent=0, total=0, power=1.0, freq=600.0):
    return DocsisChannel(channel_id=channel_id, direction=direction, frequency_mhz=freq + channel_id, power_dbmv=power,
                         snr_db=snr, modulation="QAM256", correcteds=10 * channel_id, uncorrectables=total,
                         uncorrectables_recent=recent)


def snap(ts: float, status="healthy", channels=None, recent=0) -> DocsisSignalSnapshot:
    channels = channels if channels is not None else []
    return DocsisSignalSnapshot(timestamp=ts, downstream_power_avg=1.5, downstream_snr_avg=37.5,
                                upstream_power_avg=42.0, uncorrectables_total=sum(c.uncorrectables or 0 for c in channels),
                                uncorrectables_recent=recent, channels=channels, status=status)


def router_snap(ts: float, rx=20.0, tx=2.0, cpu=30.0) -> RouterMetricsSnapshot:
    return RouterMetricsSnapshot(timestamp=ts, wan_rx_bytes=int(ts) * 1000, wan_tx_bytes=int(ts) * 100,
                                 wan_rx_rate_mbps=rx, wan_tx_rate_mbps=tx, cpu_usage_pct=cpu, mem_usage_pct=40.0,
                                 client_count=12, status="healthy",
                                 model_name="Example_Router")  # as every stored row has it (the reader no longer fills it in)


def cfg_full():
    return SimpleNamespace(
        targets=[
            SimpleNamespace(id="router", name="Local Router", category="local"),
            SimpleNamespace(id="gateway", name="Modem Gateway", category="gateway"),
            SimpleNamespace(id="isp", name="ISP Hub", category="isp"),
            SimpleNamespace(id="cloudflare", name="Cloudflare", category="public"),
        ],
        dns_probes=[SimpleNamespace(id="dns_google", name="Google DNS")],  # DnsProbeConfig has no category
    )


def incident(start: float, duration: Optional[float] = 30.0, resolved=True, **kw) -> Incident:
    end = None if duration is None else start + duration
    fields = dict(start_time=start, end_time=end, duration_seconds=duration, cause_code="xfinity_last_mile_coax_drop",
                  title="Upstream Loss", description="characterization", severity="critical", resolved=resolved,
                  affected_targets=["isp", "cloudflare"], kind="isp_loss")
    fields.update(kw)
    return Incident(**fields)


class Seed:
    def __init__(self):
        self.incident = incident(T0)
        self.config = cfg_full()
        self.samples: List[ProbeSample] = []
        self.snapshots: List[DocsisSignalSnapshot] = []
        self.routers: List[RouterMetricsSnapshot] = []


def standard_samples(start=T0, end=T0 + 30.0, targets=("router", "gateway", "isp", "cloudflare", "dns_google"),
                     fail=None, step=1.0):
    out = []
    for n, target in enumerate(targets):
        out += series(target, start - 45.0, end + 45.0, step=step, base=2.0 + 5 * n, fail=(fail or {}).get(target))
    return out


# --- scenarios ---------------------------------------------------------------------------------------------

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def short_full(s: Seed):
    s.samples = standard_samples(fail={
        "cloudflare": lambda i, t: T0 <= t <= T0 + 30,
        "isp": lambda i, t: T0 <= t <= T0 + 30 and i % 3 == 0,
        "dns_google": lambda i, t: T0 + 10 <= t <= T0 + 20,
    })
    s.snapshots = [
        snap(T0 - 60, channels=[ch(1), ch(2, snr=36.0)]),
        snap(T0 + 5, status="warning", recent=14, channels=[
            ch(1, snr=38.5, total=20), ch(2, snr=33.1, recent=12, total=400), ch(3, snr=35.0, recent=12, total=900),
            ch(4, snr=None, total=5), ch(5, direction="upstream", snr=None, power=44.0)]),
        snap(T0 + 60, recent=2, channels=[ch(1), ch(2)]),
    ]
    s.routers = [router_snap(T0 - 100), router_snap(T0 + 10, rx=95.0, cpu=80.0), router_snap(T0 + 200)]


@scenario
def no_docsis_latest_fallback(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway", "isp"))
    # nothing near the incident: the latest snapshot ever stored stands in, and its channel is erroring
    s.snapshots = [snap(T0 - 3600, channels=[ch(1, recent=0, total=3), ch(2, snr=30.0, recent=9, total=77)], recent=0)]


@scenario
def no_docsis_at_all(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway"))
    s.incident = incident(T0, 60.0)


@scenario
def no_docsis_long_outage(s: Seed):
    s.incident = incident(T0, 120.0)
    s.samples = standard_samples(end=T0 + 120.0, targets=("router", "gateway", "cloudflare"),
                                 fail={"cloudflare": lambda i, t: T0 <= t <= T0 + 120})
    s.snapshots = [snap(T0 - 3600, channels=[ch(1)])]  # healthy and far away: only the 90 s rule fires


@scenario
def gateway_loss_router_clean(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway"), fail={"gateway": lambda i, t: T0 <= t <= T0 + 30})
    s.snapshots = [snap(T0 + 5, channels=[ch(1)])]


@scenario
def gateway_loss_router_lossy(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway"), fail={
        "gateway": lambda i, t: T0 <= t <= T0 + 30, "router": lambda i, t: T0 <= t <= T0 + 30 and i % 3 == 0})
    s.snapshots = [snap(T0 + 5, channels=[ch(1)])]


@scenario
def gateway_loss_without_router_target(s: Seed):
    s.config = SimpleNamespace(targets=[SimpleNamespace(id="gateway", name="Modem Gateway", category="gateway")],
                               dns_probes=[])
    s.samples = standard_samples(targets=("gateway",), fail={"gateway": lambda i, t: T0 <= t <= T0 + 30})
    s.snapshots = [snap(T0 + 5)]


def _boundary(s: Seed, router_fail_every: int):
    s.incident = incident(T0, 19.0)
    # Real segments: the gateway and the router are found by category (target ids carry no meaning).
    s.config = SimpleNamespace(targets=[SimpleNamespace(id="isp_gateway", name="Gateway", category="gateway"),
                                        SimpleNamespace(id="local_router", name="Router", category="local")],
                               dns_probes=[])
    # 20 probes each inside the calculation window [T0 - 10, T0 + 21]; the gateway loses exactly 10 of them
    s.samples = (series("isp_gateway", T0, T0 + 19, fail=lambda i, t: i % 2 == 1)
                 + series("local_router", T0, T0 + 19, fail=lambda i, t: i in router_fail_every))
    s.snapshots = [snap(T0 + 5)]


@scenario
def boundary_gateway_50_router_15(s: Seed):
    _boundary(s, {0, 5, 10})  # 3 of 20 = exactly 15.0 %: not below the limit


@scenario
def boundary_gateway_50_router_10(s: Seed):
    _boundary(s, {0, 5})  # 10.0 %


@scenario
def snapshot_error_in_window(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway", "isp"))
    # the healthy snapshot is the nearest one; an error snapshot elsewhere in the window still counts
    s.snapshots = [snap(T0 + 2, channels=[ch(1)]), DocsisSignalSnapshot(timestamp=T0 + 40, status="error",
                                                                        error_message="modem unreachable")]


@scenario
def latest_snapshot_error_fallback(s: Seed):
    s.samples = standard_samples(targets=("router", "gateway"))
    s.snapshots = [DocsisSignalSnapshot(timestamp=T0 - 5000, status="error", error_message="login failed")]


@scenario
def open_incident(s: Seed):
    s.incident = incident(T0, None, resolved=False, end_time=None)
    s.samples = standard_samples(end=T0 + 10.0, targets=("router", "isp"), fail={"isp": lambda i, t: t >= T0 + 3})
    s.snapshots = [snap(T0 + 1, channels=[ch(1)])]


@scenario
def unknown_target_dns_guess(s: Seed):
    s.config = SimpleNamespace(targets=[SimpleNamespace(id="router", name="Local Router", category="local")], dns_probes=[])
    s.samples = standard_samples(targets=("router", "dns_extra"))


@scenario
def unknown_target_custom_guess(s: Seed):
    s.config = SimpleNamespace(targets=[SimpleNamespace(id="router", name="Local Router", category="local")], dns_probes=[])
    s.samples = standard_samples(targets=("router", "mystery"))


@scenario
def config_none(s: Seed):
    s.config = None
    s.samples = series("lonely", T0, T0 + 5)
    s.snapshots = [snap(T0)]


@scenario
def calc_window_empty_falls_back_to_all(s: Seed):
    s.samples = (series("router", T0 - 45, T0 - 20, base=3.0) + series("isp", T0 - 45, T0 - 20, base=8.0,
                 fail=lambda i, t: i < 5) + series("router", T0 + 60, T0 + 70, base=3.0))
    s.snapshots = [snap(T0 + 5)]


@scenario
def calc_window_has_some_targets_only(s: Seed):
    s.samples = series("router", T0 - 5, T0 + 5, base=3.0) + series("isp", T0 - 40, T0 - 20, base=8.0)
    s.snapshots = [snap(T0 + 5)]


@scenario
def no_samples(s: Seed):
    s.snapshots = [snap(T0 + 5)]
    s.routers = [router_snap(T0 + 5)]


@scenario
def no_downstream_channels(s: Seed):
    s.samples = standard_samples(targets=("router",))
    s.snapshots = [snap(T0 + 5, channels=[ch(7, direction="upstream", snr=None)])]


@scenario
def channels_without_snr(s: Seed):
    s.samples = standard_samples(targets=("router",))
    s.snapshots = [snap(T0 + 5, channels=[ch(1, snr=None), ch(2, snr=None, total=4)])]


@scenario
def channel_sort_ties(s: Seed):
    s.samples = standard_samples(targets=("router",))
    s.snapshots = [snap(T0 + 5, recent=0, channels=[
        ch(1, snr=40.0, recent=3, total=10), ch(2, snr=30.0, recent=3, total=10), ch(3, snr=35.0, recent=3, total=11),
        ch(4, snr=20.0, recent=0, total=999)])]


@scenario
def channel_sort_snr_tie(s: Seed):
    s.samples = standard_samples(targets=("router",))
    s.snapshots = [snap(T0 + 5, channels=[ch(1, snr=40.0), ch(2, snr=30.0), ch(3, snr=35.0)])]


@scenario
def tiny_incident_calc_floor(s: Seed):
    # a 3 s incident: the calculation window still runs 20 s from its lead-in start (T0 - 10 .. T0 + 10)
    s.incident = incident(T0, 3.0)
    s.samples = standard_samples(end=T0 + 30.0, targets=("router", "isp"), fail={"isp": lambda i, t: T0 + 6 <= t <= T0 + 9})
    s.snapshots = [snap(T0 + 1, channels=[ch(1)])]


@scenario
def worst_channel_errors_only_in_nearest(s: Seed):
    s.samples = standard_samples(targets=("router",))
    s.snapshots = [snap(T0 - 100, recent=0, channels=[ch(1)]), snap(T0 + 2, recent=0, channels=[ch(1, recent=4, total=4)])]


def _long(s: Seed, duration: float, step: float, targets=("router", "isp")):
    s.incident = incident(T0, duration)
    s.samples = standard_samples(end=T0 + duration, targets=targets, step=step, fail={
        "isp": lambda i, t: T0 + 100 <= t <= T0 + 400 and i % 2 == 0})
    s.snapshots = [snap(T0 + 50, channels=[ch(1, snr=34.0, recent=2, total=2)]), snap(T0 + duration, channels=[ch(1)])]
    s.routers = [router_snap(T0 + 100), router_snap(T0 + duration)]


@scenario
def window_exactly_1800s(s: Seed):
    _long(s, 1710.0, 5.0)  # window = duration + 90 s = 1800 s: still raw samples


@scenario
def long_window_1801s(s: Seed):
    _long(s, 1711.0, 5.0)


@scenario
def long_window_50min(s: Seed):
    _long(s, 3000.0, 2.0)


# --- recording ---------------------------------------------------------------------------------------------

INCIDENT_FIELDS = ("id", "kind", "cause_code", "start_time", "end_time")
SHARED_MODEL_FIELDS = ("grades", "unerrored")  # on snapshots and channels; covered by test_grades.py


def project(value):
    """The response without what shared models own: the incident reduced to INCIDENT_FIELDS, no grades."""
    if isinstance(value, dict):
        return {k: project(v) for k, v in value.items() if k not in SHARED_MODEL_FIELDS}
    if isinstance(value, list):
        return [project(v) for v in value]
    return value


def digest(rows) -> dict:
    text = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return {"count": len(rows), "first": rows[:2], "last": rows[-2:], "sha256": hashlib.sha256(text.encode()).hexdigest()}


async def run_scenario(name: str, tmp_path, monkeypatch) -> dict:
    s = Seed()
    SCENARIOS[name](s)
    monkeypatch.setattr(probes_module, "time", SimpleNamespace(time=lambda: NOW, monotonic=__import__("time").monotonic))
    repo = DatabaseRepository(db_path=str(tmp_path / f"{name}.db"))
    await repo.initialize()
    await repo.insert_samples(s.samples)
    # The devices of the legacy docsis:/router: sections, as before devices: (the modem polled every 900 s, which is
    # how far outside the window a modem reading still counts); the inspector reads their stored samples.
    devices = DeviceRegistry([build_modem(password="pw"), build_router(password="pw")])
    for snapshot in s.snapshots:
        await repo.insert_line_sample(line_sample("modem", snapshot))
    for snapshot in s.routers:
        await repo.insert_device_sample(device_sample("router", snapshot))
    iid = await repo.create_incident(s.incident)
    monkeypatch.setattr(app.state, STATE_ATTR, AppState(repository=repo, config=s.config, devices=devices),
                        raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/incidents/{iid}/inspect")
    assert res.status_code == 200
    body = project(json.loads(res.text))
    if len(body["samples"]) > COMPACT_ABOVE:
        body["samples"] = digest(body["samples"])
    body["incident"] = {key: body["incident"][key] for key in INCIDENT_FIELDS}
    return body


async def build_golden(tmp_path, monkeypatch) -> dict:
    golden = {}
    key_order = None
    for name in SCENARIOS:
        sub = tmp_path / name
        sub.mkdir()
        golden[name] = await run_scenario(name, sub, monkeypatch)
        if name == "short_full":
            key_order = {
                "top": list(golden[name]),
                "docsis_correlation": list(golden[name]["docsis_correlation"]),
                "hop_impact": list(golden[name]["hop_impact"][0]),
            }
    # The JSON key order of a response is part of the contract; scenario bodies are stored with sorted keys.
    golden["key_order_of_short_full"] = key_order
    return golden


async def test_inspect_matches_the_recorded_golden(tmp_path, monkeypatch):
    check_golden("inspect_characterization", await build_golden(tmp_path, monkeypatch))


def test_scenarios_cover_the_inspector_branches():
    assert len(SCENARIOS) >= 20
