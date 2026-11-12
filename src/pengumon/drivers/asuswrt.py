"""AsusWRT routers (RT-AX, ...): WAN byte counters, CPU, memory, the connected-client count, the client list and, on
an AiMesh network, the mesh nodes; with the client list, the uptime, the WAN port's link speed and the CPU temperature;
and the Traffic Analyzer's figures per client, hour and app (drivers/asuswrt_traffic.py).

Read-only: the router's status hooks on appGet.cgi (and update.cgi for the counters as a fallback) and a few of its
status pages (ALLOWED_REQUESTS lists every request), with the login token the Asus mobile app uses. One poll is
normally one request carrying every hook; firmware that cannot answer several hooks at once gets one request per hook,
and the combined form is tried again every hour. The status pages and the traffic statistics add at most
REQUESTS_PER_POLL short requests to a poll, and none while a speed test runs.
"""

import base64
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, ClassVar, Dict, List, Optional, Set, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.capabilities import (
    Capability, ClientsReading, DriverError, ErrorKind, PollResult, SystemReading, WanCounters,
)
from pengumon.drivers import asuswrt_traffic as stats
from pengumon.capabilities.contrib import DriverNotes
from pengumon.drivers.asuswrt_parse import (
    _NodeInfo, _cfg_nodes, _decode, _has_status_data, _json_object, _online_count, _parse_client_list, _parse_cpu,
    _parse_mem, _parse_mesh_names, _parse_mesh_nodes, _parse_netdev, _parse_update_netdev, _token_refused, build_mesh,
    node_id,
)
from pengumon.drivers.common import TEXT_MAX, CpuCounters, CpuMeter, clean_text
from pengumon.drivers.http import DeviceHttp, Verify, device_verify, new_client
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

USER_AGENT = "asusrouter-Android-DUTUtil-1.0.0.245"

# The answers are read up to these sizes (the rest never is). A combined appGet answer is mostly the client list, a few
# hundred bytes per client in the test fixtures and up to about 2 KB with every field a firmware adds: 4 MiB holds
# about 2000 such clients, several times the most a home router keeps (it also lists offline ones it remembers). A
# longer answer is that poll's problem (said once in the log), never parsed. update.cgi's netdev answer is a few KB.
APPGET_MAX_BYTES = 4 * 1024 * 1024
UPDATE_MAX_BYTES = 1024 * 1024

NETDEV_HOOKS = ["netdev()", "cpu_usage()", "memory_usage()"]
CLIENTLIST_HOOK = "get_clientlist()"
MESH_NODES_HOOK = "get_cfg_clientlist()"  # the AiMesh nodes (alias, model, MACs); changes almost never
UPTIME_HOOK = "uptime()"  # "<date>(<seconds> secs since boot)", asked with the client list

# Every request the driver may make, as (method, path): read-only status hooks and pages. `_request` refuses anything
# else, and a test checks this list. Never apply.cgi, applyapp.cgi, set_*.cgi, netool.cgi or the traffic log's clearing.
ALLOWED_REQUESTS = frozenset({
    ("POST", "login.cgi"), ("POST", "appGet.cgi"), ("GET", "update.cgi"),
    ("GET", stats.WAN_TRAFFIC_PAGE), ("GET", stats.APP_TRAFFIC_PAGE), ("GET", stats.PORTS_PAGE),
    ("GET", stats.CORE_TEMP_PAGE),
})

# After the router has shown that it cannot answer several hooks in one request, the combined form is tried
# again this often (a firmware update may have fixed it).
COMBINED_RETRY_SECONDS = 3600.0


