"""OpenWrt routers: WAN byte counters, CPU, memory, the connected-client count and the client list, through ubus's
JSON-RPC over HTTP (`/ubus` on uhttpd, with uhttpd-mod-ubus and rpcd, which LuCI installs).

Read-only: `session login`, then only methods that report state (READ_METHODS; nothing that changes the router is
ever called). One poll is one HTTP request carrying a batch of calls, and a second one for the Wi-Fi stations when
the client list is due. A session the router no longer knows (rpcd restarted, or it expired) is replaced by one
fresh login per poll. What each call needs in the user's rpcd ACL is in docs/devices.md; a call the ACL refuses
leaves out the readings that need it and says so, the others still come.
"""

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pengumon.capabilities import (
    BAND_2G, BAND_5G, BAND_6G, BAND_OTHER, Capability, Client, ClientsReading, DriverError, PollResult, SystemReading,
    WanCounters,
)
from pengumon.drivers.common import MAX_CLIENTS, CpuMeter, clean_text, normal_mac, proc_stat_cpu
from pengumon.drivers.http import DeviceHttp, Verify, device_verify, new_client
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

ENDPOINT = "ubus"
NO_SESSION = "0" * 32  # the session id ubus takes for `session login`
# Every (object, method) the driver calls after logging in. All of them only report state.
READ_METHODS = frozenset({
    ("network.interface", "dump"), ("luci-rpc", "getNetworkDevices"), ("system", "info"), ("system", "board"),
    ("file", "read"), ("luci-rpc", "getDHCPLeases"), ("luci-rpc", "getWirelessDevices"), ("iwinfo", "assoclist"),
})
PROC_STAT = "/proc/stat"
# /proc/net is a link to /proc/self/net, and rpcd checks the ACL against the resolved path: the ACL entry that allows
# this is "/proc/*/net/arp".
ARP_TABLE = "/proc/self/net/arp"
BOARD_INTERVAL_SECONDS = 3600.0  # the model (system board) is read at the first poll and then this often
MAX_STATIONS_INTERFACES = 16  # Wi-Fi interfaces asked for their stations in one poll

# ubus status codes (the first element of a call's "result") and uhttpd's JSON-RPC error codes.
UBUS_OK = 0
UBUS_STATUS = {1: "invalid command", 2: "invalid argument", 3: "method not found", 4: "not found", 5: "no data",
               6: "permission denied", 7: "timeout", 8: "not supported", 9: "unknown error", 10: "connection failed"}
UBUS_PERMISSION_DENIED = 6
RPC_ACCESS_DENIED = -32002  # also what an unknown or expired session gets
RPC_OBJECT_NOT_FOUND = -32000


class OpenWrtOptions(BaseModel):
    """`options:` of an openwrt device."""
    model_config = ConfigDict(extra="forbid")

    # The logical interface whose device carries the internet traffic (network.interface.<name>, "wan" by default;
    # "wan6" or a PPPoE "wan" both work: its layer-3 device is looked up each poll).
    wan_interface: str = Field(default="wan", pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    clientlist_interval_seconds: float = Field(default=60.0, gt=0)  # DHCP leases and Wi-Fi stations


@dataclass(frozen=True)
class Answer:
    """One call's outcome: `data` (the result object, {} when the call returned none) when it worked, else `problem`
    (for people) and `denied` (refused: by the ACL, or, for `session_refused`, because ubus does not know the session,
    which looks the same as an ACL that lacks the object)."""

    data: Optional[dict] = None
    problem: Optional[str] = None
    denied: bool = False
    session_refused: bool = False

    @property
    def ok(self) -> bool:
        return self.data is not None


Call = Tuple[str, str, Dict[str, Any]]


def _call_name(call: Call) -> str:
    obj, method, args = call
    path = args.get("path")
    return f"{obj} {method}" + (f" {path}" if path else "")


def _answer(call: Call, response: object) -> Answer:
    """The outcome of one call from its JSON-RPC response object."""
    name = _call_name(call)
    if not isinstance(response, dict):
        return Answer(problem=f"{name}: the router's answer was not a JSON-RPC response")
    error = response.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code == RPC_ACCESS_DENIED:
            return Answer(problem=f"{name}: access denied", denied=True, session_refused=True)
        if code == RPC_OBJECT_NOT_FOUND:
            return Answer(problem=f"{name}: not available on this router (object not found)")
        return Answer(problem=f"{name}: JSON-RPC error {clean_text(repr(code), 20)} "
                              f"{clean_text(error.get('message')) or ''}".rstrip())
    result = response.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], int) or isinstance(result[0], bool):
        return Answer(problem=f"{name}: the router's answer had no result")
    if result[0] != UBUS_OK:
        status = UBUS_STATUS.get(result[0], f"status {result[0]}")
        return Answer(problem=f"{name}: {status}", denied=result[0] == UBUS_PERMISSION_DENIED)
    data = result[1] if len(result) > 1 else {}
    if not isinstance(data, dict):
        return Answer(problem=f"{name}: the router's answer was not an object")
    return Answer(data=data)


