"""A seeded AppState for the MCP tests: a week of rollups with one incident, a probe path with live statistics, a
modem and a router that were polled once (against a refused connection), a service check, and a marker string in every
slot that must never reach a tool's output (the sign-in token, every secret slot, the device credentials). Example
values only (192.0.2.x, 198.51.100.x, 192.168.88.x)."""

import time
from types import SimpleNamespace
from typing import Dict

import httpx

from ai_pack_seed import add_incident, seed, week_end
from device_helpers import build_modem, build_router
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.registry import DeviceRegistry
from pengumon.incidents.model import SystemDiagnosis
from pengumon.models.probes import TargetStats
from pengumon.secrets import CredentialSources, SecretStore
from pengumon.services.checker import ServiceChecker
from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.state import AppState
from pengumon.speedtest.schedule import month_bounds
from pengumon.timeofday import zone as named_zone

TOKEN = "CANARY-access-token-0001"
# every secret the app can hold, each with its own marker
SECRET_MARKERS: Dict[str, str] = {
    "access_token": TOKEN,
    "ntfy.url": "https://ntfy.example.com/CANARY-ntfy-url-0002",
    "ntfy.token": "CANARY-ntfy-token-0003",
    "webhook.hook.url": "https://hooks.example.com/CANARY-webhook-url-0004",
    "webhook_header.X-Key": "CANARY-header-secret-0005",
    "modem.password": "CANARY-modem-password-0006",
    "modem.username": "CANARY-modem-user-0007",
    "router.password": "CANARY-router-password-0008",
    "router.username": "CANARY-router-user-0009",
}
MARKERS = tuple(SECRET_MARKERS.values())
# a device's own addresses: not secrets (the dashboard shows them), but the tools do not need to repeat them
DEVICE_HOSTS = ("canary-modem.lan", "canary-router.lan")
# the host of the household's own LibreSpeed server: the speed test names its provider, never this address
LIBRESPEED_HOST = "canary-librespeed.lan"

TARGETS = [
    {"id": "router", "name": "Example-Mesh main", "category": "local", "address": "192.168.88.1"},
    {"id": "gw", "name": "Example gateway", "category": "gateway", "address": "192.168.100.1"},
    {"id": "isp1", "name": "po-1.edge.example-isp.net", "category": "isp", "address": "198.51.100.1"},
    {"id": "net1", "name": "Public resolver", "category": "public", "address": "192.0.2.1"},
]


def secret_data() -> dict:
    m = SECRET_MARKERS
    return {"access_token": m["access_token"], "ntfy": {"url": m["ntfy.url"], "token": m["ntfy.token"]},
            "webhook_urls": {"hook": m["webhook.hook.url"]}, "webhooks": {"X-Key": m["webhook_header.X-Key"]},
            "devices": {"modem": {"username": m["modem.username"], "password": m["modem.password"]},
                        "router": {"username": m["router.username"], "password": m["router.password"]}}}


def _refuse(request):
    raise httpx.ConnectError("refused")


def _stat(target: dict, **kw) -> TargetStats:
    return TargetStats(target_id=target["id"], name=target["name"], category=target["category"],
                       address=target["address"], enabled=True, **kw)


