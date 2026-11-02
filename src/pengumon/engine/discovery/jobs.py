"""The discovery runs behind the API: one at a time, results kept for a while, memory bounded.

`start` begins a run, or returns the id of the one already running (single flight: a second click, another browser,
the first-run view and a "Rediscover" all share it). A finished run is kept KEEP_SECONDS for its page to read, and
at most MAX_KEPT of them at all (the oldest go first).
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from pengumon.engine.discovery.engine import DiscoveryOptions, DiscoveryResult

logger = logging.getLogger("pengumon.discovery")

KEEP_SECONDS = 600.0
MAX_KEPT = 8

Runner = Callable[[DiscoveryOptions, Callable[[str], None]], Awaitable[DiscoveryResult]]


@dataclass
class Job:
    id: str
    options: DiscoveryOptions
    started: float
    state: str = "running"  # running | done | failed
    progress: str = "Starting"
    hops: List[dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    finished: Optional[float] = None
    task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {"id": self.id, "state": self.state, "progress": self.progress, "draft": {"hops": self.hops},
                "notes": self.notes,
                "options": {"asn_lookup": self.options.asn_lookup, "device_checks": self.options.device_checks,
                            "snmp": self.options.snmp}}


class DiscoveryJobs:
    def __init__(self, runner: Runner, clock: Callable[[], float] = time.monotonic):
        self._runner = runner
        self._clock = clock
        self._jobs: Dict[str, Job] = {}
        self._running: Optional[Job] = None
        self._auto_id: Optional[str] = None  # the run the first-run dashboard started, once per server process

    def start(self, options: DiscoveryOptions) -> str:
        """The id of the new run, or of the one already running."""
        self._prune()
        if self._running is not None and self._running.state == "running":
            return self._running.id
        job = Job(id=secrets.token_hex(8), options=options, started=self._clock())
        self._jobs[job.id] = job
        self._running = job
        job.task = asyncio.create_task(self._run(job))
        self._prune()
        return job.id

    def start_first_run(self, options: DiscoveryOptions) -> Optional[str]:
        """The first-run dashboard's run: started once per server process. Later calls (every dashboard load while
        no path is applied) return that run's id, or None once it is no longer kept; they never start another."""
        self._prune()
        if self._auto_id is None:
            self._auto_id = self.start(options)
            return self._auto_id
        return self._auto_id if self._auto_id in self._jobs else None

    def get(self, job_id: str) -> Optional[dict]:
        self._prune()
        job = self._jobs.get(job_id)
        return job.public() if job is not None else None

    async def _run(self, job: Job) -> None:
        def progress(text: str) -> None:
            job.progress = text

        try:
            result = await self._runner(job.options, progress)
            job.hops, job.notes, job.state, job.progress = result.hops, result.notes, "done", "Done"
        except asyncio.CancelledError:
            job.state, job.progress = "failed", "Stopped (the server is shutting down)"
            raise
        except Exception as e:
            logger.error(f"Discovery failed: {e}", exc_info=True)
            job.state, job.progress = "failed", f"Discovery failed: {type(e).__name__}"
        finally:
            job.finished = self._clock()
            job.task = None

    def _prune(self) -> None:
        now = self._clock()
        finished = sorted((j for j in self._jobs.values() if j.state != "running"), key=lambda j: j.finished or 0)
        for job in finished:
            if now - (job.finished or now) > KEEP_SECONDS:
                del self._jobs[job.id]
        finished = [j for j in finished if j.id in self._jobs]
        while len(self._jobs) > MAX_KEPT and finished:
            del self._jobs[finished.pop(0).id]

    async def close(self) -> None:
        """Stops a running discovery (at shutdown)."""
        job = self._running
        task = job.task if job is not None else None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
