"""The outage-rules preview: an async job that replays the stored samples with candidate rules (design.md section 5).

`PreviewJobs` is the job store and its limits. It starts `python -m pengumon.engine.replay` (engine/replay.py) as a
subprocess and reads its JSON lines; the API is api/settings_preview.py.

The limits, all constructor arguments so that tests run them in fractions of a second:

* one replay at a time for the whole app (`PreviewBusy`, the API answers 429 with Retry-After 30);
* 60 s of wall-clock time (a week takes about 15 s), then the process is killed and the job fails with "took too long";
* a job nobody polled for 60 s is cancelled (the page closed, or moved on), its process killed;
* a finished job is kept for 10 minutes;
* two identical requests (same rules, same days, the window end rounded to the minute) share one job.

`build_spec` turns a request into the replay's input: it validates the draft values against the section models (only
`classifier.*`, `alerts.*` and `line.down_mbps` / `line.up_mbps` are accepted: nothing else changes incidents) and takes
the current path, rules and capacity from the running config.
"""

import asyncio
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.load_settings import LineConfig
from pengumon.notify.settings import AlertsConfig
from pengumon.observability import get_logger
from pengumon.observability.telemetry import preview_duration, preview_rounds, preview_runs, tracer
from pengumon.settings import registry

if TYPE_CHECKING:
    from pengumon.state import AppState

_log = get_logger("pengumon.settings")

CAP_SECONDS = 60.0
CAP_ERROR = "took too long"  # the error of a job the time limit stopped
POLL_TIMEOUT_SECONDS = 60.0
KEEP_SECONDS = 600.0
RETRY_AFTER_SECONDS = 30
DEFAULT_DAYS = 7
READ_LIMIT = 8 * 1024 * 1024  # one result line
STDERR_KEPT = 4000
REPLAY_COMMAND = (sys.executable, "-m", "pengumon.engine.replay")
LINE_KEYS = ("line.down_mbps", "line.up_mbps")
PREFIXES = ("classifier.", "alerts.")


# --- the answer's shape (GET /api/settings/preview/{id}; docs/api.md) ------------------------------------------------

class _PreviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewCounts(_PreviewModel):
    incidents: int
    critical: int
    warning: int
    busy_hidden: int
    total_seconds: float
    alerts: Optional[int] = None  # incident alerts the notifier's rules would have sent (engine/replay.py)


class PreviewWindow(_PreviewModel):
    start: float
    end: float
    rounds: int
    gaps: List[Dict[str, float]] = Field(default_factory=list)  # [{start, end}]


class PreviewChange(_PreviewModel):
    start: float
    end: Optional[float]
    kind: str
    severity: str
    in_: Literal["draft", "current", "changed"] = Field(alias="in")
    was_start: Optional[float] = None  # "changed": the current rules' start, end and severity of the same outage
    was_end: Optional[float] = None
    was_severity: Optional[str] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PreviewJob(_PreviewModel):
    """POST /api/settings/preview answers {"id": ...}; GET /api/settings/preview/{id} answers this."""
    id: Optional[str] = None
    state: Literal["running", "done", "failed", "cancelled"]
    progress: float = 0.0
    elapsed_s: float = 0.0
    error: Optional[str] = None  # "took too long"
    window: Optional[PreviewWindow] = None
    current: Optional[PreviewCounts] = None
    draft: Optional[PreviewCounts] = None
    recorded: Optional[int] = None
    changes: List[PreviewChange] = Field(default_factory=list)  # up to 30
    notes: List[str] = Field(default_factory=list)


class PreviewBusy(Exception):
    """Another replay is running."""

    retry_after = RETRY_AFTER_SECONDS


class PreviewInvalid(Exception):
    """The request names something the preview cannot replay, or a value outside its range. `errors` are FieldError
    dicts ({where, message})."""

    def __init__(self, errors: List[Dict[str, str]]):
        self.message = errors[0]["message"] if errors else "invalid preview request"  # a plain sentence for the page
        super().__init__(self.message)
        self.errors = errors


