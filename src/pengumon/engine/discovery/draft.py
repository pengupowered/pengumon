"""The draft: discovery's guesses as hops of the path contract (docs/discovery.md), and the diff against the live path.

Ids are stable for an address: a new hop's id is `hop_` and its address (`hop_203_0_113_9`), the public servers keep
the dashboard's default ids (cloudflare_dns, google_dns, quad9_dns; over IPv6 cloudflare_dns_v6, google_dns_v6,
quad9_dns_v6, in the "internet_v6" group, proposed only when this host has IPv6). A hop of the live path at the same address keeps
its id (all history is keyed by it), and what the owner set on it: its name (unless it is one of the dashboard's
default names), colour, device, "ignores ping" choice, enabled state, group and every other field; discovery
proposes only its role and its place. Its status is "same" when both are unchanged, else "changed". A hop discovery
did not find is "new". A live hop of the path (local, gateway, isp or public) that discovery did not see is "gone",
listed after the hop it followed, except a home hop (local, gateway) that still answers a ping: a device in the home
can be off the traced path (a cable modem in bridge mode), so it stays ("same") where it was; live DNS and custom targets are not on the traced path and are carried as they are
("same"), after the path. A hop removed from the path earlier (its history kept, hidden) is not proposed again unless
discovery finds its address: then it is "new" with its old id and the owner's fields, so applying it brings its
history back; no new hop ever takes a removed hop's id.
"""

import ipaddress
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pengumon.engine.discovery.infer import HIGH, UNKNOWN, Evidence, HopGuess
from pengumon.engine.discovery.traces import (
    INTERNET_GROUP, INTERNET_V6_GROUP, PUBLIC_SERVERS, PUBLIC_SERVERS_V6, PublicServer,
)

MAX_HOPS = 64
PATH_CATEGORIES = ("local", "gateway", "isp", "public")
HOME_CATEGORIES = ("local", "gateway")
DRAFT_ONLY = ("evidence", "confidence", "suggested_driver", "status")
OWNER_FIELDS = ("name", "enabled", "group", "ignores_ping", "color", "device_id")


def address_key(address: Optional[str]) -> str:
    """An address compared by value (2001:DB8::1 is 2001:db8::1); a host name compared without case."""
    try:
        return str(ipaddress.ip_address((address or "").strip()))
    except ValueError:
        return (address or "").strip().lower().rstrip(".")


def hop_id(address: str) -> str:
    return ("hop_" + re.sub(r"[^A-Za-z0-9]", "_", address_key(address)))[:64]


def _default_names(address: str) -> set:
    """The names the dashboard gives its default targets (engine/netinfo.default_targets): not the owner's own."""
    return {f"Local router ({address})", "Public: Cloudflare (1.1.1.1)", "Public: Google (8.8.8.8)"}


def name_hops(guesses: Sequence[HopGuess]) -> None:
    """Plain default names: Router, Gateway, Provider hop 2 (example.net), Internet hop 1 (example.org)."""
    counts: Dict[str, int] = {}
    for g in guesses:
        if g.name:
            continue
        key = g.category if g.owner is not None else "unknown"
        counts[key] = counts.get(key, 0) + 1
        n = counts[key]
        if key == "local":
            g.name = "Router" if n == 1 else f"Router {n}"
        elif key == "gateway":
            g.name = "Gateway" if n == 1 else f"Gateway {n}"
        elif key == "isp":
            g.name = f"Provider hop {n}"
        elif key == "public":
            g.name = f"Internet hop {n}"
        else:
            g.name = f"Hop {g.ttl}" if g.ttl else f"Unknown hop {n}"
        if g.operator and key in ("isp", "public"):
            g.name = f"{g.name} ({g.operator[:60]})"


def _guess_hop(g: HopGuess) -> dict:
    return {
        "id": hop_id(g.address), "name": g.name[:100], "address": g.address, "category": g.category, "enabled": True,
        "family": 6 if ":" in g.address else 4, "group": None, "ignores_ping": None, "color": None, "device_id": None,
        "evidence": [e.public() for e in g.evidence], "confidence": g.confidence,
        "suggested_driver": g.suggested_driver, "status": "new",
    }


