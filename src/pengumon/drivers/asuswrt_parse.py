"""The parsing of an AsusWRT router's answers (drivers/asuswrt.py asks, this reads): pure functions from the text of an
answer to counters, CPU and memory figures, the client list and the mesh. Nothing here makes a request or keeps state.

The answers are read up to a size cap by the driver (asuswrt.APPGET_MAX_BYTES, UPDATE_MAX_BYTES); every parser here
stays linear in the size of what it is given, and bounds the numbers and the lists it returns.
"""

import json
import re
from bisect import bisect_left
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import httpx

from pengumon.capabilities import (
    BAND_2G, BAND_5G, BAND_6G, BAND_OTHER, Client, ClientsReading, MeshNode, MeshReading,
)
from pengumon.drivers.common import MAX_CLIENTS, MAX_MESH_NODES, CpuCounters, clean_text, normal_mac

# Every pattern below runs on an answer of up to APPGET_MAX_BYTES (asuswrt.py) and must stay linear in its size: each
# starts at a fixed word and scans only its own run of digits or spaces. Pairs (a core's total and busy count; an
# interface's rx and tx) are matched by position (`_parse_cpu`, `_parse_update_netdev`), not by a `.*?` between them,
# which made the search quadratic (200 KB of "cpu1_total:1," took 17 s before 0.27; update.cgi's pattern 34-81 s).
_NUM = r'["\']?\s*:\s*["\']?'
_RE_RX = re.compile(r'INTERNET_rx' + _NUM + r'(0x[0-9a-fA-F]+|\d+)')
_RE_TX = re.compile(r'INTERNET_tx' + _NUM + r'(0x[0-9a-fA-F]+|\d+)')
_RE_CPU_TOTAL = re.compile(r'cpu(\d+)_total' + _NUM + r'(\d+)')
_RE_CPU_USAGE = re.compile(r'cpu(\d+)_usage' + _NUM + r'(\d+)')
_RE_MEM_TOTAL = re.compile(r'mem_total["\']?\s*:\s*["\']?(\d+)')
_RE_MEM_USED = re.compile(r'mem_used["\']?\s*:\s*["\']?(\d+)')
_RE_UPDATE_IFACE = re.compile(r'["\']?(?:INTERNET|eth0|vlan2|wan0)["\']?\s*:\s*\{')
_RE_UPDATE_RX = re.compile(r'rx' + _NUM + r'(\d+)')
_RE_UPDATE_TX = re.compile(r'tx' + _NUM + r'(\d+)')
# What decoding a hostile JSON value can raise besides ValueError: a nesting deeper than the recursion limit
_JSON_ERRORS = (ValueError, RecursionError)


COUNTER_DIGITS = 20  # a 64-bit counter has at most 20 decimal digits (16 hex): a longer number is a bad one


def _to_int(raw: str) -> Optional[int]:
    """A counter from the answer: decimal or 0x hex, None for a number longer than any 64-bit counter (int() refuses
    over 4300 digits, which made the poll fail)."""
    if raw.startswith("0x"):
        return int(raw, 16) if len(raw) <= 2 + 16 else None
    return int(raw) if len(raw) <= COUNTER_DIGITS else None


def _parse_netdev(text: str) -> Optional[Tuple[int, int]]:
    """(rx_bytes, tx_bytes) of the WAN interface from an appGet body containing netdev(), or None."""
    m_rx = _RE_RX.search(text)
    m_tx = _RE_TX.search(text)
    if m_rx and m_tx:
        rx, tx = _to_int(m_rx.group(1)), _to_int(m_tx.group(1))
        return (rx, tx) if rx is not None and tx is not None else None
    if "netdev" in text:
        try:
            data = json.loads(text).get("netdev", {})
            for iface in ["eth0", "vlan2", "wan0", "eth1"]:
                if iface in data:
                    dev = data[iface]
                    rx = _json_counter(dev["rx"] if "rx" in dev else dev.get("rx_bytes"))
                    tx = _json_counter(dev["tx"] if "tx" in dev else dev.get("tx_bytes"))
                    if rx is not None and tx is not None:
                        return rx, tx
        except (ValueError, AttributeError, TypeError, RecursionError):
            pass
    return None


