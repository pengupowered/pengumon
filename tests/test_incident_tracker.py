"""IncidentTracker: onset, escalation and recovery of incidents from per-round verdicts."""

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.classifier.tracker import IncidentTracker
from pengumon.engine.classifier.verdict import NOMINAL, Verdict
from pengumon.engine.load import LoadReading

LOSS = Verdict("local_loss", "degraded", "local_wifi_mesh_drop", "Router Packet Loss", "dropping", "warning", ("a", "b"))
DOWN = Verdict("local_down", "down", "local_wifi_mesh_drop", "Router Disconnect", "gone", "critical", ("a",))
SLOW = Verdict("local_latency", "degraded", "local_wifi_mesh_drop", "Router Latency Spike", "slow", "warning")
BUSY = Verdict("busy_latency", "busy", "load_induced_latency", "Line Busy: Delay From Heavy Load", "busy", "info")
T0 = 1000.0


class FakeRepository:
    def __init__(self):
        self.calls = []

    async def create_incident(self, incident):
        self.calls.append(("create", incident.id))
        return 7

    async def resolve_incident(self, incident_id, end_time):
        self.calls.append(("resolve", incident_id, end_time))

    async def set_incident_load(self, incident_id, load_pct):
        self.calls.append(("load", incident_id, load_pct))

    async def update_incident_classification(self, incident_id, kind, cause_code, title, description, severity):
        self.calls.append(("classify", incident_id, kind, cause_code, title, description, severity))


def load(pct):
    return LoadReading(pct=pct, rx_mbps=1.0, tx_mbps=1.0, down_mbps=100.0, up_mbps=100.0,
                       down_source="configured", up_source="configured")


def make(repository=None, failures=3):
    config = ClassifierConfig(consecutive_failures_for_incident=failures)
    return IncidentTracker(config, repository)


class Clock:
    def __init__(self):
        self.now = T0

    def tick(self):
        self.now += 1.0
        return self.now


async def feed(tracker, verdict, rounds, clock, loss=True, reading=None):
    outcomes = []
    for _ in range(rounds):
        outcomes.append(await tracker.observe(verdict, is_loss_or_outage=loss, reading=reading, ts=clock.tick()))
    return outcomes


async def test_loss_incident_starts_on_the_configured_round_and_is_timed_from_onset():
    repo = FakeRepository()
    tracker, clock = make(repo, failures=3), Clock()
    outcomes = await feed(tracker, LOSS, 3, clock)
    assert [o.started is not None for o in outcomes] == [False, False, True]
    incident = outcomes[2].started
    assert incident is tracker.active_incident
    assert incident.start_time == round(T0 + 3.0 - 2.0, 2)
    assert (incident.cause_code, incident.title, incident.severity) == ("local_wifi_mesh_drop", "Router Packet Loss", "warning")
    assert incident.affected_targets == ["a", "b"]
    assert incident.id == 7
    assert repo.calls == [("create", None)]  # stored without an id, which the repository then assigns
    more = await feed(tracker, LOSS, 2, clock)
    assert all(o.started is None and o.resolved is None for o in more)


async def test_latency_incident_needs_ten_rounds():
    tracker, clock = make(failures=3), Clock()
    outcomes = await feed(tracker, SLOW, 10, clock, loss=False)
    assert [o.started is not None for o in outcomes] == [False] * 9 + [True]
    assert outcomes[9].started.start_time == round(T0 + 10.0 - 9.0, 2)


async def test_loss_and_latency_rounds_count_separately_and_reset_together():
    tracker, clock = make(failures=5), Clock()
    await feed(tracker, LOSS, 2, clock, loss=True)
    await feed(tracker, SLOW, 3, clock, loss=False)
    assert (tracker.loss_failure_counter, tracker.latency_failure_counter, tracker.failure_counter) == (2, 3, 3)
    await feed(tracker, LOSS, 1, clock, loss=True)
    assert (tracker.loss_failure_counter, tracker.latency_failure_counter, tracker.failure_counter) == (3, 3, 3)
    for healthy in (NOMINAL, BUSY):
        await feed(tracker, LOSS, 1, clock, loss=True)
        await feed(tracker, healthy, 1, clock, loss=False)
        assert (tracker.loss_failure_counter, tracker.latency_failure_counter, tracker.failure_counter) == (0, 0, 0)


