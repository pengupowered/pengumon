"""Intermediate-hop ICMP deprioritization: loss at ISP hops that the end targets do not share is not a fault."""

import time

import pytest

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import TargetConfig
from pengumon.db.migrations import legacy_incident_kind
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.load import (
    PING_BACKFILL_META_KEY,
    backfill_ping_deprioritization,
)
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample, TargetStats


def stats_with(isp, public=None, dns=None):
    def st(tid, cat, name, addr, lats):
        return TargetStats(target_id=tid, name=name, category=cat, address=addr, enabled=True,
                           recent_latencies=lats)
    stats = {
        "local": st("local", "local", "Local", "192.168.1.1", [1.0] * 10),
        "gateway": st("gateway", "gateway", "Gateway", "192.168.100.1", [2.0] * 10),
        "isp": st("isp", "isp", "Hop 3: ISP Regional Hub", "198.51.100.1", isp),
        "public": st("public", "public", "Public", "1.1.1.1", public or [14.0] * 10),
    }
    if dns:
        stats["dns"] = st("dns", "dns", "DNS", "google.com", dns)
    return stats


def samples_for(stats):
    return [
        ProbeSample(target_id=t, success=s.recent_latencies[-1] is not None, latency_ms=s.recent_latencies[-1])
        for t, s in stats.items()
    ]


async def run(stats, rounds=5):
    classifier = OutageClassifier(config=ClassifierConfig(consecutive_failures_for_incident=3))
    diagnosis = None
    for _ in range(rounds):
        diagnosis = await classifier.evaluate(samples_for(stats), stats)
    return classifier, diagnosis


@pytest.mark.asyncio
async def test_isp_hop_loss_that_does_not_reach_the_public_targets_is_healthy():
    classifier, diagnosis = await run(stats_with(isp=[None] * 3 + [10.0] * 7))
    assert diagnosis.status == "healthy"
    assert "Hop 3: ISP Regional Hub drops some ping replies, but later hops and public targets are fine" in diagnosis.description
    assert "not packet loss" in diagnosis.description
    assert classifier.active_incident is None
    assert classifier.loss_failure_counter == 0 and classifier.failure_counter == 0


@pytest.mark.asyncio
async def test_full_isp_hop_outage_with_healthy_public_targets_is_also_not_a_fault():
    classifier, diagnosis = await run(stats_with(isp=[None] * 10))  # 100% at the hop, public answers
    assert diagnosis.status == "healthy"
    assert classifier.active_incident is None


@pytest.mark.asyncio
async def test_isp_loss_with_comparable_public_loss_is_still_a_fault():
    # public 20% is above the 15% limit
    classifier, diagnosis = await run(stats_with(isp=[None] * 4 + [10.0] * 6, public=[None] * 2 + [14.0] * 8))
    assert diagnosis.status in ("degraded", "down")
    assert classifier.active_incident is not None

    # public 10% is under 15% but not under half of the 15% the ISP hop loses
    classifier, diagnosis = await run(stats_with(isp=[None] * 2 + [10.0] * 8, public=[None] * 1 + [14.0] * 9))
    assert diagnosis.status == "degraded"  # isp 20%, public 10%: not below half


@pytest.mark.asyncio
async def test_failing_dns_keeps_the_fault():
    classifier, diagnosis = await run(stats_with(isp=[None] * 3 + [10.0] * 7, dns=[None] * 4 + [20.0] * 6))
    assert diagnosis.status == "degraded"


@pytest.mark.asyncio
async def test_active_incident_resolves_once_only_the_hop_loss_remains():
    stats = stats_with(isp=[None] * 3 + [10.0] * 7, public=[None] * 3 + [14.0] * 7)
    classifier, diagnosis = await run(stats, rounds=3)
    assert classifier.active_incident is not None
    hop_only = stats_with(isp=[None] * 3 + [10.0] * 7)
    for _ in range(3):
        diagnosis = await classifier.evaluate(samples_for(hop_only), hop_only)
    assert diagnosis.status == "healthy"
    assert classifier.active_incident is None


# --- backfill ---------------------------------------------------------------------------------------

TARGETS = [
    TargetConfig(id="rur", name="Hop 3", type="icmp", category="isp", address="198.51.100.1"),
    TargetConfig(id="cf", name="Cloudflare", type="icmp", category="public", address="1.1.1.1"),
    TargetConfig(id="gw", name="Gateway", type="icmp", category="gateway", address="192.168.100.1"),
]


