"""HTTP health checks of the other apps on this host (any local web service with a health URL).

Each configured service is fetched every `interval_seconds`; `failures_for_incident` failed checks in a row open a
`service_down` incident, the next good answer resolves it. This replaces the containers' own podman healthchecks,
which needed systemd timers and whose status nothing read. Runs in its own task (services/component.py),
never in the probe loop, and keeps its incidents apart from the network classifier's.

Beyond the incident each service has a live status (`up`, `degraded` after 1 or 2 failures, `down`, `unknown` before
the first check), the latency of the last answer and a classified error, which /api/services shows. A push message
is a separate matter: it goes out only once the service has failed for `notify_after_seconds`, so a restart during
a deploy stays quiet (see `on_service_down` / `on_service_up`).
"""

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional
from urllib.parse import urlsplit

import httpx

from pengumon.services.settings import ServiceCheckConfig, ServiceChecksConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.taxonomy import Kind, register_kind
from pengumon.incidents.model import Incident
from pengumon.observability.telemetry import service_check_duration, service_up

logger = logging.getLogger("pengumon.services")

SERVICE_CAUSE = "service_unavailable"
SERVICE_KIND = "service_down"
register_kind(Kind(SERVICE_KIND, "service", scope="service"))  # an app on this host, not the network

ErrorKind = str  # timeout, refused, tls, dns, unreachable, http, config


@dataclass(frozen=True)
class ServiceError:
    kind: ErrorKind
    message: str  # for people: what is wrong with the service

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message}