def _count(value: object) -> Optional[int]:
    """A non-negative integer from a JSON number; None for anything else (a bool, a float, a string, a negative)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def wan_device(dump: dict, interface: str) -> Tuple[Optional[str], Optional[str]]:
    """(layer-3 device, None) of the logical interface in a `network.interface dump`, or (None, why not)."""
    entries = dump.get("interface")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("interface") == interface:
            device = entry.get("l3_device") or entry.get("device")
            if not entry.get("up"):
                return None, f"interface {interface!r} is down"
            if not isinstance(device, str) or not device:
                return None, f"interface {interface!r} has no device"
            return device, None
    return None, f"the router has no interface {interface!r} (set options.wan_interface)"


def device_counters(devices: dict, device: str) -> Optional[Tuple[int, int]]:
    """(rx_bytes, tx_bytes) of a device in `luci-rpc getNetworkDevices`, or None."""
    entry = devices.get(device)
    stats = entry.get("stats") if isinstance(entry, dict) else None
    if not isinstance(stats, dict):
        return None
    rx, tx = _count(stats.get("rx_bytes")), _count(stats.get("tx_bytes"))
    return (rx, tx) if rx is not None and tx is not None else None


def memory_pct(info: dict) -> Optional[float]:
    """Memory in use, in percent, from `system info`: what is not available (MemAvailable), or on kernels that do
    not report that, what is neither free nor buffers or page cache."""
    memory = info.get("memory")
    if not isinstance(memory, dict):
        return None
    total = _count(memory.get("total"))
    if not total:
        return None
    available = _count(memory.get("available"))
    if available is None:
        parts = [_count(memory.get(k)) for k in ("free", "buffered", "cached")]
        if parts[0] is None:
            return None
        available = sum(p or 0 for p in parts)
    return round(max(0.0, min(100.0, (total - min(available, total)) / total * 100.0)), 1)


def _file_data(answer: Answer) -> Optional[str]:
    data = answer.data.get("data") if answer.ok else None
    return data if isinstance(data, str) else None


def arp_online(text: str) -> Dict[str, bool]:
    """MAC -> whether its ARP entry is complete (flag 0x2), from /proc/net/arp."""
    online: Dict[str, bool] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        mac = normal_mac(fields[3])
        try:
            flags = int(fields[2], 16)
        except ValueError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            online[mac] = online.get(mac, False) or bool(flags & 0x2)
    return online


def wifi_interfaces(wireless: dict) -> Dict[str, Optional[str]]:
    """Wi-Fi interface name -> its band, from `luci-rpc getWirelessDevices` (radio -> its interfaces). The band
    comes from the interface's frequency, else the radio's configured band (`band`, or the older `hwmode`)."""
    found: Dict[str, Optional[str]] = {}
    for radio in wireless.values():
        if not isinstance(radio, dict):
            continue
        config = radio.get("config") if isinstance(radio.get("config"), dict) else {}
        radio_band = {"2g": BAND_2G, "5g": BAND_5G, "6g": BAND_6G}.get(str(config.get("band", "")).lower()) or \
            {"11g": BAND_2G, "11b": BAND_2G, "11a": BAND_5G}.get(str(config.get("hwmode", "")).lower())
        for iface in radio.get("interfaces") if isinstance(radio.get("interfaces"), list) else []:
            if not isinstance(iface, dict):
                continue
            name = iface.get("ifname")
            if not isinstance(name, str) or not name or len(name) > 32:
                continue
            iwinfo = iface.get("iwinfo") if isinstance(iface.get("iwinfo"), dict) else {}
            found[name] = band_of(_count(iwinfo.get("frequency"))) or radio_band
    return found


def band_of(mhz: Optional[int]) -> Optional[str]:
    """The Wi-Fi band of a frequency in MHz; None when it is not known."""
    if not mhz:
        return None
    if 2400 <= mhz < 2500:
        return BAND_2G
    if 4900 <= mhz < 5925:
        return BAND_5G
    if 5925 <= mhz <= 7125:
        return BAND_6G
    return BAND_OTHER


def _rate_mbps(entry: object) -> Optional[float]:
    rate = _count(entry.get("rate")) if isinstance(entry, dict) else None
    return round(rate / 1000.0, 1) if rate else None  # kbit/s


def _signal(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and -120 <= value < 0 else None


def build_clients(leases: Optional[dict], stations: Dict[str, Tuple[Optional[str], List[dict]]],
                  arp: Optional[Dict[str, bool]]) -> ClientsReading:
    """The clients: every Wi-Fi station (online, with its band, signal and link rates), and every DHCP lease that is
    on no radio as a wired client. Such a client is online when its ARP entry is complete, or, when the ARP table could
    not be read, always (OpenWrt cannot then tell a wired client from a Wi-Fi one that left before its lease ran
    out). Names and addresses come from the leases."""
    by_mac: Dict[str, dict] = {}
    lease_list = (leases or {}).get("dhcp_leases")
    for lease in lease_list if isinstance(lease_list, list) else []:
        if isinstance(lease, dict):
            mac = normal_mac(lease.get("macaddr"))
            if mac:
                by_mac.setdefault(mac, lease)
    clients: List[Client] = []
    seen = set()
    for _, (band, entries) in sorted(stations.items()):
        for station in entries:
            mac = normal_mac(station.get("mac")) if isinstance(station, dict) else None
            if not mac or mac in seen:
                continue
            seen.add(mac)
            lease = by_mac.get(mac, {})
            hostname = clean_text(lease.get("hostname"))
            clients.append(Client(
                key=mac, label_parts=(hostname,) if hostname and hostname != "*" else (), ip=clean_text(lease.get("ipaddr")),
                mac=mac, connection="wifi", band=band or BAND_OTHER, rssi_dbm=_signal(station.get("signal")),
                link_tx_mbps=_rate_mbps(station.get("tx")), link_rx_mbps=_rate_mbps(station.get("rx")),
                connected_s=_count(station.get("connected_time")), online=True,
            ))
    for mac, lease in by_mac.items():
        if mac in seen:
            continue
        hostname = clean_text(lease.get("hostname"))
        clients.append(Client(key=mac, label_parts=(hostname,) if hostname and hostname != "*" else (),
                              ip=clean_text(lease.get("ipaddr")), mac=mac, connection="wired",
                              online=arp.get(mac, False) if arp is not None else True))
    return ClientsReading(tuple(clients[:MAX_CLIENTS]))


class OpenWrtDriver:
    name = "openwrt"
    label = "OpenWrt router"  # what the device is, in the dashboard's words (devices.summary.device_label)
    offers = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM, Capability.CLIENT_INVENTORY})
    options_model = OpenWrtOptions
    default_poll_seconds = 10.0  # one HTTP request per poll; rpcd answers it from memory
    default_username = "root"

    def __init__(self, url: str, username: str, password: Optional[str], *, wan_interface: str = "wan",
                 clientlist_interval_seconds: float = 60.0, client: Optional[httpx.AsyncClient] = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time,
                 verify: Verify = True):
        self._username = username
        self._password = password
        self._verify = verify
        self._wan_interface = wan_interface
        self._clientlist_interval = clientlist_interval_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._session: Optional[str] = None
        self._request_id = 0
        self.http = DeviceHttp(url, self._new_client, driver=self.name, client=client)
        self._log = get_logger("pengumon.drivers.openwrt")
        self._cpu = CpuMeter(clock)
        self._clients_read_at: Optional[float] = None
        self._client_count: Optional[int] = None
        self._clients_missing: Dict[Capability, str] = {}
        self._model: Optional[str] = None
        self._board_read_at: Optional[float] = None
        self._denied_logged: set = set()
        self._acl_refused: set = set()  # calls refused right after a login: the ACL lacks them (not the session)

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None, **clocks: Callable[[], float]) -> "OpenWrtDriver":
        """`clocks` (clock, wall_clock) are for tests."""
        options = cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, credentials.username, credentials.password, wan_interface=options.wan_interface,
                   clientlist_interval_seconds=options.clientlist_interval_seconds, client=client,
                   verify=device_verify(cfg), **clocks)

    def _new_client(self) -> httpx.AsyncClient:
        return new_client(self._verify)

    @property
    def login_name(self) -> str:
        """The login the driver signs in as: its error texts name it (the page shows them), the log records must not."""
        return self._username

    @property
    def host(self) -> str:
        return self.http.host

    def missing_configuration(self) -> Optional[str]:
        return None if self._password else "no password"

    def set_credentials(self, username: str, password: str) -> None:
        self._username, self._password = username, password
        self._session = None  # the next poll logs in with the new credentials

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- JSON-RPC -------------------------------------------------------------------------------------------------

    def _request(self, session: str, call: Call) -> dict:
        obj, method, args = call
        if (obj, method) not in READ_METHODS:  # a guard for future changes: only calls that report state
            raise DriverError("internal", f"the openwrt driver does not call {obj} {method}")
        self._request_id += 1
        return {"jsonrpc": "2.0", "id": self._request_id, "method": "call", "params": [session, obj, method, args]}

    async def _post(self, body: object) -> object:
        """POSTs a JSON-RPC request (or a batch) and returns the parsed answer. Transport errors propagate."""
        res = await self.http.post(ENDPOINT, json=body)
        if res.status_code == 404:
            raise DriverError("protocol", f"OpenWrt at {self.host} has no /ubus endpoint (install uhttpd-mod-ubus, "
                                          "or LuCI, which brings it)")
        try:
            return json.loads(res.text)
        except (ValueError, RecursionError):  # (a nesting deeper than the recursion limit)
            raise DriverError("protocol", f"OpenWrt at {self.host} answered /ubus with HTTP {res.status_code} and "
                                          "no JSON") from None

    async def _login(self) -> None:
        """Logs in and keeps the session id. Raises DriverError when refused; transport errors propagate."""
        call: Call = ("session", "login", {"username": self._username, "password": self._password or ""})
        body = {"jsonrpc": "2.0", "id": 0, "method": "call", "params": [NO_SESSION, *call]}  # not in READ_METHODS
        answer = _answer(call, await self._post(body))
        session = answer.data.get("ubus_rpc_session") if answer.ok else None
        if isinstance(session, str) and 0 < len(session) <= 64 and session.isalnum():
            self._session = session
            return
        if answer.denied:
            raise DriverError("auth", f"OpenWrt at {self.host} refused the login of {self._username!r}. Check the "
                                      "username and password (an rpcd login, see docs/devices.md).")
        raise DriverError("protocol", f"OpenWrt login at {self.host} failed: {answer.problem or 'no session id'}")

    async def _batch(self, calls: List[Call]) -> List[Answer]:
        """The calls in one request, logged in first if needed. A session the router refuses (expired, or rpcd lost
        it) gets one fresh login and one retry. A call still refused right after a login is refused by the ACL: it is
        remembered, so that its refusal does not cost a login on every later poll."""
        fresh = self._session is None
        if fresh:
            await self._login()
        for attempt in (1, 2):
            requests = [self._request(self._session or NO_SESSION, call) for call in calls]
            body = await self._post(requests)
            if not isinstance(body, list):
                raise DriverError("protocol", f"OpenWrt at {self.host} did not answer a batch of ubus calls with a "
                                              "list")
            by_id = {r["id"]: r for r in body  # (only the ids asked for: a list or an object as id is unhashable)
                     if isinstance(r, dict) and isinstance(r.get("id"), int) and not isinstance(r["id"], bool)}
            answers = [_answer(call, by_id.get(req["id"])) for call, req in zip(calls, requests)]
            for call, answer in zip(calls, answers):
                name = _call_name(call)
                if fresh and answer.session_refused:
                    self._acl_refused.add(name)
                elif answer.ok:
                    self._acl_refused.discard(name)  # the ACL was changed
            if fresh or attempt == 2 or not any(a.session_refused and _call_name(c) not in self._acl_refused
                                                for c, a in zip(calls, answers)):
                return answers
            self._log.debug("ubus refused the session; logging in again")
            self._session = None
            await self._login()
            fresh = True
        return answers  # not reached

    # --- poll -----------------------------------------------------------------------------------------------------

    async def poll(self) -> PollResult:
        now = self._clock()
        clients_due = self._clients_read_at is None or now - self._clients_read_at >= self._clientlist_interval
        board_due = self._board_read_at is None or now - self._board_read_at >= BOARD_INTERVAL_SECONDS
        calls: List[Call] = [
            ("network.interface", "dump", {}), ("luci-rpc", "getNetworkDevices", {}), ("system", "info", {}),
            ("file", "read", {"path": PROC_STAT}),
        ]
        if board_due:
            calls.append(("system", "board", {}))
        if clients_due:
            calls += [("luci-rpc", "getDHCPLeases", {}), ("luci-rpc", "getWirelessDevices", {}),
                      ("file", "read", {"path": ARP_TABLE})]
        answers = dict(zip((_call_name(c) for c in calls), await self._batch(calls)))
        read_at = self._wall_clock()
        if all(not a.ok for a in answers.values()):
            if all(a.denied for a in answers.values()):
                raise DriverError("auth", f"OpenWrt at {self.host} let {self._username!r} log in but refused every "
                                          "call: give the user the read ACL in docs/devices.md")
            raise DriverError("protocol", "; ".join(a.problem or "" for a in answers.values()))
        self._log_denied(answers)

        readings: dict = {}
        missing: dict = {}
        counters, why = self._wan(answers)
        if counters is not None:
            readings[Capability.WAN_COUNTERS] = WanCounters(rx_bytes=counters[0], tx_bytes=counters[1],
                                                            read_at=read_at)
        else:
            missing[Capability.WAN_COUNTERS] = why

        if board_due:
            self._board_read_at = now
            board = answers["system board"]
            if board.ok:
                self._model = clean_text(board.data.get("model")) or self._model

        if clients_due:
            self._clients_read_at = now
            await self._clients(answers, readings)
        missing.update({c: r for c, r in self._clients_missing.items() if c not in readings})

        info = answers["system info"]
        mem = memory_pct(info.data) if info.ok else None
        stat = _file_data(answers[f"file read {PROC_STAT}"])
        cpu = self._cpu.update(proc_stat_cpu(stat)) if stat is not None else None
        if mem is None and stat is None and self._client_count is None:
            missing[Capability.ROUTER_SYSTEM] = "; ".join(
                a.problem for a in (info, answers[f"file read {PROC_STAT}"]) if a.problem) or \
                "the router's answer had no CPU, memory or client data"
        else:
            readings[Capability.ROUTER_SYSTEM] = SystemReading(cpu_pct=cpu, mem_pct=mem, client_count=self._client_count,
                                                               model=self._model)
        return PollResult(readings=readings, missing=missing)

    async def read_wan_counters(self) -> Optional[WanCounters]:
        """The WAN counters alone, for a running speed test (Device.read_wan_counters, about once a second): the two
        calls the poll's counters come from (the interface dump, which names the WAN device, and the device
        statistics), in one request on the poll's session. rpcd reads the kernel's interface statistics when asked.
        None when the answer has no counters for the WAN device."""
        calls: List[Call] = [("network.interface", "dump", {}), ("luci-rpc", "getNetworkDevices", {})]
        answers = dict(zip((_call_name(c) for c in calls), await self._batch(calls)))
        read_at = self._wall_clock()
        counters, _ = self._wan(answers)
        return WanCounters(rx_bytes=counters[0], tx_bytes=counters[1], read_at=read_at) if counters else None

    def _wan(self, answers: Dict[str, Answer]) -> Tuple[Optional[Tuple[int, int]], str]:
        dump, devices = answers["network.interface dump"], answers["luci-rpc getNetworkDevices"]
        if not dump.ok:
            return None, dump.problem or "no interface list"
        device, why = wan_device(dump.data, self._wan_interface)
        if device is None:
            return None, why or "no WAN device"
        if not devices.ok:
            return None, devices.problem or "no device statistics"
        counters = device_counters(devices.data, device)
        return (counters, "") if counters else (None, f"the router reported no byte counters for {device!r}")

    async def _clients(self, answers: Dict[str, Answer], readings: dict) -> None:
        """The client list (DHCP leases, Wi-Fi stations, ARP) and the count; the stations are a second request."""
        leases, wireless = answers["luci-rpc getDHCPLeases"], answers["luci-rpc getWirelessDevices"]
        if not leases.ok and not wireless.ok:
            reason = "; ".join(a.problem for a in (leases, wireless) if a.problem)
            self._clients_missing = {Capability.CLIENT_INVENTORY: reason}
            self._client_count = None
            return
        interfaces = wifi_interfaces(wireless.data) if wireless.ok else {}
        stations: Dict[str, Tuple[Optional[str], List[dict]]] = {}
        names = sorted(interfaces)[:MAX_STATIONS_INTERFACES]
        if names:
            calls: List[Call] = [("iwinfo", "assoclist", {"device": name}) for name in names]
            try:
                results = await self._batch(calls)
            except (httpx.HTTPError, DriverError) as ex:  # the first request worked: keep what it brought
                self._log.debug(f"Wi-Fi stations not read: {type(ex).__name__}")
                results = [Answer(problem="not read")] * len(calls)
            for name, answer in zip(names, results):
                entries = answer.data.get("results") if answer.ok else None
                if isinstance(entries, list):
                    stations[name] = (interfaces[name], [e for e in entries if isinstance(e, dict)])
                elif answer.denied:
                    self._log_denied({f"iwinfo assoclist {name}": answer})
        arp_text = _file_data(answers[f"file read {ARP_TABLE}"])
        clients = build_clients(leases.data if leases.ok else None, stations,
                                arp_online(arp_text) if arp_text is not None else None)
        self._clients_missing = {}
        self._client_count = sum(1 for c in clients.clients if c.online)
        readings[Capability.CLIENT_INVENTORY] = clients

    def _log_denied(self, answers: Dict[str, Answer]) -> None:
        """One warning per call the ACL refuses, for the life of the process (the docs have the ACL to add)."""
        for name, answer in answers.items():
            if answer.denied and name not in self._denied_logged:
                self._denied_logged.add(name)
                self._log.warning(f"The OpenWrt router refused {name}; the readings that "
                                  "need it are left out (docs/devices.md has the rpcd ACL)", call=name)
