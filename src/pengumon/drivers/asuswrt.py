"""AsusWRT routers (RT-AX, ...): WAN byte counters, CPU, memory, the connected-client count, the client list and, on
an AiMesh network, the mesh nodes.

Read-only: the router's status hooks on appGet.cgi (and update.cgi for the counters as a fallback), with the
login token the Asus mobile app uses. One poll is normally one request carrying every hook; firmware that cannot
answer several hooks at once gets one request per hook, and the combined form is tried again every hour.
"""

import base64
import json
import re
import time
from typing import TYPE_CHECKING, Callable, ClassVar, Dict, List, NamedTuple, Optional, Sequence, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pengumon.capabilities import (
    BAND_2G, BAND_5G, BAND_6G, BAND_OTHER, Capability, Client, ClientsReading, DriverError, ErrorKind, MeshNode, MeshReading,
    PollResult, SystemReading, WanCounters,
)
from pengumon.capabilities.contrib import DriverNotes
from pengumon.drivers.common import CPU_MIN_INTERVAL_SECONDS, CpuCounters
from pengumon.drivers.common import cpu_pct as _cpu_pct
from pengumon.drivers.http import DeviceHttp, Verify, device_verify
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

USER_AGENT = "asusrouter-Android-DUTUtil-1.0.0.245"

_NUM = r'["\']?\s*:\s*["\']?'
_RE_RX = re.compile(r'INTERNET_rx' + _NUM + r'(0x[0-9a-fA-F]+|\d+)')
_RE_TX = re.compile(r'INTERNET_tx' + _NUM + r'(0x[0-9a-fA-F]+|\d+)')
_RE_CPU = re.compile(r'cpu(\d+)_total["\']?\s*:\s*["\']?(\d+)["\']?.*?cpu\1_usage["\']?\s*:\s*["\']?(\d+)')
_RE_MEM_TOTAL = re.compile(r'mem_total["\']?\s*:\s*["\']?(\d+)')
_RE_MEM_USED = re.compile(r'mem_used["\']?\s*:\s*["\']?(\d+)')
_RE_UPDATE_NETDEV = re.compile(
    r'["\']?(?:INTERNET|eth0|vlan2|wan0)["\']?\s*:\s*\{[^}]*?rx["\']?\s*:\s*["\']?(\d+)[^}]*?tx["\']?\s*:\s*["\']?(\d+)')

NETDEV_HOOKS = ["netdev()", "cpu_usage()", "memory_usage()"]
CLIENTLIST_HOOK = "get_clientlist()"
MESH_NODES_HOOK = "get_cfg_clientlist()"  # the AiMesh nodes (alias, model, MACs); changes almost never

# After the router has shown that it cannot answer several hooks in one request, the combined form is tried
# again this often (a firmware update may have fixed it).
COMBINED_RETRY_SECONDS = 3600.0


def _to_int(raw: str) -> int:
    return int(raw, 16) if raw.startswith("0x") else int(raw)


def _parse_netdev(text: str) -> Optional[Tuple[int, int]]:
    """(rx_bytes, tx_bytes) of the WAN interface from an appGet body containing netdev(), or None."""
    m_rx = _RE_RX.search(text)
    m_tx = _RE_TX.search(text)
    if m_rx and m_tx:
        return _to_int(m_rx.group(1)), _to_int(m_tx.group(1))
    if "netdev" in text:
        try:
            data = json.loads(text).get("netdev", {})
            for iface in ["eth0", "vlan2", "wan0", "eth1"]:
                if iface in data:
                    dev = data[iface]
                    rx = dev.get("rx") or dev.get("rx_bytes")
                    tx = dev.get("tx") or dev.get("tx_bytes")
                    if rx is not None and tx is not None:
                        return int(rx), int(tx)
        except (ValueError, AttributeError, TypeError):
            pass
    return None