def _json_counter(value: object) -> Optional[int]:
    """A counter from a JSON value: a whole number of 0 or more (or a string of its decimal digits) of at most
    COUNTER_DIGITS digits; None for anything else (a negative or fractional number, Infinity, a boolean)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < 10 ** COUNTER_DIGITS else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return _to_int(value)
    return None


def _json_object(text: str) -> Optional[dict]:
    """The body as a JSON object, or None if it is anything else (an HTML error page, plain text, ...)."""
    try:
        data = json.loads(text)
    except _JSON_ERRORS:
        return None
    return data if isinstance(data, dict) else None


REFUSAL_MAX = 1024  # an answer that is not JSON and names error_status is a refusal only when it is this short


def _token_refused(text: str) -> bool:
    """Whether an appGet answer refuses the session token: a JSON object with a top-level error_status, or a short
    answer that is not JSON and names it. Never a word inside the client list: a client's name is its own choice (its
    DHCP host name), and one called "error_status" made every client-list read look refused."""
    if "error_status" not in text:
        return False
    body = _json_object(text)
    if body is not None:
        return "error_status" in body
    return len(text) <= REFUSAL_MAX


def _positions(text: str, char: str) -> List[int]:
    """The positions of every `char` in `text`, in order."""
    out, i = [], text.find(char)
    while i >= 0:
        out.append(i)
        i = text.find(char, i + 1)
    return out


def _next_on_line(starts: List[int], after: int, newlines: List[int]) -> int:
    """The index in `starts` (sorted positions) of the first one at or after `after` with no newline between; -1 if
    none."""
    k = bisect_left(starts, after)
    if k == len(starts):
        return -1
    n = bisect_left(newlines, after)
    return k if n == len(newlines) or newlines[n] >= starts[k] else -1


def _parse_cpu(text: str) -> CpuCounters:
    """Per-core cumulative (total, busy) counters from an appGet body containing cpu_usage(); empty if absent.
    They count from the router's boot, so a CPU percentage needs two readings (drivers/common.py CpuMeter).

    Each `cpuN_total: <n>` pairs with the first `cpuN_usage: <n>` after it on the same line, scanning on after that
    pair (what the pattern `cpu(\\d+)_total...(\\d+).*?cpu\\1_usage...(\\d+)` with findall found, in linear time;
    a test compares them). A later pair of a core replaces an earlier one; numbers too long to be counters are left
    out."""
    usages: Dict[str, List[re.Match]] = {}
    for u in _RE_CPU_USAGE.finditer(text):
        usages.setdefault(u.group(1), []).append(u)
    starts = {core: [u.start() for u in found] for core, found in usages.items()}
    newlines = _positions(text, "\n") if usages else []
    out: CpuCounters = {}
    pos = 0
    for t in _RE_CPU_TOTAL.finditer(text):
        core = t.group(1)
        if t.start() < pos or core not in usages:
            continue
        k = _next_on_line(starts[core], t.end(), newlines)
        if k < 0:
            continue
        u = usages[core][k]
        pos = u.end()
        total, busy = _to_int(t.group(2)), _to_int(u.group(2))
        if total is not None and busy is not None:
            out[core] = (total, busy)
    return out


def _parse_update_netdev(text: str) -> Optional[Tuple[int, int]]:
    """(rx, tx) of the WAN interface from update.cgi?output=netdev: the first INTERNET, eth0, vlan2 or wan0 object
    (`"INTERNET": {...rx: <n>...tx: <n>`, before the object's first `}`) that has both, else None. Matched by position
    in linear time, as the pattern `...\\{[^}]*?rx...(\\d+)[^}]*?tx...(\\d+)` did with backtracking."""
    rxs = list(_RE_UPDATE_RX.finditer(text))
    txs = list(_RE_UPDATE_TX.finditer(text))
    if not rxs or not txs:
        return None
    rx_starts, tx_starts = [m.start() for m in rxs], [m.start() for m in txs]
    closes = _positions(text, "}")
    for opening in _RE_UPDATE_IFACE.finditer(text):
        c = bisect_left(closes, opening.end())
        end = closes[c] if c < len(closes) else len(text)
        k = bisect_left(rx_starts, opening.end())
        if k == len(rxs) or rx_starts[k] >= end:
            continue
        j = bisect_left(tx_starts, rxs[k].end())
        if j == len(txs) or tx_starts[j] >= end:
            continue
        rx, tx = _to_int(rxs[k].group(1)), _to_int(txs[j].group(1))
        return (rx, tx) if rx is not None and tx is not None else None
    return None


def _parse_mem(text: str) -> Optional[float]:
    """Memory use in percent from an appGet body containing memory_usage()."""
    m_tot = _RE_MEM_TOTAL.search(text)
    m_used = _RE_MEM_USED.search(text)
    if not (m_tot and m_used):
        return None
    total, used = _to_int(m_tot.group(1)), _to_int(m_used.group(1))
    if total is None or used is None or total <= 0:
        return None
    return round((used / total) * 100.0, 1)


def _decode(headers: httpx.Headers, body: bytes) -> str:
    """The body as text in the charset its Content-Type names (UTF-8 when none, or one Python does not know), as
    httpx's Response.text decodes it."""
    charset = None
    for param in headers.get("content-type", "").split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("\"'") or None
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeError, ValueError):  # unknown, or not a text encoding ("idna" refuses "replace")
        return body.decode("utf-8", errors="replace")


