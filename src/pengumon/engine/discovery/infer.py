"""Who operates each hop: the evidence, and the owner (home / provider / internet) and role it adds up to.

No assumption about the equipment: an ISP's all-in-one router is one home hop, a router and a modem two, a mesh
satellite one more; carrier-grade NAT may or may not be there. The rules, in the order they decide:

1. Where your network ends (the edge). Start from the leading run of private hops (private before the first public
   hop). The latency step then moves the edge earlier when the largest significant jump in round-trip time between
   consecutive hops of the home-to-provider region (up to the first public hop) lies inside that run: the hops past
   the jump are behind the access line, private addresses or not (a satellite ground station, a cellular core). A
   hop past the jump that serves a web page (or answers SNMP) is a device in the home and stays in it. Strongest
   of all, a UPnP gateway on the path: with a public internet address it is the edge; with a private one another
   NAT follows (the next private hop is in the home too); with a CGNAT one the provider's NAT comes next.
2. Past the edge, each hop is the provider's or the internet's, by the strongest evidence it has: its AS (the first
   public hop's AS is the provider's), else its reverse-DNS domain (the provider's domain is the first one seen past
   the edge, preferring a hop in the provider's AS), else the divergence point (hops all three traces share are on
   your connection's own way out; past the split they are out in the internet), else its address (a CGNAT or
   private address past the edge, or the first public hop, is the provider's). Once the path has left the provider
   it does not come back: a later hop that looks like the provider's is kept as internet, with lower confidence.
3. A hop with no evidence of its own takes its neighbors' owner when the hops either side agree, else stays
   unknown (and takes the earlier one's role).

Confidence: high when two or more kinds of evidence agree and none disagrees; likely with one kind, or when some
evidence disagrees with the stronger kind that decided; unknown with none.

Roles (category): the first home hop is "local" (Router); a later home hop at the edge is "gateway" when it is on
another network than the hop before it (double NAT), or its page says it is an ISP gateway. Provider hops are "isp",
internet hops "public".
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pengumon.engine.discovery.addresses import (
    CGNAT, CLASS_WORDS, LINK_LOCAL, PRIVATE, PUBLIC, SPECIAL, address_class, registrable_domain, same_subnet,
)
from pengumon.engine.discovery.devices import CABLE_MODEM_ADDRESS, PageInfo
from pengumon.engine.discovery.lookups import AsnInfo
from pengumon.engine.discovery.traces import MergedPath
from pengumon.engine.discovery.upnp import IgdInfo

HOME, PROVIDER, INTERNET = "home", "provider", "internet"
OWNER_CATEGORY = {PROVIDER: "isp", INTERNET: "public"}
HIGH, LIKELY, UNKNOWN = "high", "likely", "unknown"

STEP_MIN_MS = 1.0  # a latency step smaller than this is noise
STEP_RATIO = 1.5  # ... and so is one where the later hop is not at least this much slower

# Which kind of evidence decides a provider-or-internet hop, strongest first.
_PRIORITY = ("upnp", "asn", "rdns", "divergence", "address_class", "latency_step", "device_page", "off_path",
             "neighbors")


@dataclass
class Evidence:
    kind: str
    text: str
    supports: Optional[str] = None  # the owner it speaks for, or None (information only)

    def public(self) -> dict:
        return {"kind": self.kind, "text": self.text}


@dataclass
class HopGuess:
    address: str
    ttl: Optional[int]
    rtt_ms: Optional[float]
    owner: Optional[str] = None
    category: str = "custom"
    confidence: str = UNKNOWN
    evidence: List[Evidence] = field(default_factory=list)
    name: str = ""
    suggested_driver: Optional[str] = None
    operator: Optional[str] = None  # the provider's or the hop's operator (domain or AS name), for its name


OFF_PATH_TEXT = ("It answers on your network but isn't on the traced path (a modem in bridge mode, or an ISP "
                 "gateway).")


@dataclass
class OffPathDevice:
    """A common gateway address (engine.GATEWAY_ADDRESSES) that is not a hop of the traced path and answered."""

    address: str
    page: Optional[PageInfo] = None  # its front page, when it served one
    answered_ping: bool = False


@dataclass
class Facts:
    """Everything discovery learned, for `infer`."""

    path: MergedPath
    names: Dict[str, str] = field(default_factory=dict)  # address -> reverse-DNS name
    asns: Dict[str, AsnInfo] = field(default_factory=dict)
    pages: Dict[str, PageInfo] = field(default_factory=dict)  # address -> its web page
    snmp: Dict[str, str] = field(default_factory=dict)  # address -> sysDescr
    igds: List[IgdInfo] = field(default_factory=list)
    gateway: Optional[str] = None  # this host's default gateway (route table), when known


@dataclass
class Step:
    before: int  # index of the last hop before the jump
    after: int  # index of the first hop past it
    ms: float


@dataclass
class Inference:
    hops: List[HopGuess]  # answered hops, in path order (a gateway found off the path is placed at the edge)
    edge: int  # index in the path of the last home hop (-1: none)
    step: Optional[Step]
    silent: List[int]  # path indexes of hops that did not answer
    silent_owners: Dict[int, Optional[str]]
    notes: List[str] = field(default_factory=list)


def latency_step(path: MergedPath, region_end: int) -> Optional[Step]:
    """The largest significant rise in round-trip time between consecutive answering hops up to `region_end`."""
    timed = [i for i, h in enumerate(path.hops) if h.address and h.rtt_ms is not None and i <= region_end]
    best: Optional[Step] = None
    for a, b in zip(timed, timed[1:]):
        rise = path.hops[b].rtt_ms - path.hops[a].rtt_ms
        if rise < STEP_MIN_MS or path.hops[b].rtt_ms < STEP_RATIO * max(path.hops[a].rtt_ms, 0.0):
            continue
        if best is None or rise > best.ms:
            best = Step(a, b, round(rise, 1))
    return best


def _confidence(evidence: Sequence[Evidence], owner: Optional[str]) -> str:
    if owner is None:
        return UNKNOWN
    support = {e.kind for e in evidence if e.supports == owner}
    against = {e.kind for e in evidence if e.supports not in (None, owner)}
    if not support:
        return UNKNOWN
    if against or len(support) < 2:
        return LIKELY
    return HIGH


def _decide(evidence: Sequence[Evidence]) -> Optional[str]:
    for kind in _PRIORITY:
        for e in evidence:
            if e.kind == kind and e.supports is not None:
                return e.supports
    return None


def infer(facts: Facts, off_path: Sequence[OffPathDevice] = ()) -> Inference:
    path = facts.path
    hops = path.hops
    cls = [address_class(h.address) if h.address else None for h in hops]
    answered = [i for i, h in enumerate(hops) if h.address]
    silent = [i for i, h in enumerate(hops) if not h.address]
    first_public = next((i for i in answered if cls[i] == PUBLIC), None)
    igd_at = {igd.address: igd for igd in facts.igds}

    def home_device(i: int) -> bool:
        a = hops[i].address
        return a in facts.pages or a in facts.snmp or a in igd_at

    # 1. the edge of the home network
    lead_end = -1
    for i in answered:
        if cls[i] not in (PRIVATE, LINK_LOCAL):
            break
        lead_end = i
    region_end = first_public if first_public is not None else (answered[-1] if answered else -1)
    step = latency_step(path, region_end)
    edge = lead_end
    if step is not None and step.before < lead_end:
        devices_past = [i for i in answered if step.after <= i <= lead_end and home_device(i)]
        edge = max(devices_past) if devices_past else step.before
    for i in answered:
        igd = igd_at.get(hops[i].address)
        if igd is None or i > lead_end:
            continue
        ext = igd.external_class
        if ext == PUBLIC or ext == CGNAT:
            edge = i
        elif ext in (PRIVATE, LINK_LOCAL):
            following = [k for k in answered if i < k <= lead_end]
            edge = max(edge, following[0] if following else i)

    guesses: Dict[int, HopGuess] = {}
    for i in answered:
        h = hops[i]
        guesses[i] = HopGuess(address=h.address, ttl=h.ttl, rtt_ms=h.rtt_ms)

    # evidence every hop can have: its name, its device checks
    for i, g in guesses.items():
        name = facts.names.get(g.address)
        if name and i <= edge:
            g.evidence.append(Evidence("rdns", f"Its name is {name}."))
        page = facts.pages.get(g.address)
        if page is not None:
            g.evidence.append(Evidence("device_page", f"It {page.describe()}.", HOME if i <= edge else None))
            if page.fingerprint:
                g.suggested_driver = page.fingerprint.driver
        descr = facts.snmp.get(g.address)
        if descr:
            g.evidence.append(Evidence("device_page", f"Its SNMP agent describes it as “{descr}”.",
                                       HOME if i <= edge else None))
            g.suggested_driver = g.suggested_driver or "snmp"
        igd = igd_at.get(g.address)
        if igd is not None:
            g.evidence.append(Evidence("upnp", f"It {igd.describe()}.", HOME if i <= edge else None))
            g.suggested_driver = g.suggested_driver or "upnp_igd"

    if step is not None:
        if step.before in guesses:
            guesses[step.before].evidence.append(Evidence(
                "latency_step", f"The largest jump in round-trip time on the way out (+{step.ms:g} ms) comes right "
                                f"after it: the access line is likely there.", HOME if step.before <= edge else None))
        if step.after in guesses:
            guesses[step.after].evidence.append(Evidence(
                "latency_step", f"It comes right after the largest jump in round-trip time on the way out "
                                f"(+{step.ms:g} ms): past the access line.", PROVIDER if step.after > edge else None))

    # home hops
    home = [i for i in answered if i <= edge]
    for n, i in enumerate(home):
        g = guesses[i]
        g.owner = HOME
        words = CLASS_WORDS.get(cls[i], "an address")
        text = f"It has {words}, before the first public hop: inside your network"
        if facts.gateway and g.address == facts.gateway:
            text += ", and it is this host's default gateway"
        g.evidence.insert(0, Evidence("address_class", text + ".", HOME))  # home hops are private (edge <= lead_end)
        prev = guesses[home[n - 1]] if n else None
        ext_private_before = prev is not None and prev.address in igd_at and \
            igd_at[prev.address].external_class in (PRIVATE, LINK_LOCAL)
        isp_box = g.suggested_driver == "xfinity_xb"
        if n == 0 and not (isp_box and len(home) == 1):
            g.category = "local"
        elif i == home[-1] and (isp_box or ext_private_before or (prev and not same_subnet(prev.address, g.address))):
            g.category = "gateway"
            if prev and not same_subnet(prev.address, g.address):
                g.evidence.append(Evidence("address_class", "It is on another network than the hop before it: a "
                                                            "second router between yours and the provider (double "
                                                            "NAT), such as the provider's gateway.", HOME))
        else:
            g.category = "local"

    # past the edge: provider or internet
    past = [i for i in answered if i > edge]
    provider_asn = None
    if first_public is not None and hops[first_public].address in facts.asns:
        provider_asn = facts.asns[hops[first_public].address]
    provider_domain = None
    for i in past:
        domain = registrable_domain(facts.names.get(hops[i].address))
        if domain is None:
            continue
        asn = facts.asns.get(hops[i].address)
        if provider_asn is not None and asn is not None:
            if asn.number == provider_asn.number:
                provider_domain = domain
                break
            continue
        if path.divergence is None or path.divergence < 0 or i <= path.divergence:
            provider_domain = domain
            break
    split_known = path.divergence is not None
    left_provider = False
    for i in past:
        g = guesses[i]
        address = g.address
        c = cls[i]
        ev: List[Evidence] = []
        if c == CGNAT:
            ev.append(Evidence("address_class", "It has a carrier-grade NAT address (100.64.0.0/10), which providers "
                                                "use inside their network: the provider's.", PROVIDER))
        elif c in (PRIVATE, LINK_LOCAL):
            ev.append(Evidence("address_class", f"It has {CLASS_WORDS[c]}, but it is past the end of your network "
                                                "and before the first public hop: inside the provider's network "
                                                "(providers use private addresses too).", PROVIDER))
        elif c == SPECIAL:
            ev.append(Evidence("address_class", f"It has {CLASS_WORDS[c]}, which no home network uses."))
        elif i == first_public:
            ev.append(Evidence("address_class", "It is the first public address on the path: usually the provider's "
                                                "first router.", PROVIDER))
        else:
            ev.append(Evidence("address_class", "It has a public address."))
        asn = facts.asns.get(address)
        if asn is not None:
            label = f"AS{asn.number}" + (f" ({asn.operator})" if asn.operator else "")
            if i == first_public:
                ev.append(Evidence("asn", f"It is in {label}: the provider's network (the first public hop's).",
                                   PROVIDER))
            elif provider_asn is not None and asn.number == provider_asn.number:
                ev.append(Evidence("asn", f"It is in {label}, the same network as the first public hop: the "
                                          "provider's.", PROVIDER))
            elif provider_asn is not None:
                ev.append(Evidence("asn", f"It is in {label}, not the provider's AS{provider_asn.number}: another "
                                          "network.", INTERNET))
            else:
                ev.append(Evidence("asn", f"It is in {label}."))
        name = facts.names.get(address)
        if name:
            domain = registrable_domain(name)
            if domain and provider_domain and domain == provider_domain:
                ev.append(Evidence("rdns", f"Its name, {name}, is in {domain}, the provider's domain.", PROVIDER))
            elif domain and provider_domain:
                ev.append(Evidence("rdns", f"Its name, {name}, is in {domain}, not the provider's {provider_domain}: "
                                           "another network.", INTERNET))
            else:
                ev.append(Evidence("rdns", f"Its name is {name}."))
        if split_known:
            if i == path.divergence:
                ev.append(Evidence("divergence", "The paths to the three public servers part right after it; it is "
                                                 "on the way to all of them: your connection's own way out.", PROVIDER))
            elif path.divergence is not None and i < path.divergence:
                ev.append(Evidence("divergence", "It is on the way to all three public servers, before their paths "
                                                 "part: your connection's own way out.", PROVIDER))
            else:
                ev.append(Evidence("divergence", "It is past the point where the paths to the three public servers "
                                                 "part: out in the internet.", INTERNET))
        g.evidence.extend(ev)
        owner = _decide(g.evidence)
        if left_provider and owner == PROVIDER:
            owner = INTERNET
            g.evidence.append(Evidence("neighbors", "An earlier hop is already outside the provider's network, so "
                                                     "this one is taken as outside it too.", INTERNET))
        g.owner = owner
        if owner == INTERNET:
            left_provider = True
        if provider_domain and owner == PROVIDER:
            g.operator = provider_domain
        elif owner == PROVIDER and provider_asn is not None:
            g.operator = provider_asn.operator
        elif owner == INTERNET:
            g.operator = registrable_domain(name) or (asn.operator if asn else None)

    # 3. neighbors: hops with no owner, and the silent ones
    def owner_at(k: int) -> Optional[str]:
        return guesses[k].owner if k in guesses else None

    order = list(range(len(hops)))
    silent_owners: Dict[int, Optional[str]] = {}
    for i in order:
        if i in guesses and guesses[i].owner is not None:
            continue
        before = next((owner_at(k) for k in reversed(order[:i]) if owner_at(k) is not None), None)
        after = next((owner_at(k) for k in order[i + 1:] if owner_at(k) is not None), None)
        agreed = before if before is not None and before == after else None
        if i not in guesses:
            silent_owners[i] = agreed
            continue
        g = guesses[i]
        if agreed is not None:
            g.owner = agreed
            g.evidence.append(Evidence("neighbors", "It has no evidence of its own; the hops either side are both "
                                                     f"{_owner_words(agreed)}, so it likely is too.", agreed))
        else:
            g.evidence.append(Evidence("neighbors", "It has no evidence of its own, and the hops either side do "
                                                     "not agree: unknown."))
            g.owner = None
            g.category = OWNER_CATEGORY.get(before or PROVIDER, "local")

    for i, g in guesses.items():
        if g.owner == HOME:
            pass
        elif g.owner is not None:
            g.category = OWNER_CATEGORY[g.owner]
        g.confidence = _confidence(g.evidence, g.owner)

    result = [guesses[i] for i in answered]
    notes = []
    # gateways off the path (a modem in bridge mode never shows in a trace): at the edge, after your network's hops
    at = sum(1 for g in result if g.owner == HOME)
    on_path = {g.address for g in result}
    for device in off_path:
        if device.address in on_path:
            continue
        page = device.page
        guess = HopGuess(address=device.address, ttl=None, rtt_ms=None, owner=HOME, category="gateway",
                         name="Cable modem" if device.address == CABLE_MODEM_ADDRESS else "",
                         suggested_driver=page.fingerprint.driver if page is not None and page.fingerprint else None)
        if page is not None:
            where = (", where cable modems serve their status page" if device.address == CABLE_MODEM_ADDRESS
                     else "")
            guess.evidence.append(Evidence("device_page", f"Something {page.describe()} at {device.address}{where}.",
                                           HOME))
        if device.answered_ping:
            guess.evidence.append(Evidence("off_path", f"It answers ping at {device.address}."))
        guess.evidence.append(Evidence("off_path", OFF_PATH_TEXT, HOME))
        guess.confidence = LIKELY
        result.insert(at, guess)
        at += 1
        on_path.add(device.address)
    if silent:
        notes.append(f"{len(silent)} hop{'s' if len(silent) != 1 else ''} on the path did not answer the trace; "
                     "a hop without an address cannot be probed, so it is not in the draft.")
    return Inference(hops=result, edge=edge, step=step, silent=silent, silent_owners=silent_owners, notes=notes)


def _owner_words(owner: str) -> str:
    return {HOME: "in your network", PROVIDER: "the provider's", INTERNET: "out in the internet"}[owner]