async def test_escalation_updates_the_incident_and_stores_the_new_classification():
    repo = FakeRepository()
    tracker, clock = make(repo, failures=2), Clock()
    await feed(tracker, LOSS, 2, clock)
    incident = tracker.active_incident
    assert repo.calls == [("create", None)]
    outcome = (await feed(tracker, DOWN, 1, clock))[0]
    assert outcome.started is None and tracker.active_incident is incident
    assert (incident.severity, incident.kind, incident.title, incident.cause_code, incident.description) == (
        "critical", "local_down", "Router Disconnect", "local_wifi_mesh_drop", "gone")
    # stored, so the load backfills (which read the stored kind) never see the stale "local_loss"
    assert repo.calls == [("create", None),
                          ("classify", 7, "local_down", "local_wifi_mesh_drop", "Router Disconnect", "gone", "critical")]
    other = Verdict("gateway_down", "down", "double_nat_congestion", "Link Failure", "other", "critical")
    await feed(tracker, other, 1, clock)
    assert (incident.title, incident.description) == ("Router Disconnect", "gone")
    assert len(repo.calls) == 2  # already critical: no further escalation, no further write


async def test_peak_load_is_stored_at_resolution_only_when_it_changed():
    repo = FakeRepository()
    tracker, clock = make(repo, failures=2), Clock()
    await feed(tracker, LOSS, 2, clock, reading=load(30.0))
    assert tracker.active_incident.load_pct == 30.0
    await feed(tracker, LOSS, 1, clock, reading=load(55.0))
    await feed(tracker, LOSS, 1, clock, reading=load(40.0))
    assert tracker.active_incident.load_pct == 55.0
    outcomes = await feed(tracker, NOMINAL, 2, clock, reading=load(20.0))
    assert outcomes[1].resolved is not None
    assert [c[0] for c in repo.calls] == ["create", "resolve", "load"]
    assert repo.calls[2] == ("load", 7, 55.0)

    repo.calls.clear()
    await feed(tracker, LOSS, 2, clock, reading=load(30.0))
    await feed(tracker, NOMINAL, 2, clock, reading=load(20.0))
    assert [c[0] for c in repo.calls] == ["create", "resolve"]  # the peak is the stored value: no update


async def test_a_new_incident_without_a_reading_has_no_load():
    tracker, clock = make(failures=1), Clock()
    await feed(tracker, LOSS, 1, clock, reading=None)
    assert tracker.active_incident.load_pct is None


async def test_resolution_after_the_configured_healthy_rounds_at_the_last_failure():
    repo = FakeRepository()
    tracker, clock = make(repo, failures=3), Clock()
    await feed(tracker, LOSS, 4, clock)                       # failures at T0+1 .. T0+4, incident starts on the third, timed from T0+1
    incident = tracker.active_incident
    first = await feed(tracker, NOMINAL, 2, clock)
    assert all(o.resolved is None for o in first) and tracker.recovery_counter == 2
    await feed(tracker, LOSS, 1, clock)                       # a fault restarts the recovery count
    assert tracker.recovery_counter == 0
    outcomes = await feed(tracker, BUSY, 3, clock)            # busy counts as healthy
    assert [o.resolved is not None for o in outcomes] == [False, False, True]
    assert outcomes[2].resolved is incident and tracker.active_incident is None
    assert incident.resolved and incident.end_time == T0 + 7.0
    assert incident.duration_seconds == round(T0 + 7.0 - (T0 + 1.0), 1)
    assert ("resolve", 7, T0 + 7.0) in repo.calls


async def test_the_duration_is_at_least_one_second():
    tracker, clock = make(failures=1), Clock()
    await feed(tracker, LOSS, 1, clock)
    outcomes = await feed(tracker, NOMINAL, 1, clock)
    assert outcomes[0].resolved.duration_seconds == 1.0


async def test_without_a_repository_the_incident_has_no_id_and_nothing_is_stored():
    tracker, clock = make(None, failures=2), Clock()
    outcomes = await feed(tracker, LOSS, 2, clock)
    assert outcomes[1].started.id is None
    resolved = (await feed(tracker, NOMINAL, 2, clock))[1].resolved
    assert resolved is not None and resolved.resolved and resolved.id is None
