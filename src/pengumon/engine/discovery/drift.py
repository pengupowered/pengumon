"""Route-change checks: discovery rerun quietly in the background, compared with the applied path, so a provider's
route change is reported ("your network path changed: hop 3 is new") instead of waiting for a manual Rediscover.

When: once a day, at `discovery.daily_check_time` (HH:MM in general.timezone) or else at a minute picked at random
when the app starts (UTC), and INCIDENT_DELAY_S after an incident ends (at most one such check an hour). Only while
a path is applied (revision 1 or later) and `discovery.background` is on. The checker always exists; it reads
`background`, `asn_lookup` and the time from the live settings, and `wake()` (the settings hub calls it) makes the
daily loop work out its next check again.
How: a discovery run with no device checks except the common gateway addresses off the path (one ping and one GET
each, engine.GATEWAY_ADDRESSES; a configured device's host gets nothing: its driver's gate owns it), no SNMP, and the ASN
lookup only when `discovery.asn_lookup` is on (off by default). A run that finds changes is repeated once,
CONFIRM_DELAY_S later, and only the changes both runs saw count: one trace that lost a hop is not a route change.

What counts (`route_changes`): your network's, the line's and the provider's hops (local, gateway, isp), since
internet hops past the provider and the public servers vary from one trace to the next.
* new: a hop discovery proposes that the applied path does not have, and that was not removed from it before (a hop
  the owner removed and that is still there is no news);
* gone: an applied hop discovery did not see (a home hop only when it does not answer a ping either);
* changed: an applied hop that is now in another place among the others (the role alone is not compared: a
  background run has less evidence than the owner's run had).
A failed check (discovery failed, or no trace reached a public server, as in an outage) compares nothing.

The latest check is stored (meta path_drift): what discovery proposed, which changes both runs confirmed, which the
owner dismissed, and which set was announced. GET /api/path/drift compares the stored proposal with the path as it is
applied now, so after Apply (of that draft or anything else) the changes it settled are gone without a new check.
A set of changes raises one notification (Notifier.notify_path_changed); the same set is not announced again, and a
dismissed change is not shown or announced again while later checks keep seeing it.
"""

import asyncio
import logging
import random
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from pengumon.engine.discovery.draft import address_key, diff_against, trim
from pengumon.engine.discovery.engine import DiscoveryOptions, DiscoveryResult, discovery_trigger
from pengumon.observability.telemetry import drift_checks, route_changes as route_change_count, tracer
from pengumon.timeofday import next_wall_time, zone

logger = logging.getLogger("pengumon.discovery")

DRIFT_CATEGORIES = ("local", "gateway", "isp")
DAY_SECONDS = 86400.0
INCIDENT_DELAY_S = 600.0
INCIDENT_MIN_GAP_S = 3600.0
CONFIRM_DELAY_S = 60.0

Run = Callable[[DiscoveryOptions], Awaitable[DiscoveryResult]]
# The applied path: (revision, live hops, hops removed from it), as GET /api/path hops (engine.live_path_from_targets)
Paths = Callable[[], Awaitable[Tuple[int, List[dict], List[dict]]]]


def change_key(change: dict) -> str:
    return f"{change['kind']}:{address_key(change['hop'].get('address'))}"


def _longest_increasing(values: List[int]) -> Set[int]:
    """Indexes (into `values`) of one longest strictly increasing subsequence."""
    best: List[int] = [1] * len(values)
    back: List[int] = [-1] * len(values)
    for i in range(len(values)):
        for j in range(i):
            if values[j] < values[i] and best[j] + 1 > best[i]:
                best[i], back[i] = best[j] + 1, j
    if not values:
        return set()
    i = max(range(len(values)), key=lambda k: best[k])
    keep = set()
    while i >= 0:
        keep.add(i)
        i = back[i]
    return keep


def _named(h: dict) -> str:
    """A hop's name with its address, unless the name already shows the address."""
    name, address = h.get("name") or "A hop", h.get("address")
    return name if not address or str(address) in name else f"{name} ({address})"