@pytest.mark.asyncio
async def test_backfill_marks_incidents_whose_public_targets_stayed_fine(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    base = time.time() - 3600

    async def incident(offset, title, duration=20.0):
        return await repo.create_incident(Incident(
            start_time=base + offset, end_time=base + offset + duration, duration_seconds=duration,
            cause_code="xfinity_last_mile_coax_drop", title=title, description="d", severity="warning",
            resolved=True, kind=legacy_incident_kind(title, "xfinity_last_mile_coax_drop"),  # what schema v6 labels it
        ))

    async def probe(offset, duration, isp_lost, public_lost):
        samples = []
        for i in range(int(duration)):
            t = base + offset + i
            samples.append(ProbeSample(target_id="rur", timestamp=t, success=i >= isp_lost, latency_ms=9.0))
            samples.append(ProbeSample(target_id="cf", timestamp=t, success=i >= public_lost, latency_ms=14.0))
            samples.append(ProbeSample(target_id="gw", timestamp=t, success=True, latency_ms=2.0))
        await repo.insert_samples(samples)

    hop_only = await incident(0, "ISP Line / Hop 3: Regional Hub Packet Loss")
    outage_hop_only = await incident(100, "ISP Line / Hop 3: Regional Hub Outage")
    real = await incident(200, "ISP Line / Hop 3: Regional Hub Packet Loss")
    no_samples = await incident(300, "ISP Line / Hop 3: Regional Hub Packet Loss")
    other_kind = await incident(0, "ISP Upstream Backbone Packet Loss")
    gateway = await incident(0, "Hop 2: Gateway Packet Loss")
    await probe(-10, 32, isp_lost=12, public_lost=0)  # 12 of 32
    await probe(90, 32, isp_lost=32, public_lost=1)  # all lost at the hop, 1 at public
    await probe(190, 32, isp_lost=12, public_lost=10)  # public 31% is not below half... and over 15%

    assert await backfill_ping_deprioritization(repo, TARGETS, 15.0) == 2
    got = {i.id: i for i in await repo.get_recent_incidents(limit=20)}
    assert got[hop_only].expected and got[outage_hop_only].expected
    assert not got[real].expected
    assert not got[no_samples].expected  # no raw samples: left alone
    assert not got[other_kind].expected and not got[gateway].expected
    assert await repo.get_meta(PING_BACKFILL_META_KEY)

    # Idempotent, and independent of the load backfill's key.
    await probe(290, 32, isp_lost=12, public_lost=0)
    assert await backfill_ping_deprioritization(repo, TARGETS, 15.0) is None
    assert not (await repo.get_incident_by_id(no_samples)).expected


# --- descriptions state the measurement, causes are hypotheses ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("make", [
    lambda: stats_with(isp=[10.0] * 10, public=[None] * 3 + [14.0] * 7),  # backbone loss
    lambda: stats_with(isp=[None] * 10, public=[None] * 10),  # outage
    lambda: stats_with(isp=[10.0] * 10, public=[250.0] * 10),  # latency
], ids=["backbone-loss", "outage", "latency"])
async def test_fault_descriptions_phrase_causes_as_possible_causes(make):
    _, diagnosis = await run(make())
    assert diagnosis.status in ("degraded", "down")
    assert "Possible causes:" in diagnosis.description
    assert "baseline ~12" not in diagnosis.description


@pytest.mark.asyncio
@pytest.mark.parametrize("degraded_loss_pct, marked", [(15.0, False), (20.0, True)])
async def test_backfill_judges_the_public_loss_against_the_degraded_level(tmp_path, degraded_loss_pct, marked):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    base = time.time() - 3600
    hop = await repo.create_incident(Incident(
        start_time=base, end_time=base + 20.0, duration_seconds=20.0, cause_code="access_line_drop",
        title="ISP Line / Hop 3 Packet Loss", description="d", severity="warning", resolved=True, kind="isp_loss"))
    samples = []
    for i in range(32):  # window [start - 10, end + 2]: the hop loses 32 of 32, the public target 6 (18.75 %)
        t = base - 10 + i
        samples.append(ProbeSample(target_id="rur", timestamp=t, success=False, latency_ms=None))
        samples.append(ProbeSample(target_id="cf", timestamp=t, success=i >= 6, latency_ms=14.0))
    await repo.insert_samples(samples)
    assert await backfill_ping_deprioritization(repo, TARGETS, degraded_loss_pct) == int(marked)
    assert (await repo.get_incident_by_id(hop)).expected is marked