def _server_hop(server: PublicServer, reached: Optional[bool]) -> dict:
    text = f"One of the three public servers discovery traces to ({server.name}), proposed with the others as one " \
           "“Internet” node."
    if reached is False:
        text += " It did not answer its trace this time."
    return {
        "id": server.id, "name": server.name, "address": server.address, "category": "public", "enabled": True,
        "family": 4, "group": INTERNET_GROUP, "ignores_ping": None, "color": None, "device_id": None,
        "evidence": [Evidence("address_class", text).public()], "confidence": HIGH,
        "suggested_driver": None, "status": "new",
    }


def _server_hop_v6(server: PublicServer, trace_hops: Optional[int], reached: bool) -> dict:
    text = f"This host has IPv6: {server.name.replace(' (IPv6)', '')}'s resolver over IPv6, proposed with the other " \
           "two as one \u201cInternet (IPv6)\u201d node."
    if server is PUBLIC_SERVERS_V6[0]:  # the one the IPv6 trace went to
        if reached:
            text += f" The IPv6 trace reached it in {trace_hops} hops."
        else:
            text += " The IPv6 trace did not reach it this time."
    return {
        "id": server.id, "name": server.name, "address": server.address, "category": "public", "enabled": True,
        "family": 6, "group": INTERNET_V6_GROUP, "ignores_ping": None, "color": None, "device_id": None,
        "evidence": [Evidence("address_class", text).public()], "confidence": HIGH,
        "suggested_driver": None, "status": "new",
    }


def proposed_hops(guesses: Sequence[HopGuess], reached: Optional[Dict[str, bool]] = None,
                  ipv6: Optional[Tuple[Optional[int], bool]] = None) -> List[dict]:
    """The discovered hops and the public servers, in path order, before the diff. `ipv6` is None when this host
    has no IPv6 (no IPv6 group), else (hops of the IPv6 trace or None when it got nothing, whether it reached
    the first server)."""
    name_hops(guesses)
    hops = [_guess_hop(g) for g in guesses]
    taken = {address_key(h["address"]) for h in hops}
    for server in PUBLIC_SERVERS:
        if address_key(server.address) in taken:
            # the server answered as a hop of another server's path (rare): it stays one hop, in the group
            hop = next(h for h in hops if address_key(h["address"]) == address_key(server.address))
            hop.update(id=server.id, name=server.name, category="public", group=INTERNET_GROUP)
            continue
        hops.append(_server_hop(server, (reached or {}).get(server.address)))
    if ipv6 is not None:
        hops += [_server_hop_v6(server, *ipv6) for server in PUBLIC_SERVERS_V6
                 if address_key(server.address) not in taken]
    return hops


def _rank(keys: List[str], among: set) -> Dict[str, int]:
    ordered = [k for k in keys if k in among]
    return {k: n for n, k in enumerate(ordered)}


def missing_home_hops(proposed: Sequence[dict], live: Sequence[dict]) -> List[str]:
    """The addresses of live home hops (local, gateway) that discovery did not find: worth a ping before calling them
    gone, since a device in the home can be off the traced path (a modem in bridge mode)."""
    found = {address_key(h["address"]) for h in proposed}
    return [h["address"] for h in live if h.get("category") in HOME_CATEGORIES and h.get("address")
            and address_key(h["address"]) not in found]