def route_changes(draft: Sequence[dict], live: Sequence[dict], removed: Sequence[dict] = ()) -> List[dict]:
    """The changes a draft (diffed against `live`, the applied path) shows, as the module docstring says. Each is
    {"kind": "new"|"gone"|"changed", "hop": {id, name, address, category, position}, "text": plain words}."""
    removed_ids = {h.get("id") for h in removed}
    live_by_id = {h.get("id"): h for h in live}
    on_route = [h for h in draft if h.get("status") != "gone"]
    number = {h.get("id"): n for n, h in enumerate(on_route, start=1)}  # "hop 3": its place on the route now

    def hop(h: dict) -> dict:
        return {k: h.get(k) for k in ("id", "name", "address", "category", "position")}

    changes: List[dict] = []
    for h in draft:
        status, category = h.get("status"), h.get("category")
        if status == "new" and category in DRIFT_CATEGORIES and h.get("id") not in removed_ids:
            changes.append({"kind": "new", "hop": hop(h),
                            "text": f"Hop {number[h.get('id')]} is new: {_named(h)}."})
        elif status == "gone" and (live_by_id.get(h.get("id")) or h).get("category") in DRIFT_CATEGORIES:
            changes.append({"kind": "gone", "hop": hop(h),
                            "text": f"{_named(h)} is no longer on the route."})
    # moved: applied hops (of the categories above) still on the route, whose order among themselves changed
    matched = [h for h in draft if h.get("status") in ("same", "changed") and h.get("id") in live_by_id
               and live_by_id[h.get("id")].get("category") in DRIFT_CATEGORIES]
    live_rank = {h.get("id"): n for n, h in enumerate(sorted(live, key=lambda h: h.get("position") or 0))}
    ranks = [live_rank[h.get("id")] for h in matched]
    keep = _longest_increasing(ranks)
    for n, h in enumerate(matched):
        if n not in keep:
            changes.append({"kind": "changed", "hop": hop(h),
                            "text": f"{_named(h)} moved: it is hop {number[h.get('id')]} "
                                    "now."})
    return changes


def notice_line(change: dict) -> str:
    """A change in the words of a notification (no addresses: a notification may go through an outside service)."""
    name = change["hop"].get("name") or "A hop"
    if change["kind"] == "new":
        return f"A new hop on the route: {name}."
    if change["kind"] == "gone":
        return f"{name} is no longer on the route."
    return f"{name} moved to another place on the route."


def _reached(result: DiscoveryResult) -> bool:
    path = getattr(result, "path", None)
    return path is not None and any((path.reached or {}).values())


