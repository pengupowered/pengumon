"""Rules about the probe path as a whole, decided on the server once and read everywhere else.

Hops that ignore ping: a hop that gives no reply although a later stage of the path answers. The probes to that later
stage pass through it, so it is up and only ignores (or rate-limits) ping, as many provider routers do. It is shown as
"Ignores ping", not as down, and left out of the loss figures (the dashboard reads the flag from the live targets),
the "when it breaks" grid, the dossier's failing-hop finding and an incident's affected targets. A later stage: a later
ISP hop for an ISP hop, else any later category (two local or gateway targets need not be in line, so they never vouch
for each other). DNS lookups and custom targets are no stage of the path. "No reply" is the prober's status "down".

The rule judges the targets as they are now (their last ten probes); views over a longer time apply the current
verdict to all of it. It never decides whether an incident opens: the classifier's verdicts do not read it.
"""

from typing import Iterable, Set

PATH_RANK = {"local": 0, "gateway": 1, "isp": 2, "public": 3}


def ignoring_ping(targets: Iterable) -> Set[str]:
    """The ids of the targets that ignore ping, from targets in path order with target_id, category, status and
    enabled (the prober's TargetStats)."""
    path = [t for t in targets if t.enabled and t.category in PATH_RANK]
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