def diff_against(proposed: List[dict], live: Sequence[dict], answering: Iterable[str] = (),
                 removed: Sequence[dict] = ()) -> List[dict]:
    """The draft: `proposed` (in order) matched to the live path (GET /path hops) by address, with statuses and
    positions 0..n-1 (see the module docstring). A live home hop in `answering` (addresses that answered a ping) is
    kept as "same" where it was, though the trace did not see it. `removed`: hops removed from the path (history
    kept); one found again comes back with its id."""
    answering_keys = {address_key(a) for a in answering}
    live_sorted = sorted(live, key=lambda h: (h.get("position") is None, h.get("position") or 0))
    live_by_key = {}
    for h in live_sorted:
        live_by_key.setdefault(address_key(h.get("address")), h)
    proposed_keys = [address_key(h["address"]) for h in proposed]
    matched = set(proposed_keys) & set(live_by_key)
    live_rank = _rank([address_key(h.get("address")) for h in live_sorted], matched)
    draft_rank = _rank(proposed_keys, matched)

    used_ids = {h.get("id") for h in live_sorted} | {h.get("id") for h in removed}
    removed_by_key: Dict[str, dict] = {}
    for h in removed:
        if address_key(h.get("address")) not in live_by_key:
            removed_by_key.setdefault(address_key(h.get("address")), h)
    draft: List[dict] = []
    for hop, key in zip(proposed, proposed_keys):
        hop = dict(hop)
        old = live_by_key.get(key)
        back = removed_by_key.pop(key, None) if old is None else None
        if back is not None:  # removed earlier, found again: its id (and so its history) and the owner's fields
            merged = {k: v for k, v in back.items() if k not in DRAFT_ONLY and k != "position"}
            merged.update(category=hop["category"], evidence=hop["evidence"], confidence=hop["confidence"],
                          suggested_driver=hop["suggested_driver"], status="new")
            if back.get("name") in _default_names(back.get("address", "")) or not back.get("name"):
                merged["name"] = hop["name"]
            for k in OWNER_FIELDS:
                merged.setdefault(k, hop.get(k))
            draft.append(merged)
            continue
        if old is None:
            new_id, n = hop["id"], 2
            while new_id in used_ids:
                new_id, n = f"{hop['id'][:60]}_{n}", n + 1
            hop["id"] = new_id
            used_ids.add(new_id)
            draft.append(hop)
            continue
        changed = old.get("category") != hop["category"] or live_rank[key] != draft_rank[key]
        merged = {k: v for k, v in old.items() if k not in DRAFT_ONLY and k != "position"}
        merged.update(category=hop["category"], evidence=hop["evidence"], confidence=hop["confidence"],
                      suggested_driver=hop["suggested_driver"], status="changed" if changed else "same")
        if old.get("name") in _default_names(old.get("address", "")) or not old.get("name"):
            merged["name"] = hop["name"]
        for k in OWNER_FIELDS:
            merged.setdefault(k, hop.get(k))
        if merged.get("group") is None and hop.get("group"):
            merged["group"] = hop["group"]
        draft.append(merged)

    # live path hops discovery did not see: after the hop they followed
    previous: Optional[str] = None
    gone_after: Dict[Optional[str], List[dict]] = {}
    others: List[dict] = []
    for h in live_sorted:
        key = address_key(h.get("address"))
        if key in matched:
            previous = key
            continue
        hop = {k: v for k, v in h.items() if k not in DRAFT_ONLY and k != "position"}
        for k in OWNER_FIELDS:
            hop.setdefault(k, None)
        if h.get("category") in HOME_CATEGORIES and key in answering_keys:
            hop.update(evidence=[], confidence=UNKNOWN, suggested_driver=None, status="same")
            gone_after.setdefault(previous, []).append(hop)
        elif h.get("category") in PATH_CATEGORIES:
            hop.update(evidence=[], confidence=UNKNOWN, suggested_driver=None, status="gone")
            gone_after.setdefault(previous, []).append(hop)
        else:
            hop.update(evidence=[], confidence=UNKNOWN, suggested_driver=None, status="same")
            others.append(hop)
    out: List[dict] = list(gone_after.get(None, []))
    for hop in draft:
        out.append(hop)
        if address_key(hop["address"]) in matched:
            out.extend(gone_after.get(address_key(hop["address"]), []))
    out.extend(others)
    for position, hop in enumerate(out):
        hop["position"] = position
    return out


def trim(draft: List[dict], limit: int = MAX_HOPS) -> List[dict]:
    """At most `limit` hops: the internet hops nearest the servers go first (never a server, a live or a home hop)."""
    out = list(draft)
    while len(out) > limit:
        removable = [k for k, h in enumerate(out) if h["status"] == "new" and h["category"] == "public"
                     and h.get("group") is None]
        if not removable:
            break
        del out[removable[-1]]
    for position, hop in enumerate(out):
        hop["position"] = position
    return out