def _json_object(text: str) -> Optional[dict]:
    """The body as a JSON object, or None if it is anything else (an HTML error page, plain text, ...)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_cpu(text: str) -> CpuCounters:
    """Per-core cumulative (total, busy) counters from an appGet body containing cpu_usage(); empty if absent.
    They count from the router's boot, so a CPU percentage needs two readings (see _cpu_pct)."""
    return {core: (int(total), int(busy)) for core, total, busy in _RE_CPU.findall(text)}


def _parse_mem(text: str) -> Optional[float]:
    """Memory use in percent from an appGet body containing memory_usage()."""
    m_tot = _RE_MEM_TOTAL.search(text)
    m_used = _RE_MEM_USED.search(text)
    if m_tot and m_used and int(m_tot.group(1)) > 0:
        return round((int(m_used.group(1)) / int(m_tot.group(1))) * 100.0, 1)
    return None


def _object_section(text: str, key: str) -> Optional[dict]:
    """The JSON object that follows `"key":` in an appGet body, parsed on its own. Some firmware (an RT-AX model, for
    one) answers several hooks with a body that is not valid JSON as a whole ("cpu_usage":"cpu1_total":"..."
    without braces), while each object inside it is; parsing the whole body then loses every section."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, m.end() - 1)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _has_status_data(text: str) -> bool:
    """Whether a body that is not valid JSON still carries CPU, memory or client data (some firmware's combined
    answers never are valid JSON; a maintenance HTML page carries none)."""
    return bool(_parse_cpu(text)) or _parse_mem(text) is not None or _parse_clients(text) is not None


def _parse_clients(text: str) -> Optional[int]:
    """Number of online clients from an appGet body containing get_clientlist(); None if it isn't there."""
    cl_data = _object_section(text, "get_clientlist")
    if cl_data is None:
        return None
    entries = [v for v in cl_data.values() if isinstance(v, dict)]  # not "maclist" / "ClientAPILevel"
    online = sum(1 for v in entries if v.get("isOnline") == "1" or v.get("online") == "1")
    if online == 0 and entries:
        online = len(entries)
    return online


_RE_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}$")
_RE_HMS = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d)$")
# A mesh node's own MAC, then its radios'. Not the pap*: those are its parent's. Not wired_mac either: it lists the
# node's wired neighbours (on an AiMesh main router, the satellites' own MACs), not the node.
_MAC_NODE_FIELDS = ("mac", "ap2g", "ap5g", "ap5g1", "ap6g", "ap6g1", "ap2g_fh", "ap5g_fh", "ap5g1_fh", "ap6g_fh",
                    "ap6g1_fh", "apdwb")
_BANDS = {"1": BAND_2G, "2": BAND_5G}  # isWL: 0 wired; 3 and anything else is BAND_OTHER (a 2nd high band, not 6 GHz)
_TEXT_MAX = 64
# The firmware's numeric client `type` as plain words. Only the types seen on an AiMesh router with their vendors are
# mapped (9 Android devices, 10 and 6 Apple, 34 Windows and Linux PCs, 22 dhcpcd/Linux hosts, 7 game consoles, 24 Asus
# network gear); the rest, including type 0 (the default) and 18 (printers and cameras and radio modules share it),
# stay None so the page shows nothing rather than a wrong icon. The word is a key the page maps to a label and icon.
_CLIENT_KINDS = {"9": "phone", "10": "phone", "6": "computer", "34": "computer", "1": "computer", "22": "computer",
                 "7": "game", "24": "network"}


def _client_kind(entry: dict) -> Optional[str]:
    """phone / computer / game / network / printer for a client the firmware has classified, else None."""
    if str(entry.get("isPrinter")).strip() == "1":
        return "printer"
    if str(entry.get("isGateway")).strip() == "1":
        return "network"
    return _CLIENT_KINDS.get(str(entry.get("type", "")).strip())


def _mac_id(value: object) -> Optional[str]:
    """A mesh node's short id: the last six hex digits of a MAC, lower case; None if `value` is not a MAC."""
    if isinstance(value, str) and _RE_MAC.match(value.strip()):
        return re.sub(r"[^0-9a-f]", "", value.lower())[-6:]
    return None