class DriftChecker:
    def __init__(self, *, run: Run, paths: Paths, repository, jobs=None,
                 notify: Optional[Callable[[List[str]], None]] = None, asn_lookup: bool = False,
                 settings: Any = None, timezone: Callable[[], str] = lambda: "UTC",
                 clock: Callable[[], float] = time.time, rng: Optional[random.Random] = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, confirm_delay: float = CONFIRM_DELAY_S):
        self._run = run
        self._paths = paths
        self._repository = repository
        self._jobs = jobs
        self._notify = notify
        self._settings = settings  # the live DiscoverySettings: background, asn_lookup, daily_check_time
        self._asn_lookup = asn_lookup  # used when there are no settings
        self._timezone = timezone
        self._wake = asyncio.Event()
        self._clock = clock
        self._sleep = sleep
        self._confirm_delay = confirm_delay
        self.minute = (rng or random.Random()).randrange(24 * 60)  # the daily check's minute (UTC)
        self._lock = asyncio.Lock()
        self._record: Dict[str, Any] = {}
        self._last_started: Optional[float] = None
        self._incident_task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return True if self._settings is None else bool(self._settings.background)

    @property
    def options(self) -> DiscoveryOptions:
        asn = self._asn_lookup if self._settings is None else bool(self._settings.asn_lookup)
        return DiscoveryOptions(asn_lookup=asn, device_checks=False, snmp=False, off_path_gateways=True)

    def wake(self) -> None:
        """The settings changed: the daily loop works out when its next check is."""
        self._wake.set()

    # --- the stored record ---------------------------------------------------------------------------------------

    async def load(self) -> None:
        """At start: the stored latest check, and its draft back in the job store under its id."""
        self._record = await self._repository.load_path_drift() or {}
        started = self._record.get("started_at")
        self._last_started = float(started) if isinstance(started, (int, float)) else None
        await self.public()  # pins the draft, compared with the path as it is applied now

    async def _save(self) -> None:
        await self._repository.save_path_drift(self._record)

    # --- the checks ----------------------------------------------------------------------------------------------

    async def check(self, reason: str = "daily") -> dict:
        """One check (single flight: a check asked while one runs waits for it, then runs). Returns the public
        state."""
        async with self._lock:
            started = self._clock()
            revision, live, removed = await self._paths()
            if revision <= 0:
                return await self.public()  # nothing applied yet: the first-run discovery is the owner's
            self._last_started = started
            record = dict(self._record, started_at=started, reason=reason)
            with tracer.start_as_current_span("discovery.drift_check", attributes={"drift.reason": reason}) as span:
                try:
                    result = await self._discover(reason)
                    if not _reached(result):
                        raise _NotReached()
                    first = {change_key(c) for c in route_changes(result.hops, live, removed)}
                    self._count_changes(first, "found")
                    confirmed = first
                    if first:
                        await self._sleep(self._confirm_delay)
                        revision, live, removed = await self._paths()
                        result = await self._discover("confirm")
                        if not _reached(result):
                            raise _NotReached()  # nothing confirmed: the next check tries again
                        confirmed = first & {change_key(c) for c in route_changes(result.hops, live, removed)}
                        self._count_changes(confirmed, "confirmed")
                    record.update(state="checked", error=None, proposed=result.proposed, answering=result.answering,
                                  notes=result.notes, confirmed=sorted(confirmed), discovery_id=secrets.token_hex(8))
                except _NotReached:
                    record.update(state="failed", error="No trace reached a public server, so the route was not "
                                                        "compared (the connection may have been down).")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Route-change check failed: {e}", exc_info=True)
                    record.update(state="failed", error=f"Discovery failed: {type(e).__name__}")
                outcome = "failed" if record["state"] == "failed" else ("changed" if record["confirmed"] else "unchanged")
                span.set_attribute("drift.outcome", outcome)
                drift_checks.add(1, {"reason": reason, "outcome": outcome})
            record["checked_at"] = self._clock()
            if record["state"] == "checked":
                record["good_at"] = record["checked_at"]  # a later failed check keeps this: when the shown changes were found
            self._record = record
            public = await self.public()
            if record["state"] == "checked":
                # a dismissed change is forgotten once a check no longer sees it (so its return is news again)
                current = {change_key(c) for c in self._changes(public)}
                record["dismissed"] = sorted(set(record.get("dismissed") or []) & (set(record["confirmed"])))
                self._announce(public, current)
            await self._save()
            return public

    async def _discover(self, trigger: str) -> DiscoveryResult:
        """One discovery run for the check, tagged for the telemetry with why it runs (daily, incident, confirm)."""
        token = discovery_trigger.set(trigger)
        try:
            return await self._run(self.options)
        finally:
            discovery_trigger.reset(token)

    @staticmethod
    def _count_changes(keys: Set[str], stage: str) -> None:
        for key in keys:
            route_change_count.add(1, {"kind": key.split(":", 1)[0], "stage": stage})

    def _changes(self, public: dict) -> List[dict]:
        return public.get("changes") or []

    def _announce(self, public: dict, keys: Set[str]) -> None:
        announced = "|".join(sorted(keys))
        if not keys:
            self._record["announced"] = ""
            return
        if announced == self._record.get("announced"):
            return
        self._record["announced"] = announced
        if self._notify is not None:
            try:
                self._notify([notice_line(c) for c in self._changes(public)])
            except Exception as e:
                logger.error(f"Route-change notification failed: {e}", exc_info=True)

    async def public(self) -> dict:
        """GET /api/path/drift: the latest check compared with the path as it is applied now."""
        record = self._record
        state = record.get("state") if self.enabled else None  # background checks off: nothing to show
        body = {"checked_at": record.get("checked_at"), "state": "never", "changes": [],
                "discovery_id": record.get("discovery_id"), "error": record.get("error"), "good_at": record.get("good_at")}
        if state is None:
            return body
        failed = state == "failed"
        if failed and not record.get("proposed"):
            return {**body, "state": "failed", "discovery_id": None}  # no earlier good check to keep
        revision, live, removed = await self._paths()
        draft = trim(diff_against([dict(h) for h in record.get("proposed") or []], live,
                                  record.get("answering") or [], removed))
        confirmed = set(record.get("confirmed") or [])
        dismissed = set(record.get("dismissed") or [])
        changes = [c for c in route_changes(draft, live, removed)
                   if change_key(c) in confirmed and change_key(c) not in dismissed]
        if self._jobs is not None and record.get("discovery_id"):
            self._jobs.pin(record["discovery_id"], self.options, draft, record.get("notes") or [])
        if failed:  # a failed check compares nothing new: the changes of the last good check stay until reviewed
            return {**body, "state": "failed", "changes": changes}
        return {**body, "state": "changed" if changes else "same", "changes": changes}

    async def dismiss(self) -> dict:
        """POST /api/path/drift/dismiss: the changes shown now are seen; they are not shown or announced again."""
        async with self._lock:
            public = await self.public()
            keys = {change_key(c) for c in self._changes(public)}
            self._record["dismissed"] = sorted(set(self._record.get("dismissed") or []) | keys)
            await self._save()
            return await self.public()

    # --- when ----------------------------------------------------------------------------------------------------

    def next_daily(self, now: float) -> float:
        """The next time of the daily check after `now`: the configured time of day in the configured zone, or the
        random minute (UTC)."""
        at_time = getattr(self._settings, "daily_check_time", None)
        if at_time:
            return next_wall_time(now, at_time, zone(self._timezone()))
        at = (now // DAY_SECONDS) * DAY_SECONDS + self.minute * 60
        return at if at > now else at + DAY_SECONDS

    async def _wait(self, delay: Optional[float]) -> bool:
        """Waits `delay` seconds (forever for None) or until wake(); True when it was woken."""
        woke = asyncio.ensure_future(self._wake.wait())
        timer = asyncio.ensure_future(self._sleep(delay)) if delay is not None else None
        try:
            await asyncio.wait([t for t in (woke, timer) if t is not None], return_when=asyncio.FIRST_COMPLETED)
            was_woken = woke.done()
            if timer is not None and timer.done() and not was_woken:
                timer.result()  # a sleep that was cancelled or failed ends the loop, as awaiting it directly would
        finally:
            for t in (woke, timer):
                if t is not None and not t.done():
                    t.cancel()
        self._wake.clear()
        return was_woken

    async def run_daily(self) -> None:
        """The daily loop (spawned at start; cancelled at shutdown). Switched off, it waits for a change."""
        while True:
            if not self.enabled:
                await self._wait(None)
                continue
            if await self._wait(max(0.0, self.next_daily(self._clock()) - self._clock())):
                continue  # the settings changed: work the next check out again
            if not self.enabled:
                continue
            try:
                await self.check("daily")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never ends the loop
                logger.error(f"Route-change check failed: {e}", exc_info=True)

    def incident_ended(self, _incident=None) -> None:
        """Called (synchronously) when an incident ends: a check INCIDENT_DELAY_S later, unless one is waiting
        already or a check started less than INCIDENT_MIN_GAP_S before it would run. Nothing while the background
        checks are off."""
        if not self.enabled:
            return
        if self._incident_task is not None and not self._incident_task.done():
            return
        if self._last_started is not None and \
                self._clock() + INCIDENT_DELAY_S - self._last_started < INCIDENT_MIN_GAP_S:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._incident_task = loop.create_task(self._after_incident())

    async def _after_incident(self) -> None:
        await self._sleep(INCIDENT_DELAY_S)
        if not self.enabled:
            return
        if self._last_started is not None and self._clock() - self._last_started < INCIDENT_MIN_GAP_S:
            return
        try:
            await self.check("incident")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Route-change check failed: {e}", exc_info=True)

    async def close(self) -> None:
        task = self._incident_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class _NotReached(Exception):
    pass