class AsusWrtOptions(BaseModel):
    """`options:` of an asuswrt device."""
    model_config = ConfigDict(extra="forbid")

    clientlist_interval_seconds: float = Field(default=60.0, gt=0)  # the client list is the heaviest answer
    # The mesh node list (names, and whether each node is online and how it connects): asked of the router this often
    # while the client list is read; false never asks.
    mesh_node_names: bool = True
    mesh_nodes_interval_seconds: float = Field(default=60.0, gt=0)
    # Node label by MAC (or the last six hex digits of it), for nodes the router does not name or named wrongly.
    mesh_node_labels: Dict[str, str] = Field(default_factory=dict)
    # The Traffic Analyzer's figures (which clients and apps used the internet, per hour): read when the router has
    # a new hour, at most this often (the router refreshes them once an hour, so more often finds nothing new sooner
    # than a minute or so after the hour); false never reads them.
    traffic_usage: bool = True
    traffic_interval_seconds: float = Field(default=stats.DEFAULT_INTERVAL_SECONDS, ge=stats.MIN_INTERVAL_SECONDS)

    @field_validator("mesh_node_labels")
    @classmethod
    def _node_labels(cls, v: Dict[str, str]) -> Dict[str, str]:
        out = {}
        for mac, label in v.items():
            nid = node_id(str(mac))
            if nid is None:
                raise ValueError(f"mesh_node_labels: {mac!r} is not a MAC address (or its last six hex digits)")
            out[nid] = label.strip()[:TEXT_MAX]
        return out


@dataclass
class _Answers:
    """What one poll's requests brought back, before it is turned into readings."""
    problems: List[Tuple[ErrorKind, str]] = field(default_factory=list)  # why a request gave nothing, in order
    transport_error: Optional[httpx.HTTPError] = None
    fresh: bool = False  # this poll got data from the router (not only the client count kept from an earlier one)
    fresh_clients: bool = False
    wan: Optional[Tuple[int, int]] = None
    wan_read_at: Optional[float] = None
    cpu: CpuCounters = field(default_factory=dict)
    mem_pct: Optional[float] = None
    uptime: Optional[int] = None
    client_count: Optional[int] = None  # starts as the count kept from the last client-list read
    client_list: Optional[ClientsReading] = None  # get_clientlist(), parsed, when this poll got it