def node_id(value: str) -> Optional[str]:
    """The id for a MAC or for the six hex digits of one (what `mesh_node_labels` keys are), else None."""
    value = value.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return value.lower()
    return _mac_id(value)


def _text(value: object) -> Optional[str]:
    """A name or a vendor from the router: printable, trimmed and short, None when empty."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value).strip()[:_TEXT_MAX]
    return cleaned or None


def _num(value: object) -> Optional[float]:
    """A number from the router's strings; None when empty, not a number or negative."""
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def _connected_seconds(value: object) -> Optional[int]:
    """"HH:MM:SS" (hours may pass 24) as seconds; None for anything else, which includes the empty string of a wired
    client."""
    m = _RE_HMS.match(str(value).strip())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def _parse_client_list(text: str) -> Optional[ClientsReading]:
    """The clients from an appGet body containing get_clientlist(); None if it isn't there. Online status follows
    _parse_clients (so the count and the list agree). A client that is not online keeps no signal, rates or time:
    the router leaves stale values on it. No traffic is read: the per-client byte counters are empty on the firmware
    checked, and curTx/curRx are Wi-Fi link rates, not traffic."""
    cl_data = _object_section(text, "get_clientlist")
    if cl_data is None:
        return None
    entries = [(k, v) for k, v in cl_data.items() if isinstance(v, dict)]
    flags = [v.get("isOnline") == "1" or v.get("online") == "1" for _, v in entries]
    if entries and not any(flags):
        flags = [True] * len(entries)  # same fallback as _parse_clients
    clients = []
    for (key, v), online in zip(entries, flags):
        raw = v.get("mac") if isinstance(v.get("mac"), str) and v["mac"].strip() else key
        mac = raw.strip().upper().replace("-", ":") if _RE_MAC.match(raw.strip()) else None
        wifi = str(v.get("isWL", "0")).strip() not in ("0", "")
        rssi = _num(str(v.get("rssi", "")).lstrip("-")) if online and wifi else None
        rssi = -int(rssi) if rssi else None  # 0 means "no signal value"; dBm is negative
        clients.append(Client(
            key=mac or str(key),
            label_parts=tuple(n for n in (_text(v.get("nickName")), _text(v.get("name"))) if n),
            vendor=_text(v.get("vendor")),
            kind=_client_kind(v),
            ip=_text(v.get("ip")),
            mac=mac,
            connection="wifi" if wifi else "wired",
            band=_BANDS.get(str(v.get("isWL")).strip(), BAND_OTHER) if wifi else None,
            node=_mac_id(v.get("amesh_papMac")) if online else None,
            rssi_dbm=rssi,
            link_tx_mbps=_num(v.get("curTx")) if online and wifi else None,
            link_rx_mbps=_num(v.get("curRx")) if online and wifi else None,
            connected_s=_connected_seconds(v.get("wlConnectTime")) if online and wifi else None,
            online=online,
        ))
    return ClientsReading(tuple(clients))


def _parse_mesh_names(text: str) -> Dict[str, str]:
    """Node id -> label from an appGet body containing get_cfg_clientlist(): the node's alias, else its model name.
    Every MAC a node is known by (its own and its radios') maps to it, because the clients name the MAC of the radio
    they are on. Field by field across the nodes, so a node's own MAC is never taken by another node's radio field.
    Empty if the answer has no nodes."""
    body = _json_object(text)
    nodes = body.get("get_cfg_clientlist") if body else None
    labelled = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        alias = _text(node.get("alias"))
        label = alias if alias and not _RE_MAC.match(alias) else _text(node.get("model_name"))
        if label:
            labelled.append((node, label))
    names: Dict[str, str] = {}
    for field in _MAC_NODE_FIELDS:
        for node, label in labelled:
            nid = _mac_id(node.get(field))
            if nid:
                names.setdefault(nid, label)
    return names


class _NodeInfo(NamedTuple):
    """What get_cfg_clientlist() says about one node: its own MAC's id, its label (alias, else model), whether it is
    online (None: the answer has no `online`) and how it is connected to its parent."""

    id: Optional[str]
    label: Optional[str]
    online: Optional[bool]
    backhaul: Optional[str]
    band: Optional[str]
    rssi_dbm: Optional[int]