def _object_section(text: str, key: str) -> Optional[dict]:
    """The JSON object that follows `"key":` in an appGet body, parsed on its own. Some firmware (an RT-AX model, for
    one) answers several hooks with a body that is not valid JSON as a whole ("cpu_usage":"cpu1_total":"..."
    without braces), while each object inside it is; parsing the whole body then loses every section."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, m.end() - 1)
    except _JSON_ERRORS:
        return None
    return value if isinstance(value, dict) else None


def _has_status_data(text: str) -> bool:
    """Whether a body that is not valid JSON still carries CPU, memory or client data (some firmware's combined
    answers never are valid JSON; a maintenance HTML page carries none)."""
    return bool(_parse_cpu(text)) or _parse_mem(text) is not None or _parse_clients(text) is not None


def _online_count(clients: Optional[ClientsReading]) -> Optional[int]:
    """The client count _parse_clients gives, from the parsed list (the same rule: online entries, or all of them when
    none says it is online), so the answer is parsed once."""
    return sum(1 for c in clients.clients if c.online) if clients is not None else None


def _parse_clients(text: str) -> Optional[int]:
    """Number of online clients from an appGet body containing get_clientlist(); None if it isn't there."""
    cl_data = _object_section(text, "get_clientlist")
    if cl_data is None:
        return None
    entries = [v for v in cl_data.values() if isinstance(v, dict)][:MAX_CLIENTS]  # not "maclist" / "ClientAPILevel"
    online = sum(1 for v in entries if v.get("isOnline") == "1" or v.get("online") == "1")
    if online == 0 and entries:
        online = len(entries)
    return online


_RE_HMS = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d)$")
# A mesh node's own MAC, then its radios'. Not the pap*: those are its parent's. Not wired_mac either: it lists the
# node's wired neighbors (on an AiMesh main router, the satellites' own MACs), not the node.
_MAC_NODE_FIELDS = ("mac", "ap2g", "ap5g", "ap5g1", "ap6g", "ap6g1", "ap2g_fh", "ap5g_fh", "ap5g1_fh", "ap6g_fh",
                    "ap6g1_fh", "apdwb")
_BANDS = {"1": BAND_2G, "2": BAND_5G}  # isWL: 0 wired; 3 and anything else is BAND_OTHER (a 2nd high band, not 6 GHz)
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
    mac = normal_mac(value)
    return re.sub(r"[^0-9a-f]", "", mac.lower())[-6:] if mac else None