def _ssl_cause(exc: BaseException) -> Optional[ssl.SSLError]:
    """The ssl error behind a transport error (httpx wraps it: ConnectError -> __cause__ ... -> SSLCertVerificationError)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def _chain_has(exc: BaseException, types) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, types):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _tls_message(err: ssl.SSLError) -> str:
    reason = (getattr(err, "verify_message", None) or getattr(err, "reason", None) or str(err) or "").lower()
    if "expired" in reason:
        return "certificate expired"
    if getattr(err, "verify_code", None) is not None or "certificate" in reason or "verify" in reason:
        return "certificate not trusted"
    return f"TLS handshake failed: {err.reason or str(err) or type(err).__name__}"


def classify_error(exc: BaseException) -> ServiceError:
    """What a failed check means, in words. An exception with an empty message (httpx's ConnectError often has one)
    is described by its type, like devices.device.classify."""
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        return ServiceError("timeout", f"no answer in time ({type(exc).__name__})")
    tls = _ssl_cause(exc)
    if tls is not None:
        return ServiceError("tls", _tls_message(tls))
    if isinstance(exc, httpx.TransportError):
        if _chain_has(exc, ConnectionRefusedError):
            return ServiceError("refused", "connection refused (stopped or restarting)")
        if _chain_has(exc, socket.gaierror):
            return ServiceError("dns", f"name not found ({detail})")
        return ServiceError("unreachable", detail)
    return ServiceError("unreachable", f"{type(exc).__name__}: {detail}" if str(exc) else detail)


@dataclass
class ServiceStatus:
    ok: Optional[bool] = None  # None until the first check
    last_checked: Optional[float] = None
    last_ok: Optional[float] = None  # when the service last answered
    since: Optional[float] = None  # start of the current run: answering (ok) or failing
    last_status_code: Optional[int] = None
    latency_ms: Optional[float] = None  # of the last check that got an answer (HTTP error answers count)
    error: Optional[ServiceError] = None
    consecutive_failures: int = 0
    incident_id: Optional[int] = None
    announced: bool = False  # on_service_down went out for the current outage


def _client_for(service: ServiceCheckConfig) -> httpx.AsyncClient:
    """Verifies TLS against the service's own CA file when it has one, else the system store."""
    verify = ssl.create_default_context(cafile=service.ca_file) if service.ca_file else True
    return httpx.AsyncClient(timeout=service.timeout_seconds, verify=verify, follow_redirects=False)


def display_url(url: str) -> str:
    """The check URL as text for people: host:port/path, without scheme, credentials or query."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}{parts.path}"


class ServiceChecker:
    def __init__(
        self,
        config: ServiceChecksConfig,
        repository: DatabaseRepository,
        on_incident_started: Optional[Callable[[Incident], None]] = None,
        on_incident_resolved: Optional[Callable[[Incident], None]] = None,
        client_factory: Callable[[ServiceCheckConfig], httpx.AsyncClient] = _client_for,
        clock: Callable[[], float] = time.time,
        on_service_down: Optional[Callable[[ServiceCheckConfig, ServiceError, float], None]] = None,
        on_service_up: Optional[Callable[[ServiceCheckConfig, float], None]] = None,
        timer: Callable[[], float] = time.perf_counter,
    ):
        """`on_service_down(service, error, seconds_down)` fires once per outage, when a service with notify=True
        has failed for notify_after_seconds; `on_service_up(service, seconds_down)` fires on recovery only if that
        happened. The incident callbacks fire when the incident opens and closes (not tied to notifications)."""
        self.config = config
        self.repository = repository
        self.on_incident_started = on_incident_started
        self.on_incident_resolved = on_incident_resolved
        self.on_service_down = on_service_down
        self.on_service_up = on_service_up
        self.clock = clock
        self._timer = timer
        self._client_factory = client_factory
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self.status: Dict[str, ServiceStatus] = {}

    @property
    def services(self):
        return [s for s in self.config.services if s.enabled]

    def state_of(self, service_id: str) -> str:
        """up, degraded (failing, below the incident threshold), down or unknown (not checked yet)."""
        st = self.status.get(service_id)
        if st is None or st.ok is None:
            return "unknown"
        if st.ok:
            return "up"
        return "down" if st.consecutive_failures >= self.config.failures_for_incident else "degraded"

    async def poll(self, force: bool = False) -> None:
        """Checks every enabled service once, side by side (signature shared with the other collectors)."""
        await asyncio.gather(*(self._check_and_track(s) for s in self.services))

    async def _check(self, service: ServiceCheckConfig) -> tuple:
        """(error, status code, latency in ms): error is None when the service answered 2xx. Latency is None
        when nothing answered."""
        client = self._clients.get(service.id)
        if client is None:
            try:
                client = self._clients[service.id] = self._client_factory(service)
            except Exception as e:  # e.g. a missing or unreadable ca_file, retried at the next check
                return ServiceError("config", f"client setup failed (ca_file {service.ca_file}): "
                                              f"{type(e).__name__}: {e}"), None, None
        started = self._timer()
        try:
            response = await client.get(service.url)
        except Exception as e:
            return classify_error(e), None, None
        latency_ms = (self._timer() - started) * 1000.0
        if not response.is_success:
            return ServiceError("http", f"HTTP {response.status_code}"), response.status_code, latency_ms
        return None, response.status_code, latency_ms

    async def _check_and_track(self, service: ServiceCheckConfig) -> None:
        error, code, latency_ms = await self._check(service)
        st = self.status.setdefault(service.id, ServiceStatus())
        now = self.clock()
        ok = error is None
        previous_since = st.since
        if st.ok is None or st.ok != ok:
            st.since = now
        st.last_checked, st.error, st.ok, st.last_status_code, st.latency_ms = now, error, ok, code, latency_ms
        service_up.set(1 if ok else 0, {"service": service.id})
        if latency_ms is not None:
            service_check_duration.record(latency_ms / 1000.0, {"service": service.id})
        if ok:
            st.last_ok = now
            st.consecutive_failures = 0
            was_announced, st.announced = st.announced, False
            if st.incident_id is not None:
                await self._resolve(st, now)
            if was_announced and self.on_service_up:
                self.on_service_up(service, now - (previous_since or now))
            return
        st.consecutive_failures += 1
        if st.incident_id is None and st.consecutive_failures >= self.config.failures_for_incident:
            await self._open(service, st, now, error)
        down_for = now - (st.since or now)  # st.since: first failure of this run
        if service.notify and not st.announced and down_for >= self.config.notify_after_seconds:
            st.announced = True
            if self.on_service_down:
                self.on_service_down(service, error, down_for)

    async def _open(self, service: ServiceCheckConfig, st: ServiceStatus, now: float, error: ServiceError) -> None:
        failed_for = st.consecutive_failures * self.config.interval_seconds
        incident = Incident(
            start_time=now - failed_for,
            cause_code=SERVICE_CAUSE,
            kind=SERVICE_KIND,
            severity="critical",
            title=f"{service.name} is not responding",
            description=(f"{display_url(service.url)} failed {st.consecutive_failures} health checks in a row "
                         f"(about {failed_for:.0f} s): {error.message}"),
            affected_targets=[service.id],
        )
        incident.id = st.incident_id = await self.repository.create_incident(incident)
        logger.warning(f"{incident.title}: {error.message}")
        if self.on_incident_started:
            self.on_incident_started(incident)

    async def _resolve(self, st: ServiceStatus, now: float) -> None:
        """Closes the open incident."""
        incident_id, st.incident_id = st.incident_id, None
        await self.repository.resolve_incident(incident_id, now)
        incident = await self.repository.get_incident_by_id(incident_id)
        if incident:
            logger.info(f"Resolved: {incident.title}")
            if self.on_incident_resolved:
                self.on_incident_resolved(incident)

    async def resolve_open(self) -> None:
        """Closes this checker's open incidents (on shutdown: nothing watches them after that)."""
        now = self.clock()
        for st in self.status.values():
            if st.incident_id is not None:
                await self.repository.resolve_incident(st.incident_id, now)
                st.incident_id = None

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()