# The fields of a node that name its parent's radio (and the signal to it): the node is on Wi-Fi to its parent when
# one of them holds a MAC. Left empty by a wired node, and by the main router.
_BACKHAUL_RADIOS = (("pap6g", "rssi6g", BAND_6G), ("pap5g1", "rssi5g1", BAND_OTHER), ("pap5g", "rssi5g", BAND_5G),
                    ("pap2g", "rssi2g", BAND_2G))


def _node_info(node: dict, wired_neighbours: set) -> _NodeInfo:
    """One node of get_cfg_clientlist(). `wired_neighbours` holds the ids listed in any node's `wired_mac`: the main
    router lists the satellites it reaches by cable, so a node found there with no parent radio is on a cable.
    Whether a satellite on Wi-Fi fills pap*/rssi* was not seen on the firmware checked (every pap* was empty on a
    AiMesh network whose satellites are wired); that part follows the field names only."""
    online = {"1": True, "0": False}.get(str(node.get("online", "")).strip())
    nid = _mac_id(node.get("mac"))
    alias = _text(node.get("alias"))
    label = alias if alias and not _RE_MAC.match(alias) else _text(node.get("model_name"))
    for pap, rssi, band in _BACKHAUL_RADIOS:
        if _mac_id(node.get(pap)):
            raw = _num(str(node.get(rssi, "")).lstrip("-"))
            return _NodeInfo(nid, label, online, "wifi", band, -int(raw) if raw else None)
    return _NodeInfo(nid, label, online, "wired" if nid in wired_neighbours else None, None, None)


def _parse_mesh_nodes(text: str) -> Tuple[List[_NodeInfo], Dict[str, _NodeInfo]]:
    """(the nodes in the router's order, node id -> its info for every MAC the node is known by) from an appGet body
    containing get_cfg_clientlist(). The MACs map field by field exactly as _parse_mesh_names does."""
    body = _json_object(text)
    nodes = body.get("get_cfg_clientlist") if body else None
    raw = [n for n in (nodes if isinstance(nodes, list) else []) if isinstance(n, dict)]
    neighbours = set()
    for node in raw:
        wired = node.get("wired_mac")
        neighbours.update(i for i in (_mac_id(m) for m in (wired if isinstance(wired, list) else [])) if i)
    infos = [_node_info(n, neighbours) for n in raw]
    by_id: Dict[str, _NodeInfo] = {}
    for field in _MAC_NODE_FIELDS:
        for node, info in zip(raw, infos):
            nid = _mac_id(node.get(field))
            if nid:
                by_id.setdefault(nid, info)
    return infos, by_id


def build_mesh(clients: ClientsReading, names: Dict[str, str], overrides: Dict[str, str],
               infos: Sequence[_NodeInfo] = (), info_by_id: Optional[Dict[str, _NodeInfo]] = None) -> Optional[MeshReading]:
    """The mesh nodes the online clients are on, labelled by `overrides` (the options' mesh_node_labels), else by the
    router's `names`, else "Node 1", "Node 2", ... in the order of the node ids. With the router's node list (`infos`,
    `info_by_id` from _parse_mesh_nodes) each node also carries whether it is online and its backhaul, and a node
    that carries no client (an offline one, or an idle one) is listed too, so that a node that dropped off is seen.
    Without it only nodes that carry a client are listed. None when no online client says which node it is on and
    the router lists fewer than two nodes, as with a single router."""
    info_by_id = dict(info_by_id or {})
    counts: Dict[str, List[int]] = {}
    for c in clients.clients:
        if c.online and c.node:
            total_wired = counts.setdefault(c.node, [0, 0])
            total_wired[0] += 1
            total_wired[1] += c.connection == "wired"
    seen = {id(info_by_id[nid]) for nid in counts if nid in info_by_id}
    for info in infos:  # nodes without a client; their own MAC is their id
        if id(info) not in seen and info.id and info.id not in counts and len(infos) > 1:
            counts[info.id] = [0, 0]
            info_by_id.setdefault(info.id, info)
    if not counts:
        return None
    nodes, fallback = [], 0
    for nid in sorted(counts):
        info = info_by_id.get(nid)
        label = overrides.get(nid) or names.get(nid) or (info.label if info else None)
        if not label:
            fallback += 1
            label = f"Node {fallback}"
        nodes.append(MeshNode(id=nid, label=label, clients=counts[nid][0], wired=counts[nid][1],
                              online=info.online if info else None, backhaul=info.backhaul if info else None,
                              backhaul_band=info.band if info else None,
                              backhaul_rssi_dbm=info.rssi_dbm if info else None))
    return MeshReading(tuple(nodes))


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

    @field_validator("mesh_node_labels")
    @classmethod
    def _node_labels(cls, v: Dict[str, str]) -> Dict[str, str]:
        out = {}
        for mac, label in v.items():
            nid = node_id(str(mac))
            if nid is None:
                raise ValueError(f"mesh_node_labels: {mac!r} is not a MAC address (or its last six hex digits)")
            out[nid] = label.strip()[:_TEXT_MAX]
        return out