async def build_state(tmp_path, *, enabled: bool = True) -> AppState:
    """The state; the caller closes `state.repository`."""
    config = AppConfig(
        targets=TARGETS, dns_probes=[], mcp={"enabled": enabled}, general={"timezone": "America/New_York"},
        speedtest={"enabled": True, "provider": "librespeed", "librespeed_url": f"http://{LIBRESPEED_HOST}/backend",
                   "monthly_cap_mb": 500},
        line={"down_mbps": 300, "up_mbps": 20},
        service_checks={"services": [{"id": "app", "name": "Example App", "url": "http://192.0.2.9:9000/health"}]},
        devices=[{"id": "modem", "driver": "xfinity_xb", "url": "http://canary-modem.lan", "model": "Example-Cable 3.1",
                  "username": SECRET_MARKERS["modem.username"]},
                 {"id": "router", "driver": "asuswrt", "url": "http://canary-router.lan", "model": "Example-Mesh X1",
                  "username": SECRET_MARKERS["router.username"]}])
    repo = DatabaseRepository(db_path=str(tmp_path / "mcp.db"))
    await repo.initialize()
    seeding = SimpleNamespace(targets=[SimpleNamespace(id=t["id"], name=t["name"], category=t["category"],
                                                       address=t["address"], enabled=True) for t in TARGETS],
                              dns_probes=[])
    end = week_end()
    ids = await seed(repo, seeding, end)
    incident_id = await add_incident(repo, seeding, ids, end - 7200, 30, "gw")

    now = time.time()
    # inside this month (America/New_York), however close to its start the run is: the tool counts this month's data
    month_start = month_bounds(now, named_zone("America/New_York"))[0]
    for k, (down, up, status, reason) in enumerate([(280.0, 19.0, "ok", None), (95.0, 18.5, "ok", None),
                                                    (None, None, "failed", f"{LIBRESPEED_HOST} answered HTTP 404")]):
        await repo.insert_speed_test({
            "ts": max(now - 60 * (k + 1), month_start + (3 - k) * 0.001), "duration_s": 12.0, "trigger": "scheduled", "provider": "librespeed",
            "server": LIBRESPEED_HOST, "status": status, "reason": reason, "down_mbps": down, "up_mbps": up,
            "idle_ms": 12.0 if down else None, "loaded_down_ms": 50.0 if down else None,
            "loaded_up_ms": 30.0 if down else None, "bytes_down": 100_000_000 if down else 0,
            "bytes_up": 10_000_000 if down else 0})

    sources = CredentialSources(env={}, secrets_dir=None, store=secret_data())
    store = SecretStore(data=secret_data())
    modem = build_modem(repo, password=SECRET_MARKERS["modem.password"], url="http://canary-modem.lan", model="Example-Cable 3.1",
                        username=SECRET_MARKERS["modem.username"],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
    router = build_router(repo, password=SECRET_MARKERS["router.password"], url="http://canary-router.lan", model="Example-Mesh X1",
                          username=SECRET_MARKERS["router.username"],
                          client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
    for device in (modem, router):
        await device.poll(force=True, bypass_gate=True)

    stats = {t["id"]: _stat(t, last_ms=3.0 * (i + 1), jitter_ms=0.5, loss_recent_pct=1.0 if t["id"] == "isp1" else 0.0)
             for i, t in enumerate(TARGETS)}
    stats["isp1"].status = "degraded"
    prober = SimpleNamespace(stats=stats, probe_method=lambda: {"backend": "raw", "resolution_ms": 0.1, "error": None},
                             probing_problem=lambda: None)
    checks = ServiceChecksConfig(services=[ServiceCheckConfig(id="app", name="Example App",
                                                              url="http://192.0.2.9:9000/health")])
    state = AppState(
        config=config, repository=repo, prober=prober,
        classifier=SimpleNamespace(last_diagnosis=SystemDiagnosis(
            status="degraded", cause_code="nominal", title="Example title", description="Example description",
            timestamp=time.time(), load_pct=12.5)),
        devices=DeviceRegistry([modem, router]), secret_store=store, credentials=sources,
        service_checker=ServiceChecker(checks, repo))
    state.example_incident_id = incident_id  # type: ignore[attr-defined]
    return state


# --- every device credential kind, and what a driver's error message can carry -------------------------------------

# The device kinds build_state has not got, each with its credentials as markers: OpenWrt (a login), SNMP (the community
# string is the password), a second ASUS router and a UPnP router (no credentials). Their hosts are markers too: a
# driver's error names the host (and OpenWrt's the login it tried), a socket error the address it dialed.
DEVICE_MARKERS: Dict[str, str] = {
    "owrt.username": "CANARY-owrt-user-0010",
    "owrt.password": "CANARY-owrt-password-0011",
    "snmp.community": "CANARY-snmp-community-0012",
    "asus2.username": "CANARY-asus2-user-0013",
    "asus2.password": "CANARY-asus2-password-0014",
}
MORE_DEVICE_HOSTS = ("canary-owrt.lan", "canary-asus2.lan", "192.168.88.77")
# a connected client, named and addressed: only counts may leave, whatever privacy.client_details says
CLIENT_MARKERS = ("Canary-Client-Laptop", "02:00:00:ca:fe:01", "192.168.88.201")


def _owrt_refuses_the_login(request):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": [6]})  # ubus: permission denied


def _asus_refuses_the_login(request):
    return httpx.Response(200, json={"error_status": "3"})


def _connect_fails(request):
    raise httpx.ConnectError("[Errno 111] Connect call failed ('192.168.88.77', 80)")


async def add_every_device_kind(state) -> None:
    """Adds the devices of DEVICE_MARKERS to `state`, each polled once (and failing, so that its error message is the
    latest snapshot's), a named client on the router, privacy.client_details "full", and a notifier holding the ntfy
    and webhook secrets of SECRET_MARKERS."""
    from pengumon.capabilities import Client, ClientsReading
    from pengumon.devices.registry import build_device
    from pengumon.devices.settings import DeviceConfig
    from pengumon.notify.notifier import Notifier
    from pengumon.notify.settings import NotificationsConfig
    from pengumon.secrets import password_env_var

    m = DEVICE_MARKERS

    def device(cfg, password, handler=None):
        env = {password_env_var(cfg["id"]): password} if password else {}
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
        sources = CredentialSources(env=env, secrets_dir=None, store={})
        return build_device(DeviceConfig(**cfg), state.repository, sources, client=client)

    added = [
        device({"id": "owrt", "driver": "openwrt", "url": "http://canary-owrt.lan", "username": m["owrt.username"]},
               m["owrt.password"], _owrt_refuses_the_login),
        device({"id": "sw", "driver": "snmp", "url": "snmp://127.0.0.1:9",
                "options": {"timeout_seconds": 0.2, "retries": 0}}, m["snmp.community"]),
        device({"id": "asus2", "driver": "asuswrt", "url": "http://canary-asus2.lan", "username": m["asus2.username"]},
               m["asus2.password"], _asus_refuses_the_login),
        device({"id": "igd", "driver": "upnp_igd", "url": "http://192.168.88.77"}, None, _connect_fails),
    ]
    for d in added:
        await d.poll(force=True, bypass_gate=True)
    state.devices = DeviceRegistry([*state.devices.all(), *added])

    router = state.devices.get("router")
    name, mac, ip = CLIENT_MARKERS
    router.monitor.latest_clients = ClientsReading((Client(key=mac, label_parts=(name,), ip=ip, mac=mac,
                                                           connection="wifi", band="5"),))
    router.monitor.clients_at = time.time()
    state.config.privacy.client_details = "full"

    settings = NotificationsConfig(webhooks=[{"id": "hook", "name": "Hook", "url": SECRET_MARKERS["webhook.hook.url"],
                                              "headers": {"X-Key": "secret:X-Key"}}])
    state.config.notifications = settings
    state.notifier = Notifier.build(settings, store=SecretStore(data=secret_data()), env={}, secrets_dir=None)
