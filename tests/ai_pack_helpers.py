"""Hand-made facts for the AI pack tests, in the shapes of reports.ai_pack and capabilities.contrib (design 6.2).
Example values only. Every free-text field of the redaction seed carries a canary."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from pengumon.reports.ai_pack import (
    AppFact, ChangeFact, CloseUpFact, DayFact, HopFact, IncidentFact, ModuleInput, PackView, PackScope,
)

TZ = timezone(timedelta(hours=-5))


@dataclass(frozen=True)
class Section:               # capabilities.contrib.ModuleSection
    headline: Optional[str] = None
    findings: List[str] = field(default_factory=list)
    table: Optional[Tuple[List[str], List[List[str]]]] = None
    day_columns: Dict[str, Dict[str, str]] = field(default_factory=dict)
    shorter: Dict[int, Tuple[str, ...]] = field(default_factory=dict)


def ts(month: int, day: int, hh: int, mm: int = 0, ss: int = 0) -> float:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=TZ).timestamp()


HOME_CANARIES = ["Canary-Router-Name", "Canary-Modem-Name", "Canary-DNS-Home", "Canary-Public-Name", "Canary-ISP-Edge",
                 "192.168.88.1", "192.168.88.53", "192.168.100.1", "canary-modem.lan", "Canary-Router-Dev",
                 "Canary-Mesh-Kitchen", "Canary-Mesh-Hall", "Canary-App", "02:00:00:aa:bb:01", "canary-router-name",
                 "192-168-88-1", "hop-192-168-88-1", "canary-app.lan"]
NEVER_SHOWN = ["0000a1", "0000a2", "CANARY-DESC", "CANARY-ERROR", "http://canary-app.lan:8080/health"]
PROVIDER_CANARIES = ["198.51.100.1", "po-1.edge.example-isp.net", "192.0.2.53", "Example-ISP"]
MODEL = "Example-Mesh X1"

HOPS = (
    HopFact("hop-192-168-88-1", "local", "Canary-Router-Name", "192.168.88.1", normal_ms=1.9, p90_ms=3.1, jitter_ms=0.4,
            loss7_pct=0.01, window_ms=2.0, window_loss_pct=0.0, slow_minutes=12, lossy_minutes=9, error="CANARY-ERROR"),
    HopFact("gw-192-168-100-1", "gateway", "Canary-Modem-Name", "192.168.100.1", rdns="canary-modem.lan", normal_ms=3.4,
            p90_ms=6.0, jitter_ms=0.9, loss7_pct=0.31, window_ms=3.9, window_loss_pct=0.8, slow_minutes=40,
            lossy_minutes=61),
    HopFact("isp-198-51-100-1", "isp", "Canary-ISP-Edge", "198.51.100.1", rdns="po-1.edge.example-isp.net", normal_ms=11.0,
            p90_ms=15, jitter_ms=1.8, loss7_pct=0.33, window_ms=12.0, window_loss_pct=0.9, slow_minutes=52,
            lossy_minutes=64, ignores_ping=True),
    HopFact("pub-192-0-2-53", "public", "Canary-Public-Name", "192.0.2.53", normal_ms=18.2, p90_ms=24, jitter_ms=2.1,
            loss7_pct=0.34, window_ms=19.0, window_loss_pct=0.9, slow_minutes=61, lossy_minutes=66),
    HopFact("dns-home", "dns", "Canary-DNS-Home", "192.168.88.53", normal_ms=22.0, p90_ms=30, jitter_ms=3.0,
            loss7_pct=0.1, window_ms=23.0, window_loss_pct=0.1, slow_minutes=3, lossy_minutes=2),
)
ROUTER, LINE, ISP, PUBLIC, DNS = (h.id for h in HOPS)


def incidents(week: bool) -> Tuple[IncidentFact, ...]:
    m, d = (10, 30) if week else (11, 3)
    return (
        IncidentFact(1, ts(m, d, 21, 14, 3), 31, 14, LINE, "carried", "gateway_loss", "gateway", "high", False, 9.0,
                     ("codewords +312", "{role:mesh_node:2} at 0 clients"),
                     "Packet loss at Canary-Modem-Name (192.168.100.1) hop-192-168-88-1", description="CANARY-DESC"),
        IncidentFact(2, ts(m, d, 22, 40), 12, 6, ISP, "one_hop", "isp_loss", "line", "low", False, 1.0, (),
                     "Loss at po-1.edge.example-isp.net via Example-ISP from canary_router_name 192-168-88-1"),
        IncidentFact(3, ts(m, d + 1, 9, 5), 300, 0, None, "none", "busy_latency", "load", "medium", True, 0.2,
                     ("load 71 %",), "Latency while busy; client 02:00:00:aa:bb:01 downloading"),
    )


def modules(rich: bool = True, leaky: bool = False) -> Tuple[ModuleInput, ...]:
    if not rich:
        return ()
    wifi = Section(
        headline="Wi-Fi and mesh: node {role:mesh_node:2} fell to 0 clients once.",
        findings=["Online clients: median 14.", "Node Canary-Mesh-Kitchen was busiest." if leaky else "Busiest node: {role:mesh_node:1}.",
                  "No sharp drops."],
        table=(["Node", "Backhaul", "Median clients"],
               [["{role:mesh_node:1}", "wired", "9"], ["{role:mesh_node:2}", "Wi-Fi 5 GHz, -61 dBm (good)", "5"]]),
        day_columns={"Peak load %": {"2026-10-28": "41", "2026-10-29": "55", "2026-11-03": "30", "2026-11-04": "12"}})
    return (
        ModuleInput("docsis", "Cable line (DOCSIS)", "technology", 3000,
                    "How to read it: the modem talks to the provider over the coaxial cable on many channels at once.",
                    Section(headline="Cable line: 4 bursts of uncorrectable codewords.",
                            findings=["Downstream SNR 34.8-37.1 dB.", "2,310 new uncorrectable codewords."],
                            table=(["Dir", "Freq MHz", "Power dBmV"], [["DS", "537", "9.8"]])),
                    driver_note=("Device notes: the modem is an Xfinity gateway (XB7), supplied by the provider.",
                                 "Device notes: the modem is a cable gateway supplied by the provider."),
                    role_labels={"line_device": "Gateway"}, identifiers={"canary-modem.lan": "line_device"}),
        ModuleInput("router_system", "Router", "capability", 1200,
                    "How to read it: router CPU above 80 % for minutes can delay traffic.",
                    Section(findings=[f"Router model {MODEL}; CPU median 6 %."]),
                    driver_note=("Device notes: the router is an ASUS router with ASUS firmware (AsusWRT), on an AiMesh network.",
                                 "Device notes: the router reports counters every 10 s."),
                    role_labels={"router_device": "Canary-Router-Dev"}, identifiers={"Canary-Router-Dev": "router_device"},
                    field_values={MODEL: ("keep", "keep", "drop")}),
        ModuleInput("wifi_mesh", "Wi-Fi and mesh", "capability", 1800,
                    "How to read it: Pengumon counts the connected devices per Wi-Fi band and per mesh node.", wifi,
                    role_labels={"mesh_node:1": "Canary-Mesh-Kitchen", "mesh_node:2": "Canary-Mesh-Hall"},
                    identifiers={"Canary-Mesh-Kitchen": "mesh_node:1", "Canary-Mesh-Hall": "mesh_node:2",
                                 "0000a1": "never", "0000a2": "never"}),
    )


def canary_facts(week: bool = True, with_modules: bool = True, scope_kind: str = "window",
                 leaky: bool = False) -> PackView:
    start = ts(10, 28, 14) if week else ts(11, 3, 14)
    end = ts(11, 4, 14)
    incs = incidents(week)
    day1, day2 = ("2026-10-28", "2026-10-29") if week else ("2026-11-03", "2026-11-04")
    INC1 = (10, 30) if week else (11, 3)
    return PackView(
        scope=PackScope(scope_kind, "Past 7 days" if week else "Past 24 hours", start, end, TZ),
        version="0.22.0", provider="Example-ISP", hops=HOPS, incidents=incs,
        closeups=(CloseUpFact(1, ((LINE, 0.8), (ISP, 0.9), (ROUTER, 0.0)), ("codewords +312",),
                              ("{hop:%s} ms" % LINE, "{hop:%s} loss %%" % LINE),
                              ((ts(*INC1, 21, 12), ("3.4", "0")), (ts(*INC1, 21, 14), ("9.9", "40"))),
                              fine_rows=((ts(*INC1, 21, 14, 10), ("12.1", "100")),) if scope_kind == "incident" else None),),
        findings=("**Where problems start.** 1 of 3: loss starts at {hop:%s} and carries on." % LINE,
                  "**Time of day.** busiest 21:00."),
        time_table=(("Hours", "{hop:%s} ms" % LINE), (("18:00-24:00", "3.9"), ("06:00-12:00", "3.1"))),
        days=(DayFact(day1, 2, 0.6), DayFact(day2, 1, 0.1)),
        apps=(AppFact("Canary-App", True, url="http://canary-app.lan:8080/health", error="CANARY-ERROR to 192.168.88.7"),),
        changes=(ChangeFact(ts(*INC1, 20), ("Path changed: Canary-Router-Name added; " if leaky else "Path changed: ") + "{hop:%s} moved." % ISP),),
        environment=("Ping backend: raw sockets.", "IPv6: not available."),
        modules=modules(with_modules, leaky))


# --- the same canaries through build_ai_pack, with the real contributions ------------------------------------------
# A planted title (as an older version or an import could have stored it) carries what must not get through: a target
# switched off and the old name of a renamed one (Vague), the router's address in another IPv6 spelling (Vague), the
# models (kept at Vague, dropped at Anonymized), a mesh node id, a client's name and MAC and a password in a URL (no
# level). The modem's device id is "modem", as in config.example.yaml: a word of Pengumon's own text.
BUILT_TITLE = ("Canary-Old-Printer and Canary-Renamed-Old at 2001:DB8:88:0:0::1; Example-Mesh X1 and Example-Cable 3.1 "
               "node 0000a1, client Canary-Client-Phone 02-00-00-AA-BB-01 via http://admin:CanaryPass@canary-router.lan/")
BUILT_TARGETS = [
    {"id": "router", "name": "Canary-Router-Name", "category": "local", "address": "2001:db8:88::1"},
    {"id": "gw-192-168-100-1", "name": "Canary-Modem-Name", "category": "gateway", "address": "192.168.100.1"},
    {"id": "isp-198-51-100-1", "name": "Canary-ISP-Edge", "category": "isp", "address": "198.51.100.1"},
    {"id": "pub-192-0-2-53", "name": "Canary-Public-Name 02:00:00:aa:bb:02", "category": "public",
     "address": "192.0.2.53"},  # a MAC typed into a name the hop table shows as typed: only the final scan sees it
    {"id": "printer", "name": "Canary-Old-Printer", "category": "custom", "address": "192.168.88.77", "enabled": False},
]


async def built_pack(tmp_path, monkeypatch, level: str) -> dict:
    """build_ai_pack's answer at `level` for a seeded day: the canary targets (one switched off, the modem hop renamed
    since the first path version), a modem and a router with a two-node mesh and one client, and an incident whose
    title is BUILT_TITLE."""
    import sqlite3
    import time
    from types import SimpleNamespace

    from ai_pack_seed import add_incident, seed, week_end
    from pengumon.capabilities import BAND_5G, Client, ClientsReading, MeshNode, MeshReading
    from pengumon.config import AppConfig
    from pengumon.db.repository import DatabaseRepository
    from pengumon.models.probes import TargetConfig
    from pengumon.reports.contributions import build_ai_pack

    config = AppConfig(targets=BUILT_TARGETS, dns_probes=[], devices=[
        {"id": "modem", "driver": "xfinity_xb", "url": "http://canary-modem.lan", "model": "Example-Cable 3.1"},
        {"id": "Canary-Router-Dev", "driver": "asuswrt", "url": "http://canary-router.lan", "model": MODEL,
         "options": {"mesh_node_labels": {"0000a1": "Canary-Mesh-Kitchen"}}}])
    monitor = SimpleNamespace(
        latest_mesh=MeshReading(nodes=(MeshNode(id="0000a1", label="Canary-Mesh-Kitchen", clients=9, backhaul="wired"),
                                       MeshNode(id="0000a2", label="Canary-Mesh-Hall", clients=5, backhaul="wifi",
                                                backhaul_band=BAND_5G, backhaul_rssi_dbm=-61))),
        latest_clients=ClientsReading((Client(key="02:00:00:aa:bb:01", label_parts=("Canary-Client-Phone",),
                                              mac="02:00:00:aa:bb:01"),)))
    live = SimpleNamespace(providing=lambda capability: (SimpleNamespace(monitor=monitor),))
    repo = DatabaseRepository(db_path=str(tmp_path / "pack.db"))
    await repo.initialize()
    try:
        enabled = SimpleNamespace(targets=[t for t in config.targets if t.enabled], dns_probes=[])
        end = week_end()
        ids = await seed(repo, enabled, end)
        iid = await add_incident(repo, enabled, ids, end - 7200, 30, "gw-192-168-100-1")
        con = sqlite3.connect(repo.db_path)
        con.execute("UPDATE incidents SET title = ?, description = ? WHERE id = ?", (BUILT_TITLE, "CANARY-DESC", iid))
        con.commit()
        con.close()
        first = [TargetConfig(**{**t, "name": "Canary-Renamed-Old"} if t["category"] == "gateway" else t) for t in BUILT_TARGETS]
        await repo.import_path(first)
        await repo.save_path(await repo.path_revision(), list(config.targets))
        monkeypatch.setattr(time, "time", lambda: end + 300)
        return await build_ai_pack(repo, config, scope="window", hours=24, incident_id=None, level=level,
                                   detail="short", devices=tuple(config.devices), live=live)
    finally:
        await repo.close()
