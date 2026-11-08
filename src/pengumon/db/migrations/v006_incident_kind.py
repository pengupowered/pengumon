"""v6: incident kind, backfilled once from the titles. `backfill_incident_kinds` also runs on every start."""

import aiosqlite

from pengumon.db.migrations.common import columns

VERSION = 6


_LATENCY_WORDS = ("Latency", "Jitter", "Delay")
_FIXED_TITLES = {
    "ISP Latency Spikes / Jitter": "upstream_latency",
    "ISP Upstream Backbone Packet Loss": "backbone_loss",
    "DNS Resolution Outage": "dns_outage",
    "DNS Query Packet Drops": "dns_loss",
    "Slow DNS Queries": "dns_slow",
    "All Network Hops Operational": "nominal",
    # the titles as they were stored (British spelling) and as they are since v16 (US): the kind is the current one
    "ISP Hop Deprioritises Ping": "hop_deprioritized",
    "ISP Hop Deprioritizes Ping": "hop_deprioritized",
    "Line Busy: ISP Hops Deprioritise Ping": "busy_isp_hops",
    "Line Busy: ISP Hops Deprioritize Ping": "busy_isp_hops",
}


def legacy_incident_kind(title: str, cause_code: str) -> str:
    """The kind of an incident stored before schema v6, from its title (and cause, where two kinds share a
    title template). Frozen copy of the classifier's titles at v6 plus the older titles found in real data;
    it must never import from the engine, which will keep changing."""
    if title.startswith("ISP Line / "):
        if title.endswith(" Packet Loss"):
            return "isp_loss"
        if title.endswith(" Outage"):
            return "isp_outage"
    if title in _FIXED_TITLES:
        return _FIXED_TITLES[title]
    if title.endswith(" Latency Spike"):
        return "local_latency"
    if title.endswith(" Latency Delay"):
        return "gateway_latency"
    if title.startswith("Line Busy: Delay"):
        return "busy_latency"
    if any(word in title for word in _LATENCY_WORDS):
        return "legacy_latency"
    if title.endswith(" Link Failure"):
        return "gateway_down"
    if title.endswith(" Disconnect"):
        return "local_down"
    if title.endswith(" Packet Loss") and cause_code == "local_wifi_mesh_drop":
        return "local_loss"
    if title.endswith(" Packet Loss") and cause_code == "double_nat_congestion":
        return "gateway_loss"
    return "legacy_other"


async def migrate(db: aiosqlite.Connection):
    """Incident kind: what the classifier diagnosed, so code stops matching display titles."""
    if "kind" not in await columns(db, "incidents"):
        await db.execute("ALTER TABLE incidents ADD COLUMN kind TEXT")
    await backfill_incident_kinds(db)


async def backfill_incident_kinds(db: aiosqlite.Connection) -> int:
    """Gives every incident without a kind the one its title implies; idempotent, so it also runs on every start
    (an older build still running against the database writes incidents with kind NULL). Returns the count."""
    async with db.execute("SELECT id, title, cause_code FROM incidents WHERE kind IS NULL") as cursor:
        rows = await cursor.fetchall()
    await db.executemany("UPDATE incidents SET kind = ? WHERE id = ?",
                         [(legacy_incident_kind(title, cause), row_id) for row_id, title, cause in rows])
    return len(rows)