class AsusWrtDriver:
    name = "asuswrt"
    label = "ASUS router"  # what the device is, in the dashboard's words (devices.summary.device_label)
    offers = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM, Capability.CLIENT_INVENTORY,
                        Capability.MESH, Capability.TRAFFIC_USAGE})
    options_model = AsusWrtOptions
    # WAN rates for the load-aware classifier need recent counters; one request every 10 s is light for the router.
    default_poll_seconds = 10.0
    # The AI pack's note on this driver (capabilities/contrib.py). Each statement is established by this file:
    #  - ASUS router with ASUS firmware (AsusWRT), mesh nodes only on an AiMesh network: the module docstring, :1-2,
    #    and `label` :363; `{aimesh}` is filled in only when a mesh was seen;
    #  - counters read every {poll}: the poll interval of the device entry (`default_poll_seconds` :368 when it sets
    #    none; one request carries every hook, :4-6);
    #  - client list every {clientlist_interval}, counts in between repeat the last list: `clientlist_interval_seconds`
    #    :341 and the reuse at :518-519;
    #  - CPU is the average since the previous reading: drivers/common.py `CpuMeter`;
    #  - node names are the alias, else the model: `_parse_mesh_names` in asuswrt_parse.py;
    #  - offline clients the router remembers are not counted: devices/samples.py `client_counts` :18 (online only);
    #  - traffic per client and hour is the router's own count, hourly, the hour in progress never included, app figures
    #    only as 24-hour windows: asuswrt_traffic.py (module docstring, `TrafficRounds`);
    #  - uptime, WAN link speed and CPU temperature with the client list: UPTIME_HOOK and `_status_pages`.
    # The anonymized text leaves out the vendor, firmware and mesh-network words.
    pack_notes: ClassVar[DriverNotes] = DriverNotes(
        full=("Device notes: the router is an ASUS router with ASUS firmware (AsusWRT){aimesh}. Its counters are "
              "read every {poll}, its client list every {clientlist_interval} (counts in between repeat the last "
              "list). CPU is the average since the previous reading. Node names are the owner's alias, else the "
              "model. Clients the router still remembers but are offline are not counted. Traffic per client is the "
              "router's own hourly count, published after each hour; app figures cover 24-hour windows."),
        anonymized=("Device notes: the router reports WAN counters every {poll} and its client list every "
                    "{clientlist_interval}; CPU is the average since the previous reading. Traffic per client is the "
                    "router's own hourly count; app figures cover 24-hour windows."),
    )

    def __init__(self, url: str, username: str, password: Optional[str], *,
                 clientlist_interval_seconds: float = 60.0, mesh_node_names: bool = True,
                 mesh_nodes_interval_seconds: float = 60.0, mesh_node_labels: Optional[Dict[str, str]] = None,
                 traffic_usage: bool = True, traffic_interval_seconds: float = stats.DEFAULT_INTERVAL_SECONDS,
                 client: Optional[httpx.AsyncClient] = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 verify: Verify = True):
        self._username = username
        self._password = password
        self._verify = verify
        self._clientlist_interval = clientlist_interval_seconds
        self._mesh_node_names = mesh_node_names
        self._mesh_nodes_interval = mesh_nodes_interval_seconds
        self._mesh_node_labels = dict(mesh_node_labels or {})  # node id -> label, from the options
        self._mesh_names: Dict[str, str] = {}  # node id -> label, as the router names them
        self._mesh_infos: List[_NodeInfo] = []  # the router's node list: online, backhaul
        self._mesh_info_by_id: Dict[str, _NodeInfo] = {}
        self._mesh_names_read_at: Optional[float] = None
        # Why the client list or the mesh is missing, from the last poll that read the client list; repeated on the polls
        # in between, so that the device logs the gap once instead of once per client-list read.
        self._clients_missing: Dict[Capability, str] = {}
        self._clock = clock  # paces the client-list reads, the combined-request retry and CPU %; injectable
        self._wall_clock = wall_clock  # when the counters were read (WanCounters.read_at)
        self._token: Optional[str] = None
        self._session_cookies: Dict[str, str] = {}
        self.http = DeviceHttp(url, self._new_client, driver=self.name, client=client)
        self._log = get_logger("pengumon.drivers.asuswrt")
        # Several hooks in one appGet request: assumed to work until the router answers one with a 200 JSON
        # object that has no netdev data; tried again every COMBINED_RETRY_SECONDS after that.
        self._combined_ok = True
        self._combined_retry_at = 0.0
        self._client_count: Optional[int] = None
        self._clients_read_at: Optional[float] = None
        self._cpu = CpuMeter(clock)
        # The router's statistics pages, read at the client list's pace (link speed, temperature) or in rounds (traffic)
        self._pages_unsupported: Dict[str, float] = {}  # page -> monotonic time to ask for it again (it answered 404)
        self._pages_too_big: Set[str] = set()  # pages that answered longer than stats.PAGE_MAX_BYTES (logged once)
        self._appget_too_big = False  # an appGet answer was longer than APPGET_MAX_BYTES (logged once)
        self._traffic = (stats.TrafficRounds(self._page_get, traffic_interval_seconds, wall_clock=wall_clock,
                                             log=self._log) if traffic_usage else None)
        self._traffic_reason: Optional[str] = None  # why there is no traffic, as last said in `missing`
        if not traffic_usage:
            self.offers = self.offers - {Capability.TRAFFIC_USAGE}
        # True while the reads that can wait must wait: a speed test is running (DeviceRegistry sets it). A read of the
        # WAN counters for a speed test (read_wan_counters) also holds them for SPEED_TEST_QUIET_SECONDS.
        self.pause_reads: Callable[[], bool] = lambda: False
        self._counters_read_at: Optional[float] = None

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None, **clocks: Callable[[], float]) -> "AsusWrtDriver":
        """`clocks` (clock, wall_clock) are for tests."""
        options = cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, credentials.username, credentials.password,
                   clientlist_interval_seconds=options.clientlist_interval_seconds,
                   mesh_node_names=options.mesh_node_names,
                   mesh_nodes_interval_seconds=options.mesh_nodes_interval_seconds,
                   mesh_node_labels=options.mesh_node_labels, traffic_usage=options.traffic_usage,
                   traffic_interval_seconds=options.traffic_interval_seconds, client=client,
                   verify=device_verify(cfg), **clocks)

    def _new_client(self) -> httpx.AsyncClient:
        client = new_client(self._verify, headers={"User-Agent": USER_AGENT})
        if self._session_cookies:
            client.cookies.update(self._session_cookies)
        return client

    @property
    def host(self) -> str:
        return self.http.host

    @property
    def traffic_interval_seconds(self) -> Optional[float]:
        """How often the Traffic Analyzer is read at most (None: switched off in the options)."""
        return self._traffic.interval if self._traffic is not None else None

    def missing_configuration(self) -> Optional[str]:
        return None if self._password else "no password"

    def set_credentials(self, username: str, password: str) -> None:
        self._username, self._password = username, password
        self._token = None  # the next poll logs in with the new credentials

    async def aclose(self) -> None:
        await self.http.aclose()

    @staticmethod
    def _allow(method: str, path: str) -> None:
        if (method, path) not in ALLOWED_REQUESTS:
            raise DriverError("internal", f"refused to send {method} {path}: not one of the driver's read-only requests")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Every request the driver makes goes through here or `_capped`: one of ALLOWED_REQUESTS, or none at all."""
        self._allow(method, path)
        return await self.http.request(method, path, **kwargs)

    async def _capped(self, method: str, path: str, limit: int, **kwargs) -> stats.Page:
        """One of ALLOWED_REQUESTS with its answer read up to `limit` bytes only (the rest never is): `truncated` says
        it was longer. The status pages, the traffic pages, appGet.cgi and update.cgi are read this way."""
        self._allow(method, path)
        status, headers, body, truncated = await self.http.request_capped(method, path, limit, **kwargs)
        return stats.Page(status, _decode(headers, body), truncated)

    async def _page_get(self, page: str, params: Optional[dict] = None) -> stats.Page:
        """GET of one of the router's .asp pages on the session, as its own web page would ask for it, read up to
        stats.PAGE_MAX_BYTES."""
        return await self._capped("GET", page, stats.PAGE_MAX_BYTES, params=params,
                                  timeout=stats.PAGE_TIMEOUT_SECONDS,
                                  headers={"User-Agent": USER_AGENT, "Referer": f"{self.http.base_url}/index.asp"})

    async def _login(self) -> None:
        """Logs in and keeps the token. Raises DriverError when refused; transport errors propagate."""
        raw = f"{self._username}:{self._password}".encode("utf-8")
        res = await self._request(
            "POST", "login.cgi",
            data={"login_authorization": base64.b64encode(raw).decode("ascii")},
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        )
        data: dict = {}
        if res.status_code == 200 and res.headers.get("content-type", "").startswith("application/json"):
            data = _json_object(res.text) or {}
        token = (data.get("asus_token") or res.cookies.get("asus_token")) if res.status_code == 200 else None
        if token:
            self._token = token
            self._session_cookies["asus_token"] = token
            for k, v in res.cookies.items():
                self._session_cookies[k] = v
            return
        error_status = data.get("error_status")
        if error_status:
            raise DriverError("auth", f"Authentication failed on Asus router ({self.host}): error_status "
                                      f"{clean_text(str(error_status), 20)}. Check credentials.")
        raise DriverError("auth" if res.status_code in (401, 403) else "protocol",
                          f"Asus router login at {self.host} answered HTTP {res.status_code} without a token")

    async def _hook(self, hooks: List[str], problems: List[Tuple[ErrorKind, str]]) -> Optional[str]:
        """POSTs the hooks to appGet.cgi (joined with ';') and returns the body of a 200 response. Logs in again
        once if the token was refused (401/403, or an error_status body even with status 200). None if it still
        is, and for any other status (a 502 page from a struggling router is a failed poll, not an answer); the
        reason goes to `problems`. Transport errors and a refused login propagate."""
        for attempt in (1, 2):
            res = await self._capped(
                "POST", "appGet.cgi", APPGET_MAX_BYTES,
                data={"hook": ";".join(hooks)},
                headers={"User-Agent": USER_AGENT, "asus_token": self._token or ""},
            )
            if res.status_code == 200 and res.truncated:  # not an answer this driver reads: none of it is used
                reason = f"appGet answered with more than {APPGET_MAX_BYTES // (1024 * 1024)} MiB: not read"
                if not self._appget_too_big:
                    self._log.warning(f"The router {reason} (said once)")
                    self._appget_too_big = True
                problems.append(("protocol", reason))
                return None
            if res.status_code == 200 and not _token_refused(res.text):
                return res.text
            if res.status_code not in (200, 401, 403):
                problems.append(("protocol", f"appGet answered HTTP {res.status_code}"))
                return None
            if attempt == 1:
                try:
                    await self._login()
                except DriverError as err:  # keep what earlier requests of this poll got
                    problems.append((err.kind, err.message))
                    return None
        problems.append(("auth", "the router refused the session token again right after a fresh login"))
        return None

    async def poll(self) -> PollResult:
        if not self._token:
            await self._login()

        # The client list changes slowly and is the heaviest answer: read it every clientlist_interval_seconds
        # and reuse the last count in between.
        clients_due = (
            self._clients_read_at is None
            or self._clock() - self._clients_read_at >= self._clientlist_interval
        )
        hooks = NETDEV_HOOKS + ([CLIENTLIST_HOOK, UPTIME_HOOK] if clients_due else [])
        if clients_due:
            self._clients_read_at = self._clock()
        got = await self._ask_router(hooks, clients_due)
        return await self._readings_of(got, clients_due)

    async def _ask_router(self, hooks: List[str], clients_due: bool) -> "_Answers":
        """The requests of one poll: the hooks (in one request, else one each), then update.cgi for the counters if
        they are still missing. Raises when nothing at all was read (the first problem found, else the transport
        error, else a protocol error)."""
        got = _Answers(client_count=self._client_count)
        text = await self._read_combined(hooks, clients_due, got)
        if text is None and not self._combined_ok:
            await self._read_each_hook(hooks, got)
        if clients_due:
            self._client_count = got.client_count
        if got.wan is None:  # fallback for the counters: the realtime traffic page's data source
            await self._read_update_cgi(got)
        if not got.fresh:
            if got.problems:
                raise DriverError(*got.problems[0])
            if got.transport_error is not None:
                raise got.transport_error
            raise DriverError("protocol", "the router answered without WAN counters, CPU, memory or clients")
        return got

    async def _read_combined(self, hooks: List[str], clients_due: bool, got: "_Answers") -> Optional[str]:
        """All the hooks in one appGet request (not while the router is known to refuse that, except for the hourly
        retry). Returns the answer's text when it carries data, else None; what it holds goes to `got`."""
        if not (self._combined_ok or self._clock() >= self._combined_retry_at):
            return None
        if not self._combined_ok:  # the hourly retry; whatever happens, the next one is an hour away
            self._combined_retry_at = self._clock() + COMBINED_RETRY_SECONDS
        try:
            text = await self._hook(hooks, got.problems)
        except httpx.HTTPError as ex:
            got.transport_error = ex
            return None
        counters = _parse_netdev(text) if text is not None else None
        if text is not None and counters is None:
            text = self._judge_answer_without_counters(text, got)
        elif text is not None and not self._combined_ok:
            self._log.info("Router answers combined appGet requests again")
            self._combined_ok = True
        if text is None:
            return None
        got.fresh = True
        got.wan, got.wan_read_at = counters, self._wall_clock()
        got.cpu = _parse_cpu(text)
        got.mem_pct = _parse_mem(text)
        if clients_due:
            got.client_list = _parse_client_list(text)
            got.client_count = _online_count(got.client_list)
            got.fresh_clients = got.client_count is not None
            got.uptime = stats.uptime_seconds(text)
        return text

    def _judge_answer_without_counters(self, text: str, got: "_Answers") -> Optional[str]:
        """A combined answer that has no WAN counters: `text` when it still carries data, else None (the reason goes to
        `got.problems` when it was not JSON). A JSON answer without netdev data means the router does not take
        several hooks at once, which is remembered."""
        body = _json_object(text)
        if body is not None and "netdev" not in body:
            # A real answer that lacks the counters: the router does not take several hooks at once.
            # (A non-200 status, or a 200 that is not JSON, says nothing about that: failed poll.)
            log = self._log.warning if self._combined_ok else self._log.debug
            log("Router returned no WAN counters for a combined appGet request; "
                "using one request per hook (combined requests are retried every "
                f"{COMBINED_RETRY_SECONDS / 3600:g} h)")
            self._combined_ok = False
            self._combined_retry_at = self._clock() + COMBINED_RETRY_SECONDS
        if body is None and not _has_status_data(text):
            got.problems.append(("protocol", "appGet answer was not JSON"))
            return None
        if body is not None and "netdev" not in body:
            return None
        return text

    async def _read_each_hook(self, hooks: List[str], got: "_Answers") -> None:
        """One request per hook (firmware that rejects several hooks in one request)."""
        for hook in hooks:
            try:
                part = await self._hook([hook], got.problems)
            except httpx.HTTPError as ex:
                got.transport_error = ex
                continue
            if part is None:
                continue
            got.fresh = True
            if hook == "netdev()":
                parsed = _parse_netdev(part)
                if parsed:
                    got.wan, got.wan_read_at = parsed, self._wall_clock()
            elif hook == "cpu_usage()":
                got.cpu = _parse_cpu(part)
            elif hook == "memory_usage()":
                got.mem_pct = _parse_mem(part)
            elif hook == UPTIME_HOOK:
                got.uptime = stats.uptime_seconds(part)
            else:
                got.client_list = _parse_client_list(part)
                got.client_count = _online_count(got.client_list)
                got.fresh_clients = got.client_count is not None

    async def _read_update_cgi(self, got: "_Answers") -> None:
        """The WAN counters from update.cgi?output=netdev, when the hooks gave none."""
        try:
            res = await self._capped(
                "GET", "update.cgi", UPDATE_MAX_BYTES, params={"output": "netdev"},
                headers={"User-Agent": USER_AGENT,
                         "Referer": f"{self.http.base_url}/Main_TrafficMonitor_realtime.htm"},
            )
        except httpx.HTTPError as ex:
            got.transport_error = ex
            return
        found = (_parse_update_netdev(res.text)
                 if res.status_code == 200 and not res.truncated else None)
        if found:
            got.wan, got.wan_read_at = found, self._wall_clock()
            got.fresh = True

    async def _readings_of(self, got: "_Answers", clients_due: bool) -> PollResult:
        """What the poll's answers make: the capabilities' readings, and why the others are missing. The status pages
        and the Traffic Analyzer round add their requests here, within the poll's request budget."""
        cpu_pct = self._cpu.update(got.cpu)

        readings: dict = {}
        missing: dict = {}
        if clients_due:
            self._clients_missing = {}
            if got.fresh_clients and got.client_list is not None:
                await self._client_readings(got.client_list, readings)
            else:
                reason = "the router's answer had no client list"
                self._clients_missing = {Capability.CLIENT_INVENTORY: reason, Capability.MESH: reason}
        missing.update({c: r for c, r in self._clients_missing.items() if c not in readings})
        if got.wan is not None:
            readings[Capability.WAN_COUNTERS] = WanCounters(rx_bytes=got.wan[0], tx_bytes=got.wan[1],
                                                            read_at=got.wan_read_at)
        else:
            missing[Capability.WAN_COUNTERS] = (got.problems[0][1] if got.problems
                                                else "the router's answer had no WAN counters")
        budget = stats.REQUESTS_PER_POLL
        link = temp = None
        if clients_due:  # the status pages, at the client list's pace
            link, temp, made = await self._status_pages(budget)
            budget -= made
        if not got.cpu and got.mem_pct is None and not got.fresh_clients:
            missing[Capability.ROUTER_SYSTEM] = "the router's answer had no CPU, memory or client data"
        else:
            readings[Capability.ROUTER_SYSTEM] = SystemReading(cpu_pct=cpu_pct, mem_pct=got.mem_pct,
                                                               client_count=got.client_count, uptime_s=got.uptime,
                                                               cpu_temp_c=temp, wan_link_mbps=link)
        await self._traffic_step(budget, readings, missing)
        return PollResult(readings=readings, missing=missing)

    def _paused(self) -> bool:
        """Whether the reads that can wait (the statistics pages) must wait: during a speed test."""
        read_at = self._counters_read_at
        if read_at is not None and self._clock() - read_at < stats.SPEED_TEST_QUIET_SECONDS:
            return True
        try:
            return bool(self.pause_reads())
        except Exception:  # a broken hook must not cost the poll
            return False

    async def _status_page(self, page: str) -> Optional[str]:
        """The body of one status page, or None: not offered by this router (404: asked again a day later, logged
        once), any other answer, or a transport error (the next client-list poll asks again)."""
        if self._clock() < self._pages_unsupported.get(page, 0.0):
            return None
        try:
            res = await self._page_get(page)
        except httpx.HTTPError as ex:
            self._log.debug(f"{page} not read: {type(ex).__name__}")
            return None
        if res.status_code == 404:
            if page not in self._pages_unsupported:
                self._log.info(f"The router does not offer {page}; asking again in a day")
            self._pages_unsupported[page] = self._clock() + stats.UNSUPPORTED_RECHECK_SECONDS
            return None
        if res.status_code == 200 and res.truncated:  # not the page's size: none of it is used
            if page not in self._pages_too_big:
                self._log.info(f"The router answered {page} with more than {stats.PAGE_MAX_BYTES} bytes; not read")
                self._pages_too_big.add(page)
            return None
        return res.text if res.status_code == 200 else None

    async def _status_pages(self, budget: int) -> Tuple[Optional[int], Optional[float], int]:
        """(WAN link Mb/s, CPU °C, requests made) from the two status pages; nothing during a speed test."""
        if self._paused():
            return None, None, 0
        made = 0
        link = temp = None
        for page in (stats.PORTS_PAGE, stats.CORE_TEMP_PAGE):
            if made >= budget or self._clock() < self._pages_unsupported.get(page, 0.0):
                continue
            made += 1
            body = await self._status_page(page)
            if body is None:
                continue
            if page == stats.PORTS_PAGE:
                link = stats.wan_link_mbps(body)
            else:
                temp = stats.cpu_temp_c(body)
        return link, temp, made

    async def _traffic_step(self, budget: int, readings: dict, missing: dict) -> None:
        """A few requests of the Traffic Analyzer round under way (asuswrt_traffic.TrafficRounds). Why there is no
        traffic goes to `missing` once, when it is found out (or changes), not at every poll."""
        if self._traffic is None or self._paused():
            return
        reading, reason, _ = await self._traffic.step(budget)
        if reading is not None:
            readings[Capability.TRAFFIC_USAGE] = reading
            self._traffic_reason = None
        if reason is not None and reason != self._traffic_reason:
            missing[Capability.TRAFFIC_USAGE] = reason
            self._traffic_reason = reason

    async def read_wan_counters(self) -> Optional[WanCounters]:
        """The WAN counters alone, for a running speed test (Device.read_wan_counters, about once a second): one
        appGet request with the netdev() hook, on the session the polls use (the router allows one admin session: a
        token it refused gets the same one re-login a poll would do). The counters are the router's at the moment it
        answers (the hook reads the interface statistics when it is asked; nothing here keeps them). None when the
        answer has none; a refused login raises, like a poll's."""
        if not self._token:
            await self._login()
        self._counters_read_at = self._clock()  # a speed test is running: the statistics pages wait
        problems: List[Tuple[ErrorKind, str]] = []
        text = await self._hook(["netdev()"], problems)
        counters = _parse_netdev(text) if text is not None else None
        if counters is None:
            if problems:
                raise DriverError(*problems[0])
            return None
        return WanCounters(rx_bytes=counters[0], tx_bytes=counters[1], read_at=self._wall_clock())

    async def _client_readings(self, clients: ClientsReading, readings: dict) -> None:
        """The client list (parsed from the answer that carried get_clientlist()) and the mesh: no further request
        except the node names, asked at most every mesh_nodes_interval_seconds."""
        self._clients_missing = {}
        readings[Capability.CLIENT_INVENTORY] = clients
        if self._mesh_node_names and any(c.node for c in clients.clients):
            await self._read_mesh_names()
        mesh = build_mesh(clients, self._mesh_names, self._mesh_node_labels, self._mesh_infos, self._mesh_info_by_id)
        if mesh is not None:  # a single router has no mesh: no reading, and not a fault either
            readings[Capability.MESH] = mesh

    async def _read_mesh_names(self) -> None:
        """Best effort and rare: a failure keeps the names already known and is retried at the next due time."""
        now = self._clock()
        if self._mesh_names_read_at is not None and now - self._mesh_names_read_at < self._mesh_nodes_interval:
            return
        self._mesh_names_read_at = now
        problems: List[Tuple[ErrorKind, str]] = []
        try:
            text = await self._hook([MESH_NODES_HOOK], problems)
        except (httpx.HTTPError, DriverError) as ex:
            self._log.debug(f"Mesh node names not read: {type(ex).__name__}")
            self._mesh_infos, self._mesh_info_by_id = [], {}  # an online flag that is not current is worse than none
            return
        nodes = _cfg_nodes(text) if text is not None else None  # (parsed once for both)
        names = _parse_mesh_names("", nodes) if nodes is not None else {}
        self._mesh_infos, self._mesh_info_by_id = _parse_mesh_nodes("", nodes) if nodes is not None else ([], {})
        if names:
            self._mesh_names = names
        else:
            self._log.debug("The router gave no mesh node names; nodes are numbered")