class AsusWrtDriver:
    name = "asuswrt"
    label = "ASUS router"  # what the device is, in the dashboard's words (api.devices.device_label)
    offers = frozenset({Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM, Capability.CLIENT_INVENTORY,
                        Capability.MESH})
    options_model = AsusWrtOptions
    # WAN rates for the load-aware classifier need recent counters; one request every 10 s is light for the router.
    default_poll_seconds = 10.0
    # The AI pack's note on this driver (capabilities/contrib.py). Each statement is established by this file:
    #  - ASUS router with ASUS firmware (AsusWRT), mesh nodes only on an AiMesh network: the module docstring, :1-2,
    #    and `label` :375; `{aimesh}` is filled in only when a mesh was seen;
    #  - counters read every {poll}: the poll interval of the device entry (`default_poll_seconds` :380 when it sets
    #    none; one request carries every hook, :4-6);
    #  - client list every {clientlist_interval}, counts in between repeat the last list: `clientlist_interval_seconds`
    #    :353 and the reuse at :532-533;
    #  - CPU is the average since the previous reading: `_cpu_percent` :687-688;
    #  - node names are the alias, else the model: `_parse_mesh_names` :235-247;
    #  - offline clients the router remembers are not counted: devices/samples.py `client_counts` :18 (online only).
    # The anonymized text leaves out the vendor, firmware and mesh-network words.
    pack_notes: ClassVar[DriverNotes] = DriverNotes(
        full=("Device notes: the router is an ASUS router with ASUS firmware (AsusWRT){aimesh}. Its counters are "
              "read every {poll}, its client list every {clientlist_interval} (counts in between repeat the last "
              "list). CPU is the average since the previous reading. Node names are the owner's alias, else the "
              "model. Clients the router still remembers but are offline are not counted."),
        anonymized=("Device notes: the router reports WAN counters every {poll} and its client list every "
                    "{clientlist_interval}; CPU is the average since the previous reading."),
    )

    def __init__(self, url: str, username: str, password: Optional[str], *,
                 clientlist_interval_seconds: float = 60.0, mesh_node_names: bool = True,
                 mesh_nodes_interval_seconds: float = 60.0, mesh_node_labels: Optional[Dict[str, str]] = None,
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
        self._log = get_logger("pengumon.drivers.asuswrt", host=self.http.host)
        # Several hooks in one appGet request: assumed to work until the router answers one with a 200 JSON
        # object that has no netdev data; tried again every COMBINED_RETRY_SECONDS after that.
        self._combined_ok = True
        self._combined_retry_at = 0.0
        self._client_count: Optional[int] = None
        self._clients_read_at: Optional[float] = None
        self._cpu_counters: CpuCounters = {}
        self._cpu_counters_at: Optional[float] = None
        self._cpu_pct: Optional[float] = None

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None, **clocks: Callable[[], float]) -> "AsusWrtDriver":
        """`clocks` (clock, wall_clock) are for tests."""
        options = cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, credentials.username, credentials.password,
                   clientlist_interval_seconds=options.clientlist_interval_seconds,
                   mesh_node_names=options.mesh_node_names,
                   mesh_nodes_interval_seconds=options.mesh_nodes_interval_seconds,
                   mesh_node_labels=options.mesh_node_labels, client=client,
                   verify=device_verify(cfg), **clocks)

    def _new_client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(timeout=6.0, verify=self._verify, headers={"User-Agent": USER_AGENT})
        if self._session_cookies:
            client.cookies.update(self._session_cookies)
        return client

    @property
    def host(self) -> str:
        return self.http.host

    def missing_configuration(self) -> Optional[str]:
        return None if self._password else "no password"

    def set_credentials(self, username: str, password: str) -> None:
        self._username, self._password = username, password
        self._token = None  # the next poll logs in with the new credentials

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _login(self) -> None:
        """Logs in and keeps the token. Raises DriverError when refused; transport errors propagate."""
        raw = f"{self._username}:{self._password}".encode("utf-8")
        res = await self.http.post(
            "login.cgi",
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
                                      f"{error_status}. Check credentials.")
        raise DriverError("auth" if res.status_code in (401, 403) else "protocol",
                          f"Asus router login at {self.host} answered HTTP {res.status_code} without a token")

    async def _hook(self, hooks: List[str], problems: List[Tuple[ErrorKind, str]]) -> Optional[str]:
        """POSTs the hooks to appGet.cgi (joined with ';') and returns the body of a 200 response. Logs in again
        once if the token was refused (401/403, or an error_status body even with status 200). None if it still
        is, and for any other status (a 502 page from a struggling router is a failed poll, not an answer); the
        reason goes to `problems`. Transport errors and a refused login propagate."""
        for attempt in (1, 2):
            res = await self.http.post(
                "appGet.cgi",
                data={"hook": ";".join(hooks)},
                headers={"User-Agent": USER_AGENT, "asus_token": self._token or ""},
            )
            if res.status_code == 200 and "error_status" not in res.text:
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

        problems: List[Tuple[ErrorKind, str]] = []
        transport_error: Optional[httpx.HTTPError] = None
        fresh = False  # this poll got data from the router (not only the client count kept from an earlier one)
        fresh_clients = False
        wan: Optional[Tuple[int, int]] = None
        wan_read_at: Optional[float] = None
        cpu: CpuCounters = {}
        mem_pct: Optional[float] = None

        # The client list changes slowly and is the heaviest answer: read it every clientlist_interval_seconds
        # and reuse the last count in between.
        clients_due = (
            self._clients_read_at is None
            or self._clock() - self._clients_read_at >= self._clientlist_interval
        )
        hooks = NETDEV_HOOKS + ([CLIENTLIST_HOOK] if clients_due else [])
        if clients_due:
            self._clients_read_at = self._clock()
        client_count = self._client_count
        clients_text: Optional[str] = None  # the answer that carried get_clientlist(), when this poll got one

        text: Optional[str] = None
        if self._combined_ok or self._clock() >= self._combined_retry_at:
            if not self._combined_ok:  # the hourly retry; whatever happens, the next one is an hour away
                self._combined_retry_at = self._clock() + COMBINED_RETRY_SECONDS
            try:
                text = await self._hook(hooks, problems)
            except httpx.HTTPError as ex:
                transport_error = ex
            else:
                counters = _parse_netdev(text) if text is not None else None
                if text is not None and counters is None:
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
                        problems.append(("protocol", "appGet answer was not JSON"))
                        text = None
                    elif body is not None and "netdev" not in body:
                        text = None
                elif text is not None and not self._combined_ok:
                    self._log.info("Router answers combined appGet requests again")
                    self._combined_ok = True
            if text is not None:
                fresh = True
                wan, wan_read_at = counters, self._wall_clock()
                cpu = _parse_cpu(text)
                mem_pct = _parse_mem(text)
                if clients_due:
                    client_count = _parse_clients(text)
                    fresh_clients = client_count is not None
                    clients_text = text
        if text is None and not self._combined_ok:
            # One request per hook (firmware that rejects several hooks in one request)
            for hook in hooks:
                try:
                    part = await self._hook([hook], problems)
                except httpx.HTTPError as ex:
                    transport_error = ex
                    continue
                if part is None:
                    continue
                fresh = True
                if hook == "netdev()":
                    parsed = _parse_netdev(part)
                    if parsed:
                        wan, wan_read_at = parsed, self._wall_clock()
                elif hook == "cpu_usage()":
                    cpu = _parse_cpu(part)
                elif hook == "memory_usage()":
                    mem_pct = _parse_mem(part)
                else:
                    client_count = _parse_clients(part)
                    fresh_clients = client_count is not None
                    clients_text = part
        if clients_due:
            self._client_count = client_count

        if wan is None:  # fallback for the counters: the realtime traffic page's data source
            try:
                res = await self.http.get(
                    "update.cgi", params={"output": "netdev"},
                    headers={"User-Agent": USER_AGENT,
                             "Referer": f"{self.http.base_url}/Main_TrafficMonitor_realtime.htm"},
                )
            except httpx.HTTPError as ex:
                transport_error = ex
            else:
                m = _RE_UPDATE_NETDEV.search(res.text) if res.status_code == 200 else None
                if m:
                    wan, wan_read_at = (int(m.group(1)), int(m.group(2))), self._wall_clock()
                    fresh = True

        if not fresh:
            if problems:
                raise DriverError(*problems[0])
            if transport_error is not None:
                raise transport_error
            raise DriverError("protocol", "the router answered without WAN counters, CPU, memory or clients")

        cpu_pct = self._cpu_percent(cpu)

        readings: dict = {}
        missing: dict = {}
        if clients_due:
            self._clients_missing = {}
            if fresh_clients and clients_text is not None:
                await self._client_readings(clients_text, readings)
            else:
                reason = "the router's answer had no client list"
                self._clients_missing = {Capability.CLIENT_INVENTORY: reason, Capability.MESH: reason}
        missing.update({c: r for c, r in self._clients_missing.items() if c not in readings})
        if wan is not None:
            readings[Capability.WAN_COUNTERS] = WanCounters(rx_bytes=wan[0], tx_bytes=wan[1], read_at=wan_read_at)
        else:
            missing[Capability.WAN_COUNTERS] = problems[0][1] if problems else "the router's answer had no WAN counters"
        if not cpu and mem_pct is None and not fresh_clients:
            missing[Capability.ROUTER_SYSTEM] = "the router's answer had no CPU, memory or client data"
        else:
            readings[Capability.ROUTER_SYSTEM] = SystemReading(cpu_pct=cpu_pct, mem_pct=mem_pct,
                                                               client_count=client_count)
        return PollResult(readings=readings, missing=missing)

    async def _client_readings(self, text: str, readings: dict) -> None:
        """The client list and the mesh from the answer that carried get_clientlist(): no further request except the
        node names, asked at most every mesh_nodes_interval_seconds."""
        clients = _parse_client_list(text)
        if clients is None:
            return
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
        names = _parse_mesh_names(text) if text is not None else {}
        self._mesh_infos, self._mesh_info_by_id = _parse_mesh_nodes(text) if text is not None else ([], {})
        if names:
            self._mesh_names = names
        else:
            self._log.debug("The router gave no mesh node names; nodes are numbered")

    def _cpu_percent(self, cpu: CpuCounters) -> Optional[float]:
        """CPU % since the previous reading, which becomes the new baseline; readings less than
        CPU_MIN_INTERVAL_SECONDS apart repeat the last percentage and leave the baseline alone."""
        if not cpu:
            return None
        now = self._clock()
        if self._cpu_counters_at is not None and now - self._cpu_counters_at < CPU_MIN_INTERVAL_SECONDS:
            return self._cpu_pct
        self._cpu_pct = _cpu_pct(self._cpu_counters, cpu)
        self._cpu_counters, self._cpu_counters_at = cpu, now
        return self._cpu_pct
