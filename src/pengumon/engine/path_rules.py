"""Rules about the probe path as a whole, decided on the server once and read everywhere else.

Hops that ignore ping: a hop that gives no reply although a later stage of the path answers. The probes to that later
stage pass through it, so it is up and only ignores (or rate-limits) ping, as many provider routers do. It is shown as
"Ignores ping", not as down, and left out of the loss figures (the dashboard reads the flag from the live targets),
the "when it breaks" grid, the dossier's failing-hop finding and an incident's affected targets. A later stage: a later
ISP hop for an ISP hop, else any later category (two local or gateway targets need not be in line, so they never vouch
for each other). DNS lookups and custom targets are no stage of the path. "No reply" is the prober's status "down".
An IPv6 target this host cannot probe now (TargetStats.ipv6_unavailable: no IPv6 here) is no stage either: it has no
fresh verdict to give or to vouch with.

The rule judges the targets as they are now (their last ten probes); views over a longer time apply the current
verdict to all of it. It never decides whether an incident opens: the classifier's verdicts do not read it.

The hops' role words (`hop_roles`): what the dashboard (`short_label` of /api/targets and the live stream) and the AI
pack call each hop when they do not use its configured name.
"""

from typing import Dict, Iterable, List, Set, Tuple

PATH_RANK = {"local": 0, "gateway": 1, "isp": 2, "public": 3}
_ROLE_WORD = {"local": "Router", "gateway": "Gateway", "isp": "ISP hop", "dns": "DNS lookup"}


def hop_roles(hops: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """Target id -> its role words, from (id, category) pairs in path order (the switched-on hops only): Router,
    Gateway, ISP hop n, Internet, DNS lookup, Other hop n. A number only where a category has several hops (an ISP hop
    and a custom one always have one). Never a name or an address: a custom hop's configured name is free text."""
    by_cat: Dict[str, List[str]] = {}
    for hop_id, category in hops:
        by_cat.setdefault(category, []).append(hop_id)
    out: Dict[str, str] = {}
    for cat, group in by_cat.items():
        many = len(group) > 1
        for n, hop_id in enumerate(group, 1):
            if cat == "public":
                out[hop_id] = f"Internet {n}" if many else "Internet"
            elif cat == "isp":
                out[hop_id] = f"ISP hop {n}"
            elif cat == "custom":
                out[hop_id] = f"Other hop {n}"
            else:
                out[hop_id] = f"{_ROLE_WORD[cat]} {n}" if many else _ROLE_WORD[cat]
    return out


def ignoring_ping(targets: Iterable) -> Set[str]:
    """The ids of the targets that ignore ping, from targets in path order with target_id, category, status and
    enabled (the prober's TargetStats)."""
    path = [t for t in targets if t.enabled and t.category in PATH_RANK and not getattr(t, "ipv6_unavailable", False)]
    out = set()
    for i, t in enumerate(path):
        if t.status != "down":
            continue
        rank = PATH_RANK[t.category]
        later = [o for k, o in enumerate(path)
                 if PATH_RANK[o.category] > rank or (t.category == "isp" and o.category == "isp" and k > i)]
        if any(o.status != "down" for o in later):
            out.add(t.target_id)
    return out


def ignoring_ping_ids(prober) -> Set[str]:
    """The targets the prober has flagged as ignoring ping (none without a prober)."""
    if prober is None:
        return set()
    return {tid for tid, st in prober.stats.items() if st.ignores_ping}