@dataclass
class Job:
    id: str
    key: str
    started: float
    state: str = "running"
    progress: float = 0.0
    finished: Optional[float] = None
    last_polled: float = 0.0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class PreviewJobs:
    def __init__(self, command: Optional[Sequence[str]] = None, cap_seconds: float = CAP_SECONDS,
                 poll_timeout: float = POLL_TIMEOUT_SECONDS, keep_seconds: float = KEEP_SECONDS, tick: float = 0.5,
                 clock: Callable[[], float] = time.monotonic):
        self.command = tuple(command) if command is not None else REPLAY_COMMAND
        self.cap_seconds = cap_seconds
        self.poll_timeout = poll_timeout
        self.keep_seconds = keep_seconds
        self.tick = tick
        self.clock = clock
        self._jobs: Dict[str, Job] = {}
        self._by_key: Dict[str, str] = {}

    # --- the store ---------------------------------------------------------------------------------------------

    def _purge(self) -> None:
        now = self.clock()
        for job in [j for j in self._jobs.values() if j.finished is not None and now - j.finished > self.keep_seconds]:
            self._drop(job)

    def _drop(self, job: Job) -> None:
        self._jobs.pop(job.id, None)
        if self._by_key.get(job.key) == job.id:
            del self._by_key[job.key]

    def running(self) -> Optional[Job]:
        return next((j for j in self._jobs.values() if j.state == "running"), None)

    def get(self, job_id: str) -> Optional[Job]:
        """The job, or None (unknown, or kept longer than 10 minutes). Asking for it is the poll that keeps it alive."""
        self._purge()
        job = self._jobs.get(job_id)
        if job is not None:
            job.last_polled = self.clock()
        return job

    def start(self, key: str, spec: Dict[str, Any]) -> Job:
        """The job for this request: the cached one for an identical request, else a new one. Raises PreviewBusy while
        another replay runs."""
        self._purge()
        known = self._jobs.get(self._by_key.get(key, ""))
        if known is not None and known.state in ("running", "done"):
            known.last_polled = self.clock()
            return known
        if self.running() is not None:
            preview_runs.add(1, {"outcome": "busy"})
            raise PreviewBusy()
        now = self.clock()
        job = Job(id=secrets.token_urlsafe(12), key=key, started=now, last_polled=now)
        self._jobs[job.id] = job
        self._by_key[key] = job.id
        job.task = asyncio.create_task(self._run(job, spec))
        return job

    async def cancel(self, job_id: str) -> Optional[Job]:
        """Stops a running job (its process is killed before this returns) and forgets a finished one. Returns the job
        as it was left, or None when it is unknown."""
        self._purge()
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.state == "running":
            await self._end(job, "cancelled")
        self._drop(job)
        return job

    async def aclose(self) -> None:
        for job in [j for j in self._jobs.values() if j.state == "running"]:
            await self._end(job, "cancelled")
        for job in list(self._jobs.values()):
            if job.task is not None:
                await asyncio.gather(job.task, return_exceptions=True)

    # --- one job -----------------------------------------------------------------------------------------------

    async def _end(self, job: Job, state: str, error: Optional[str] = None) -> None:
        """Moves a running job to its final state, then kills its process."""
        if job.state != "running":
            return
        job.state, job.error, job.finished = state, error, self.clock()
        await self._kill(job)

    @staticmethod
    async def _kill(job: Job) -> None:
        proc = job.process
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def _run(self, job: Job, spec: Dict[str, Any]) -> None:
        """One preview, as a span; its outcome, time and rounds are counted when it ends, whichever way."""
        with tracer.start_as_current_span("settings.preview") as span:
            try:
                await self._replay(job, spec)
            finally:
                self._record(job, spec, span)

    def _record(self, job: Job, spec: Dict[str, Any], span) -> None:
        if job.state == "done":
            outcome = "done"
        elif job.state == "cancelled":
            outcome = "cancelled"
        else:
            outcome = "cap" if job.error == CAP_ERROR else "failed"
        seconds = max(0.0, (job.finished if job.finished is not None else self.clock()) - job.started)
        rounds = ((job.result or {}).get("window") or {}).get("rounds") if outcome == "done" else None
        preview_runs.add(1, {"outcome": outcome})
        preview_duration.record(seconds, {"outcome": outcome})
        if isinstance(rounds, int):
            preview_rounds.record(rounds)
        span.set_attributes({"preview.outcome": outcome, "preview.days": round((spec.get("end", 0) - spec.get("start", 0)) / 86400, 1),
                             **({"preview.rounds": rounds} if isinstance(rounds, int) else {})})
        _log.info("Settings preview ended", outcome=outcome, duration_s=round(seconds, 1), rounds=rounds)

    async def _replay(self, job: Job, spec: Dict[str, Any]) -> None:
        stderr_tail = bytearray()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=READ_LIMIT)
            job.process = proc
            if job.state != "running":  # cancelled while it started
                await self._kill(job)
                return
            reader = asyncio.create_task(self._read(job, proc))
            drain = asyncio.create_task(self._drain(proc, stderr_tail))
            try:
                proc.stdin.write(json.dumps(spec).encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it ended already; its output says why
            try:
                while not reader.done() and job.state == "running":
                    await asyncio.wait({reader}, timeout=self.tick)
                    now = self.clock()
                    if job.state != "running":
                        break
                    if now - job.started > self.cap_seconds:
                        await self._end(job, "failed", CAP_ERROR)
                    elif now - job.last_polled > self.poll_timeout:
                        await self._end(job, "cancelled")
            finally:
                if job.state != "running":
                    await self._kill(job)
                reader.cancel()
                drain.cancel()
                await asyncio.gather(reader, drain, return_exceptions=True)
            if job.state != "running":
                return
            code = await proc.wait()
            job.finished = self.clock()
            if job.result is not None and code == 0:
                job.state, job.progress = "done", 1.0
            else:
                job.state = "failed"
                job.error = job.error or "The preview failed"
                _log.warning(f"settings preview: the replay ended with code {code}: "
                             f"{bytes(stderr_tail).decode(errors='replace')[-500:]}")
        except asyncio.CancelledError:
            await self._end(job, "cancelled")
            raise
        except Exception as e:
            _log.error(f"settings preview: {e}", exc_info=True)
            await self._end(job, "failed", "The preview failed")

    @staticmethod
    async def _drain(proc, tail: bytearray) -> None:
        """Keeps the last bytes of stderr (and keeps the pipe from filling up)."""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            tail.extend(chunk)
            del tail[:-STDERR_KEPT]

    @staticmethod
    async def _read(job: Job, proc) -> None:
        async for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "progress" in message:
                job.progress = max(0.0, min(1.0, float(message["progress"])))
            elif "result" in message:
                job.result = message["result"]
            elif "error" in message:
                job.error = str(message["error"])

    # --- the answer --------------------------------------------------------------------------------------------

    def describe(self, job: Job) -> PreviewJob:
        end = job.finished if job.finished is not None else self.clock()
        fields: Dict[str, Any] = {"id": job.id, "state": job.state, "progress": 1.0 if job.state == "done" else job.progress,
                                  "elapsed_s": round(max(0.0, end - job.started), 1), "error": job.error}
        if job.state == "done" and job.result is not None:
            fields.update({k: job.result.get(k) for k in ("window", "current", "draft", "recorded")})
            fields["changes"] = job.result.get("changes", [])
            fields["notes"] = job.result.get("notes", [])
        return PreviewJob.model_validate(fields)


# --- the request -----------------------------------------------------------------------------------------------

def _set(tree: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        tree = tree.setdefault(part, {})
    tree[parts[-1]] = value


def _message(error: Dict[str, Any]) -> str:
    return str(error.get("msg", "invalid")).removeprefix("Value error, ")


def draft_rules(config, values: Dict[str, Any]) -> Dict[str, Any]:
    """The draft rule set: the running config's `classifier`, `line` capacity and `alerts`, with `values` laid over it
    (null: the key's default) and validated as the sections are. Raises PreviewInvalid."""
    errors: List[Dict[str, str]] = []
    trees: Dict[str, Dict[str, Any]] = {
        "classifier": config.classifier.model_dump(mode="json"),
        "line": {"down_mbps": config.line.down_mbps, "up_mbps": config.line.up_mbps},
        "alerts": config.alerts.model_dump(mode="json"),
    }
    for key, value in values.items():
        if key not in registry.BY_KEY or not (key.startswith(PREFIXES) or key in LINE_KEYS):
            errors.append({"where": f"values.{key}", "message": "The preview only replays the outage rules, the line "
                                                                "capacity and the alert rules"})
            continue
        if value is None:
            value = registry.get(key).default()
            value = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        _set(trees, key, value)
    models = {"classifier": ClassifierConfig, "line": LineConfig, "alerts": AlertsConfig}
    for name, model in models.items():
        try:
            trees[name] = model.model_validate(trees[name]).model_dump(mode="json")
        except ValidationError as e:
            for error in e.errors():
                where = ".".join(str(p) for p in (name, *error["loc"]))
                errors.append({"where": f"values.{where}", "message": _message(error)})
    if errors:
        raise PreviewInvalid(errors)
    return {"classifier": trees["classifier"], "line": {k: trees["line"][k] for k in ("down_mbps", "up_mbps")},
            "alerts": trees["alerts"]}


def current_rules(config) -> Dict[str, Any]:
    return {"classifier": config.classifier.model_dump(mode="json"),
            "line": {"down_mbps": config.line.down_mbps, "up_mbps": config.line.up_mbps},
            "alerts": config.alerts.model_dump(mode="json")}


def build_spec(state: "AppState", values: Dict[str, Any], days: Optional[int], now: float) -> Tuple[Dict[str, Any], str]:
    """(the replay's input, the cache key) for a request. The window ends at `now` rounded down to the minute, so two
    requests in the same minute are the same request. Raises PreviewInvalid."""
    config, repository = state.config, state.repository
    retention = config.database.retention_days
    if days is None:
        days = min(DEFAULT_DAYS, retention)
    if days > retention:
        raise PreviewInvalid([{"where": "days", "message": f"Only {retention} days of samples are kept"}])
    rules = {"current": current_rules(config), "draft": draft_rules(config, values)}
    end = float(int(now // 60) * 60)
    start = end - days * 86400
    load = getattr(state.classifier, "load", None)
    devices = state.devices
    spec = {
        "db_path": str(repository.db_path), "start": start, "end": end, "round_seconds": config.prober.interval_seconds,
        "targets": [t.model_dump(mode="json") for t in config.targets],
        "dns_probes": [p.model_dump(mode="json") for p in config.dns_probes],
        "hop_status": config.prober.hop_status.model_dump(mode="json"),
        "rules": rules,
        "learned": {"down_mbps": getattr(load, "learned_down_mbps", None),
                    "up_mbps": getattr(load, "learned_up_mbps", None)},
        "wan_device_id": getattr(load, "device_id", None),
        "line_technology": devices.line_technology() if devices is not None else None,
        "timezone": config.general.timezone,
    }
    key = hashlib.sha256(json.dumps({"rules": rules, "start": start, "end": end, "learned": spec["learned"]},
                                    sort_keys=True).encode()).hexdigest()
    return spec, key
