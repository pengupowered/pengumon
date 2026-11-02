"""Opt-in browser smoke test of the dashboard (PENGUMON_UI_SMOKE=1).

Starts the real app on a free port with a seeded database (tests/ui/serve.py), drives it with headless Chromium
through puppeteer-core (tests/ui/smoke.mjs) and fails on any page error, console error, CSP violation or failed
request. Supported way to run it: scripts/test-ui.sh, which builds Dockerfile.ui (on the shared ui-test-runner image: Chromium, Node, the npm packages
pinned in tests/ui/runner/package-lock.json) and runs the test in a locked-down container with no network and a read-only
root filesystem. Alternative: any installed Chrome (e.g. on Windows) named by PUPPETEER_EXECUTABLE_PATH, with
puppeteer-core installed from the lockfile into the directory $PGBROWSER (optionally PENGUMON_BROWSER_ARGS for extra
Chrome flags). None of it is a dependency of the project, so the test is skipped unless node and puppeteer-core exist.
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from pengumon.capabilities import BAND_5G, Client, ClientsReading
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import MeshCount
from pengumon.devices.samples import device_sample
from pengumon.lines.docsis import line_sample
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import PathTraceHop, ProbeSample

ROOT = Path(__file__).resolve().parents[1]
UI = Path(__file__).resolve().parent / "ui"
PGBROWSER = os.environ.get("PGBROWSER") or str(Path.home() / "pgbrowser")
TARGETS = [("local_router", "Hop 1: Local Router (LAN)", "local"),
           ("isp_gateway", "Hop 2: ISP Modem / Gateway", "gateway"),
           ("cloudflare_dns", "Public: Cloudflare Anycast (1.1.1.1)", "public")]

# the `longpath` variant: the owner's real shape (seven nodes, long configured names) under made-up vendor names
LONG_TARGETS = [("lp_router", "Hop 1: Example Vendor Wireless Router (LAN)", "local"),
                ("lp_modem", "Hop 2: Example Vendor Cable Modem / Gateway", "gateway"),
                ("lp_isp1", "Hop 3: Example Provider Regional Aggregation Router", "isp"),
                ("lp_isp2", "Hop 4: Example Provider Backbone Router", "isp"),
                ("lp_isp3", "Hop 5: Example Provider Peering Edge Router", "isp"),
                ("lp_public", "Public: Example Public Resolver Anycast (203.0.113.1)", "public"),
                ("lp_public2", "Public: Second Example Resolver Anycast (203.0.113.2)", "public"),
                ("lp_public3", "Public: Third Example Resolver Anycast (203.0.113.3)", "public")]

pytestmark = pytest.mark.skipif(
    os.environ.get("PENGUMON_UI_SMOKE") != "1" or not shutil.which("node")
    or not (Path(PGBROWSER) / "node_modules" / "puppeteer-core").is_dir(),
    reason="run scripts/test-ui.sh, or set PENGUMON_UI_SMOKE=1 with node, $PGBROWSER (puppeteer-core) and PUPPETEER_EXECUTABLE_PATH")


async def seed(db_path: str, now: float) -> None:
    """Three hops of 1 s probes for 2 h 20 min, 15 network incidents plus a service one (a 2 h one, a short one with a path trace, one
    explained by load), DOCSIS and router history around them."""
    repo = DatabaseRepository(db_path=db_path)
    await repo.initialize()
    start = float(int(now)) - 8400.0
    samples = []
    # one probe round per second, the targets 25 ms apart as the prober sends them, straddling the half second: rounding
    # timestamps to whole seconds would split every round (the inspector once did, and showed holes as "null%")
    for i, (tid, _, _) in enumerate(TARGETS):
        t = start + 0.49 + 0.025 * i
        while t < now - 5.0:
            lost = tid == "isp_gateway" and now - 3000 <= t <= now - 2910 and int(t) % 2 == 0
            samples.append(ProbeSample(target_id=tid, timestamp=t, success=not lost, latency_ms=None if lost else 8.0 + int(t) % 5,
                                       packet_loss_pct=100.0 if lost else 0.0, jitter_ms=None if lost else 0.5))
            t += 1.0
    await repo.insert_samples(samples)
    await repo.flush_samples()

    def incident(title, begin, seconds, **kw):
        return Incident(start_time=begin, end_time=begin + seconds, duration_seconds=seconds, cause_code="unknown_outage",
                        title=title, description=f"{title} (seeded)", severity=kw.pop("severity", "warning"), resolved=True, **kw)

    long_id = await repo.create_incident(incident("Long evening outage", now - 7900, 7200.0, severity="critical"))
    # detected 3 s after the gateway's probes start failing and closed 8 s after they answer again, as the classifier does:
    # the app's start reads the loss span (about 90 s) from the probes, the recorded 98 s stay the incident's duration
    short_id = await repo.create_incident(incident("Short gateway drop", now - 2997, 98.0, affected_targets=["isp_gateway"]))
    await repo.create_incident(incident("Busy line slowdown", now - 2000, 120.0, load_pct=96.0, expected=True))
    for i in range(12):
        await repo.create_incident(incident(f"Seeded blip {i + 1}", now - 1800 + i * 60, 20.0))
    await repo.create_incident(Incident(
        start_time=now - 4000, end_time=now - 3900, duration_seconds=100.0, cause_code="service_unavailable",
        kind="service_down", title="Service Down is not responding", description="seeded service outage",
        severity="critical", resolved=True, affected_targets=["svc_down"]))
    await repo.update_incident_path_trace(short_id, [
        PathTraceHop(hop=1, ip="192.168.1.1", hostname="router.lan", latency_ms=1.2),
        PathTraceHop(hop=2, ip="192.168.100.1", latency_ms=None, loss_pct=100.0)])

    for k in range(0, 8400, 60):
        ts = start + k
        recent = 42 if now - 3000 <= ts <= now - 2900 else 0
        await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
            timestamp=ts, downstream_power_avg=1.3, downstream_snr_avg=35.9, upstream_power_avg=44.5,
            uncorrectables_total=1000 + k, uncorrectables_recent=recent, status="healthy",
            channels=[DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=537.0, power_dbmv=2.0, snr_db=31.5,
                                    uncorrectables=1000 + k, uncorrectables_recent=recent),
                      DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=531.0, power_dbmv=1.5,
                                    snr_db=36.0 + (k // 60) % 4, uncorrectables=10),
                      DocsisChannel(channel_id=1, direction="upstream", frequency_mhz=30.6, power_dbmv=44.5)])))
    for k in range(0, 8400, 10):
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
            timestamp=start + k, wan_rx_rate_mbps=50.0 + k % 40, wan_tx_rate_mbps=5.0, cpu_usage_pct=20.0 + k % 30, mem_usage_pct=40.0,
            client_count=12 + k // 3000, status="healthy")))
    for k in range(0, 8400, 60):  # client counts (aggregates only) of the made-up client list in serve.py
        ts = start + k
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
            timestamp=ts + 1, status="healthy"), clients=ClientsReading(
                (Client(key="w", connection="wired"),) * 2 + (Client(key="f", connection="wifi", band=BAND_5G),) * (k // 60 % 4 + 1))))
        await repo.insert_mesh_counts([MeshCount("router", ts + 1, "0000a1", 2), MeshCount("router", ts + 1, "0000a2", k // 60 % 4 + 1)])
    await repo.flush_samples()
    await repo.close()
    assert long_id and short_id


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


DEVICES = {
    "longpath": "",
    "full": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
            '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    "bare": "",
    # two routers: one card each, each with its own history and setup
    "tworouters": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                  '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n'
                  '  - {id: router2, driver: asuswrt, url: "http://192.168.97.1", model: "Second_AP"}\n',
    # an access token: smoke.mjs signs in through the sign-in page; only the sign-out steps run (steps/sign-out.mjs)
    "token": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
             '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
    # a line device of a technology the page has no adapter for (serve.py relabels the modem driver)
    "unknownline": 'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.168.99.1"}\n'
                   '  - {id: router, driver: asuswrt, url: "http://192.168.98.1", model: "Example_Router"}\n',
}


@pytest.mark.parametrize("variant", list(DEVICES))
def test_dashboard_in_headless_chromium(tmp_path, variant):
    """`full`: modem, router and a two-node mesh (every panel and the Local network modal). `bare`: no devices at all
    (no device cards, the rest of the live page works). `tworouters`: a card per router. `unknownline`: the line card's
    common part only. `token`: `full` behind an access token (sign in, sign out, sign out everywhere)."""
    db = tmp_path / "smoke.db"
    port = free_port()
    asyncio.run(seed(str(db), time.time()))
    config = tmp_path / "config.yaml"
    targets = "".join(
        f'  - {{id: "{t}", name: "{n}", category: {c}, address: "127.0.0.1", enabled: true}}\n'
        for t, n, c in (LONG_TARGETS if variant == "longpath" else TARGETS))
    config.write_text(
        "prober: {interval_seconds: 1.0}\n"
        f"targets:\n{targets}"  # no dns_probes: the two default lookups
        f'database: {{db_path: "{db.as_posix()}"}}\n'
        "privacy: {client_details: full}\n"  # the Clients modal then shows names and addresses
        "line: {isp_name: Example ISP}\n"  # the dossier title and file name the smoke script checks
        "service_checks:\n  interval_seconds: 2\n  failures_for_incident: 1\n  services:\n"
        f'    - {{id: svc_up, name: "Service Up", url: "http://127.0.0.1:{port}/api/health", description: "the app itself",'
        ' open_url: "http://services.example.test:9000/app"}\n'
        '    - {id: svc_down, name: "Service Down", url: "http://127.0.0.1:1/health"}\n'
        + DEVICES[variant],
        encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PENGUMON_CONFIG": str(config), "PGBROWSER": PGBROWSER}
    # configured devices (a password each), so the features the history views need are available
    env.update({"PENGUMON_DEVICE_MODEM_PASSWORD": "smoke-pw", "PENGUMON_DEVICE_ROUTER_PASSWORD": "smoke-pw",
                "PENGUMON_DEVICE_ROUTER2_PASSWORD": "smoke-pw"})
    if variant == "unknownline":
        env["PENGUMON_SMOKE_LINE_TECHNOLOGY"] = "fiber"
    if variant == "longpath":  # as on Windows: round trips in whole milliseconds (serve.py's stand-in backend)
        env["PENGUMON_SMOKE_PROBE_RESOLUTION_MS"] = "1.0"
    for name in ("PENGUMON_ACCESS_TOKEN", "PENGUMON_ALLOWED_HOSTS", "PENGUMON_ALLOWED_ORIGINS"):
        env.pop(name, None)
    headers = {}
    if variant == "token":  # made up for the test; the page signs in with it, the checks below send it as Bearer
        env["PENGUMON_ACCESS_TOKEN"] = env["PENGUMON_SMOKE_TOKEN"] = "smoke-token-not-a-secret"
        headers = {"Authorization": "Bearer smoke-token-not-a-secret"}
    # cwd: the credential forms write data/secrets.json relative to it, so they must not write into the repository
    server = subprocess.Popen([sys.executable, str(UI / "serve.py"), str(port)], cwd=tmp_path, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if urllib.request.urlopen(f"{base}/api/health", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(0.2)
        else:
            server.terminate()
            pytest.fail(f"the app did not start:\n{server.communicate(timeout=10)[0]}")
        for _ in range(100):  # the first service check ran before the port was open; the page loads the settled state
            services = urllib.request.Request(f"{base}/api/services", headers=headers)
            if '"status":"up"' in urllib.request.urlopen(services, timeout=5).read().decode():
                break
            time.sleep(0.2)
        else:
            pytest.fail("the service check of the app itself never came up")
        out_dir = tmp_path / "out"  # screenshots and the downloaded dossier; tmp_path is the only writable place in the container
        out_dir.mkdir()
        run = subprocess.run(["node", str(UI / "smoke.mjs"), base, str(out_dir), variant], env=env, capture_output=True, text=True, timeout=240)
        print(run.stdout)  # shown with -s: the steps passed and the sparkline cost
        assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