def node_id(value: str) -> Optional[str]:
    """The id for a MAC or for the six hex digits of one (what `mesh_node_labels` keys are), else None."""
    value = value.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return value.lower()
    return _mac_id(value)


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
    entries = [(k, v) for k, v in cl_data.items() if isinstance(v, dict)][:MAX_CLIENTS]
    flags = [v.get("isOnline") == "1" or v.get("online") == "1" for _, v in entries]
    if entries and not any(flags):
        flags = [True] * len(entries)  # same fallback as _parse_clients
    clients = []
    for (key, v), online in zip(entries, flags):
        raw = v.get("mac") if isinstance(v.get("mac"), str) and v["mac"].strip() else key
        mac = normal_mac(raw)
        wifi = str(v.get("isWL", "0")).strip() not in ("0", "")
        rssi = _num(str(v.get("rssi", "")).lstrip("-")) if online and wifi else None
        rssi = -int(rssi) if rssi else None  # 0 means "no signal value"; dBm is negative
        clients.append(Client(
            key=mac or str(key),
            label_parts=tuple(n for n in (clean_text(v.get("nickName")), clean_text(v.get("name"))) if n),
            vendor=clean_text(v.get("vendor")),
            kind=_client_kind(v),
            ip=clean_text(v.get("ip")),
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


def _cfg_nodes(text: str) -> object:
    """get_cfg_clientlist() of an appGet body, parsed (None when it is not there): what the two mesh parsers read."""
    body = _json_object(text)
    return body.get("get_cfg_clientlist") if body else None


def _parse_mesh_names(text: str, nodes: object = None) -> Dict[str, str]:
    """Node id -> label from an appGet body containing get_cfg_clientlist(): the node's alias, else its model name.
    Every MAC a node is known by (its own and its radios') maps to it, because the clients name the MAC of the radio
    they are on. Field by field across the nodes, so a node's own MAC is never taken by another node's radio field.
    Empty if the answer has no nodes. `nodes`: the body's _cfg_nodes, when already parsed."""
    nodes = _cfg_nodes(text) if nodes is None else nodes
    labeled = []
    for node in nodes[:MAX_MESH_NODES] if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        alias = clean_text(node.get("alias"))
        label = alias if alias and normal_mac(alias) is None else clean_text(node.get("model_name"))
        if label:
            labeled.append((node, label))
    names: Dict[str, str] = {}
    for field in _MAC_NODE_FIELDS:
        for node, label in labeled:
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


def _node_info(node: dict, wired_neighbors: set) -> _NodeInfo:
    """One node of get_cfg_clientlist(). `wired_neighbors` holds the ids listed in any node's `wired_mac`: the main
    router lists the satellites it reaches by cable, so a node found there with no parent radio is on a cable.
    Whether a satellite on Wi-Fi fills pap*/rssi* was not seen on the firmware checked (every pap* was empty on a
    AiMesh network whose satellites are wired); that part follows the field names only."""
    online = {"1": True, "0": False}.get(str(node.get("online", "")).strip())
    nid = _mac_id(node.get("mac"))
    alias = clean_text(node.get("alias"))
    label = alias if alias and normal_mac(alias) is None else clean_text(node.get("model_name"))
    for pap, rssi, band in _BACKHAUL_RADIOS:
        if _mac_id(node.get(pap)):
            raw = _num(str(node.get(rssi, "")).lstrip("-"))
            return _NodeInfo(nid, label, online, "wifi", band, -int(raw) if raw else None)
    return _NodeInfo(nid, label, online, "wired" if nid in wired_neighbors else None, None, None)


def _parse_mesh_nodes(text: str, nodes: object = None) -> Tuple[List[_NodeInfo], Dict[str, _NodeInfo]]:
    """(the nodes in the router's order, node id -> its info for every MAC the node is known by) from an appGet body
    containing get_cfg_clientlist(). The MACs map field by field exactly as _parse_mesh_names does. `nodes`: the
    body's _cfg_nodes, when already parsed."""
    nodes = _cfg_nodes(text) if nodes is None else nodes
    raw = [n for n in (nodes if isinstance(nodes, list) else []) if isinstance(n, dict)][:MAX_MESH_NODES]
    neighbors = set()
    for node in raw:
        wired = node.get("wired_mac")
        neighbors.update(i for i in (_mac_id(m) for m in (wired if isinstance(wired, list) else [])) if i)
    infos = [_node_info(n, neighbors) for n in raw]
    by_id: Dict[str, _NodeInfo] = {}
    for field in _MAC_NODE_FIELDS:
        for node, info in zip(raw, infos):
            nid = _mac_id(node.get(field))
            if nid:
                by_id.setdefault(nid, info)
    return infos, by_id


def build_mesh(clients: ClientsReading, names: Dict[str, str], overrides: Dict[str, str],
               infos: Sequence[_NodeInfo] = (), info_by_id: Optional[Dict[str, _NodeInfo]] = None) -> Optional[MeshReading]:
    """The mesh nodes the online clients are on, labeled by `overrides` (the options' mesh_node_labels), else by the
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
    if len(counts) > MAX_MESH_NODES:  # (a broken answer: keep the nodes that carry the most clients)
        counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1][0], kv[0]))[:MAX_MESH_NODES])
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

