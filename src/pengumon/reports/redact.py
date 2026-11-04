"""The redactor of the AI troubleshooting pack: one per pack, so a value has one role string everywhere in it.

It is built from the identifiers the pack knows (hop ids, names, addresses and reverse names; the provider; host
apps; the identifiers each module contribution declares) and does four things:

- role strings: a hop is "Router", "Gateway", "ISP hop 2"..., a module role key ("mesh_node:2") is "Mesh node 2";
- resolves the tokens in Pengumon's own sentences: `{hop:<target id>}` and `{role:<key>}`;
- sweeps quoted text (titles, hop names) for the identifiers, in any spelling (case, separators), at Vague and
  Anonymized;
- the final scan over the finished document, at every level: patterns (at Full MACs and passwords in URLs; at Vague
  also home addresses and host names; at Anonymized any address, date, host name, vendor word) and the identifiers
  again. It is a safety net: a hit means a rule upstream missed something, so `Redactor.hits` is what tests assert is
  empty.

Identifiers a pack must never show (raw mesh node ids, anything per client) are swept at every level, Full included,
unless the pack shows that very value at Full (a target the user named after a client). An identifier that is a word
or phrase of Pengumon's own text at this level (`vocabulary`: a device called "modem") is not swept: it names nobody,
and sweeping it would rewrite Pengumon's sentences. Hops the pack knows but does not show (`known_hops`: targets
switched off, earlier versions of the path) are swept like the shown ones, so a title from before a rename is too."""

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

FULL, VAGUE, ANONYMIZED = "full", "vague", "anonymized"
LEVELS = (FULL, VAGUE, ANONYMIZED)
NEVER = "never"          # an identifier map value: this value is shown at no level
REMOVED = "[removed]"
GENERIC = "[redacted]"

# Hops whose category puts them in the home: their names, addresses and reverse names are Full only.
HOME_CATEGORIES = ("local", "gateway", "custom")
_SHORT = {"local": "Router", "gateway": "Gateway", "isp": "ISP hop", "dns": "DNS lookup"}
WHAT_IT_IS = {"local": "home router", "gateway": "modem or gateway", "isp": "provider router",
              "public": "public server", "dns": "name lookup", "custom": "other"}
# Vendor and product words that name the provider or the owner's hardware: removed at Anonymized.
VENDOR_WORDS = ("xfinity", "comcast", "asuswrt", "asus", "aimesh", "xb6", "xb7", "xb8")

# `{hop:<target id>}` and `{role:<key>}` tokens, and the bare role keys the modules write (capabilities.contrib)
_ROLE_KEY = r"(?<![\w:])(?:line_device|router_device|mesh_node:\d+)(?!\w)"
# the generic role word of a hop the pack knows but does not show, by category
_KNOWN_ROLE = {"local": "Router", "gateway": "Gateway", "isp": "ISP hop", "public": "Internet", "dns": "DNS lookup",
               "custom": "Other hop"}
_TOKEN = re.compile(r"(\{(?:hop|role):[^{}]*\}|" + _ROLE_KEY + ")")
_ALNUM = re.compile(r"[A-Za-z0-9]+")


# Not ip.is_private: that also covers the documentation ranges (192.0.2.0/24, 198.51.100.0/24) the tests use for the
# provider's side, and 100.64.0.0/10 (carrier-grade NAT), which is the provider's too.
_HOME_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7", "fe80::/10")]


def is_private_address(value: str) -> bool:
    """A literal address in the home (private, loopback, link-local): never a provider's or the internet's."""
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    return any(ip in net for net in _HOME_NETS if net.version == ip.version)


def _home_name(value: str) -> bool:
    v = value.strip().lower()
    return bool(v) and (is_private_address(v) or "." not in v or v.endswith((".lan", ".local", ".home", ".internal",
                                                                          ".home.arpa", ".localdomain")))


def is_home_hop(hop) -> bool:
    """By category, but also by what the hop points at: a "dns" target on the router's address is a home hop."""
    return hop.category in HOME_CATEGORIES or _home_name(hop.address or "x")


def role_labels(hops: Sequence[Any]) -> Dict[str, str]:
    """Target id -> role string, in path order (the dashboard's shortLabels: a number only where a role has several
    hops). A custom hop is "Other hop n" instead of its configured name, which is free text."""
    by_cat: Dict[str, List[Any]] = {}
    for hop in hops:
        by_cat.setdefault(hop.category, []).append(hop)
    out: Dict[str, str] = {}
    for cat, group in by_cat.items():
        for n, hop in enumerate(group, 1):
            many = len(group) > 1
            if cat == "public":
                out[hop.id] = f"Internet {n}" if many else "Internet"
            elif cat == "isp":
                out[hop.id] = f"ISP hop {n}"
            elif cat == "custom":
                out[hop.id] = f"Other hop {n}"
            else:
                out[hop.id] = f"{_SHORT[cat]} {n}" if many else _SHORT[cat]
    return out


def module_role_word(key: str) -> str:
    """The string a module role key gets at Vague and Anonymized: line_device -> Gateway (the modem or gateway that
    reads the line, the box the Gateway hop probes), mesh_node:2 -> Mesh node 2."""
    if key == "line_device":
        return "Gateway"
    if key == "router_device":
        return "Router"
    name, _, n = key.partition(":")
    word = name.replace("_", " ").strip().capitalize() or "Device"
    return f"{word} {n}".strip()


def _flex(value: str) -> Optional[str]:
    """A regex for the value in any spelling: case-insensitive, any (or no) separators between its letter and digit
    runs, not inside a longer word. None: too short to sweep without eating ordinary words."""
    tokens = _ALNUM.findall(value)
    if sum(map(len, tokens)) < 3:
        return None
    # a value in brackets ("Mesh (192.168.88.1)") takes its brackets along, or they are left behind as debris
    lead = r"(?:[(\[]\s*)?" if value.lstrip()[:1] in "([" else ""
    trail = r"(?:\s*[)\]])?" if value.rstrip()[-1:] in ")]" else ""
    return (r"(?<![A-Za-z0-9])" + lead + r"[\W_]*".join(re.escape(t) for t in tokens) + r"(?![A-Za-z0-9])"
            + trail)


def _norm(value: str) -> str:
    return "".join(_ALNUM.findall(value)).lower()


def _ip6(value: str):
    try:
        ip = ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None
    return ip if ip.version == 6 else None


class _Sweep:
    """Identifier -> replacement, compiled to one pattern (longest value first). `skip`: role words; `vocabulary`:
    Pengumon's own text, whose words and phrases are no identifiers. An IPv6 address is matched in every spelling
    (zero compression, leading zeros, case) by comparing parsed addresses."""

    def __init__(self, pairs: Iterable[Tuple[str, str]], skip: Iterable[str] = (), vocabulary: str = ""):
        skip = {_norm(s) for s in skip}
        seen: Dict[str, Tuple[str, str]] = {}
        self.ips: Dict[Any, str] = {}
        for value, repl in pairs:
            key = _norm(value or "")
            ip = _ip6(value)
            if ip is not None:
                self.ips.setdefault(ip, repl)
            if key and key not in skip and key not in seen and _flex(value) \
                    and not (vocabulary and re.search(_flex(value), vocabulary, re.I)):
                seen[key] = (value, repl)
        ordered = sorted(seen.values(), key=lambda p: -len(_norm(p[0])))
        self.repls = [r for _, r in ordered]
        self.regex = re.compile("|".join(f"(?P<g{i}>{_flex(v)})" for i, (v, _) in enumerate(ordered)), re.I) \
            if ordered else None

    def apply(self, text: str, count) -> str:
        if self.ips:
            def sub6(m):
                repl = self.ips.get(_ip6(m.group(0)))
                if repl is None:
                    return m.group(0)
                count(repl)
                return repl
            text = re.sub(_IP6, sub6, text, flags=re.I)
        if not self.regex:
            return text

        def sub(m):
            repl = self.repls[int(m.lastgroup[1:])]
            count(repl)
            return repl
        text = self.regex.sub(sub, text)
        for repl in set(self.repls):         # a name and its address, both swept: "Line (Line)" says it once
            text = text.replace(f"{repl} ({repl})", repl)
        return text


_IP4 = r"(?<![\d.])\d{1,3}(?P<a>[.\-_])\d{1,3}(?P=a)\d{1,3}(?P=a)\d{1,3}(?![\d])"
_PRIVATE4 = "|".join([
    r"(?<![\d.])(?:10|127)(?P<p1>[.\-_])\d{1,3}(?P=p1)\d{1,3}(?P=p1)\d{1,3}(?![\d])",
    r"(?<![\d.])192(?P<p2>[.\-_])168(?P=p2)\d{1,3}(?P=p2)\d{1,3}(?![\d])",
    r"(?<![\d.])172(?P<p3>[.\-_])(?:1[6-9]|2\d|3[01])(?P=p3)\d{1,3}(?P=p3)\d{1,3}(?![\d])",
    r"(?<![\d.])169(?P<p4>[.\-_])254(?P=p4)\d{1,3}(?P=p4)\d{1,3}(?![\d])"])
_IP6_HOME = r"(?<![\w:])(?:fe80|f[cd][0-9a-f]{2})(?::[0-9a-f]{0,4}){2,7}(?![\w:])"
_IP6 = (r"(?<![\w:])(?:(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){0,6}[0-9a-f]{0,4}::"
        r"(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?)(?![\w:])")
_MAC = r"(?<![0-9a-f:\-])[0-9a-f]{2}(?:[:\-][0-9a-f]{2}){5}(?![0-9a-f:\-])"
_HOME_HOST = r"\b[\w-]+(?:\.[\w-]+)*\.(?:lan|local|home|internal|localdomain|home\.arpa)\b"
_HOST = (r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|us|uk|de|fr|nl|ca|au|edu|gov|info|biz|tv|me|dev|app|"
         r"lan|local|home|internal|arpa)\b")
_USERINFO = r"(?<=://)[^\s/@:]+:[^\s/@]+@"   # a password in a URL: user:password@
_DATE = r"\b\d{4}-\d{2}-\d{2}\b|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|\bUTC[+\-]\d\d:\d\d\b"
_VENDOR = r"\b(?:" + "|".join(VENDOR_WORDS) + r")\b"
# (rule name, pattern, replacement) per level; the identifiers are scanned too, as rule "identifier".
SCAN_RULES: Dict[str, List[Tuple[str, str, str]]] = {
    FULL: [("mac", _MAC, REMOVED), ("secret", _USERINFO, REMOVED)],
    VAGUE: [("secret", _USERINFO, REMOVED), ("home address", _PRIVATE4, REMOVED), ("home address", _IP6_HOME, REMOVED),
            ("mac", _MAC, REMOVED),
            ("home name", _HOME_HOST, REMOVED)],
    ANONYMIZED: [("secret", _USERINFO, REMOVED), ("address", _IP4, REMOVED), ("address", _IP6_HOME, REMOVED),
                 ("address", _IP6, REMOVED),
                 ("mac", _MAC, REMOVED), ("host name", _HOST, REMOVED), ("date", _DATE, REMOVED),
                 ("vendor", _VENDOR, REMOVED)],
}


@dataclass
class Redactor:
    """`hops`: objects with id, category, name, address, rdns. `provider`: the provider's name or "". `apps`: host app
    names. `module_identifiers`: value -> role key (or NEVER) from every active contribution. `module_labels`: role
    key -> the Full string (a device's or mesh node's configured label). `field_values`: value -> (full, vague,
    anonymized) action (keep | generic | drop) for module fields such as the router model. `known_hops`: hops the
    pack does not show but whose names may be in old titles (disabled targets, earlier path versions). `vocabulary`:
    Pengumon's own text at this level (fixed text, prompt texts, notes): an identifier found in it is not swept."""
    level: str
    hops: Sequence[Any] = ()
    provider: str = ""
    apps: Sequence[str] = ()
    module_identifiers: Mapping[str, str] = field(default_factory=dict)
    module_labels: Mapping[str, str] = field(default_factory=dict)
    field_values: Mapping[str, Tuple[str, str, str]] = field(default_factory=dict)
    known_hops: Sequence[Any] = ()
    vocabulary: str = ""
    tz: tzinfo = None
    day_zero: float = 0.0          # Anonymized: "Day 1" is the calendar day of this time
    hits: Dict[str, int] = field(default_factory=dict)       # final scan, by rule: should stay empty
    swept: Dict[str, int] = field(default_factory=dict)      # replacements by the sweep, by kind (for the counts line)
    used_roles: Dict[str, str] = field(default_factory=dict)  # role string -> Full name ("Who is who")

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"unknown detail level {self.level!r}")
        from datetime import timezone
        self.tz = self.tz or timezone.utc
        self._hop_role = role_labels(self.hops)
        self._hop_by_id = {h.id: h for h in self.hops}
        self._apps = {name: f"App {n}" for n, name in enumerate(self.apps, 1)}
        self._sweep = _Sweep(self._sweep_pairs(), skip=self._role_words(), vocabulary=self.vocabulary)

    # -- roles ----------------------------------------------------------------------------------------------------
    def hop(self, target_id: str) -> str:
        """The hop's role string (the same at every level, and the only name Pengumon's sentences use)."""
        role = self._hop_role.get(target_id, "a hop")
        h = self._hop_by_id.get(target_id)
        if h is not None:
            self.used_roles[role] = h.name
        return role

    def app(self, name: str) -> str:
        """A host app: its configured name at Full, "App n" otherwise."""
        role = self._apps.get(name, "App")
        self.used_roles[role] = name
        return name if self.level == FULL else role

    def role(self, key: str, table: bool = False) -> str:
        """A module role key as text. Sentences always get the role word. In a table cell at Full the node's own label
        comes first, with the role word in brackets, so the cell can be matched to a sentence."""
        word = module_role_word(key)
        label = self.module_labels.get(key, "")
        if label:
            self.used_roles[word] = label
        if table and self.level == FULL and label and _norm(label) != _norm(word):
            return f"{label} ({word})"
        return word

    def _role_words(self) -> List[str]:
        words = ["Router", "Gateway", "Line", "Internet", "DNS lookup", "ISP", "the provider", "Other hop"]
        words += list(self._hop_role.values()) + [module_role_word(k) for k in self.module_identifiers.values()]
        words += list(self._apps.values()) + [module_role_word(k) for k in self.module_labels]
        return words

    # -- what is swept --------------------------------------------------------------------------------------------
    def _sweep_pairs(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        level = self.level
        shown = {_norm(v) for h in self.hops for v in (h.name, h.address, getattr(h, "rdns", "")) if v}
        shown |= {_norm(v) for v in self.module_labels.values()}
        for value, key in self.module_identifiers.items():
            if key == NEVER and _norm(value) not in shown:
                pairs.append((value, REMOVED))
        if level == FULL:
            return pairs
        # At Vague the provider's and public hops' addresses and reverse names are shown (and the provider's name): a
        # name or id of such a hop found in them (a hop named after its reverse name, an earlier name that was one) is
        # what the level shows, not an identifier. Home hops, devices, clients and apps are swept whatever they match.
        kept = "\n".join([v for h in self.hops if not is_home_hop(h) for v in (h.address, getattr(h, "rdns", "")) if v]
                         + [self.provider]) if level == VAGUE else ""

        def own(value: str, home: bool, role: str) -> List[Tuple[str, str]]:
            """The pairs of a hop's id and name: none for what Vague shows of a hop outside the home."""
            flex = _flex(value or "")
            return [] if not home and kept and flex and re.search(flex, kept, re.I) else [(value, role)]

        for h in self.hops:
            role = self._hop_role.get(h.id, "a hop")
            home = is_home_hop(h)
            pairs += own(h.id, home, role) + own(h.name, home, role)
            if home or level == ANONYMIZED:
                pairs.append((h.address, role))
                pairs.append((getattr(h, "rdns", ""), role))
            if level == ANONYMIZED and getattr(h, "rdns", ""):
                pairs.append((".".join(h.rdns.rstrip(".").split(".")[-2:]), "the provider"))  # the provider's domain
        for h in self.known_hops:              # after the shown hops: the first pair of a value wins
            role = self._hop_role.get(h.id) or _KNOWN_ROLE.get(h.category, "Other hop")   # a renamed hop: its role
            pairs += own(h.id, is_home_hop(h), role) + own(h.name, is_home_hop(h), role)
            if is_home_hop(h) or level == ANONYMIZED:
                pairs += [(h.address, role), (getattr(h, "rdns", ""), role)]
        for value, key in self.module_identifiers.items():
            if key != NEVER:
                pairs.append((value, module_role_word(key)))
        for name, role in self._apps.items():
            pairs.append((name, role))
        if level == ANONYMIZED and self.provider:
            pairs.append((self.provider, "the provider"))
        for value, actions in self.field_values.items():
            action = actions[1 if level == VAGUE else 2]
            if action != "keep":
                pairs.append((value, GENERIC if action == "generic" else REMOVED))
        return pairs

    def _count(self, repl: str) -> None:
        kind = "removed" if repl in (REMOVED, GENERIC) else "role"
        self.swept[kind] = self.swept.get(kind, 0) + 1

    # -- text -----------------------------------------------------------------------------------------------------
    def text(self, s: str, table: bool = False) -> str:
        """Pengumon's sentence or quoted text, tokens resolved and (Vague, Anonymized) swept. The sweep sees only the
        text outside tokens, so a role string is never swept again."""
        out = []
        for part in _TOKEN.split(s or ""):
            if part.startswith("{hop:"):
                out.append(self.hop(part[5:-1]))
            elif part.startswith("{role:"):
                out.append(self.role(part[6:-1], table=table))
            elif part.startswith(("line_device", "router_device", "mesh_node:")):
                out.append(self.role(part, table=table))
            else:
                out.append(self._patterns(self._sweep.apply(part, self._count)))
        return "".join(out)

    def _patterns(self, text: str) -> str:
        """The scan's patterns, applied to quoted text: addresses, MACs and host names need no list to be found."""
        for _, pattern, repl in SCAN_RULES[self.level]:
            text = re.sub(pattern, lambda m: (self._count(repl), repl)[1], text, flags=re.I)
        return text

    def title(self, title: str, kind: str, cause: str) -> str:
        """An incident title: kept at Full, swept at Vague, taxonomy words at Anonymized."""
        if self.level == ANONYMIZED:
            return f"{_CAUSE_WORD.get(cause, 'Unknown')}: {kind.replace('_', ' ')}" if kind else "Incident"
        return self.text(title)

    def hop_fields(self, hop) -> Tuple[str, str]:
        """(address, name) cells of the hop table at this level; "-" where the level hides them."""
        home = is_home_hop(hop)
        if self.level == FULL:
            return hop.address or "-", hop.name or "-"
        if self.level == VAGUE and not home:
            return hop.address or "-", (getattr(hop, "rdns", "") or "-")
        return "-", "-"

    def provider_text(self) -> str:
        return self.provider if self.level != ANONYMIZED else "the provider"

    # -- times ----------------------------------------------------------------------------------------------------
    def _dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def day_number(self, ts: float) -> int:
        return (self._dt(ts).date() - self._dt(self.day_zero).date()).days + 1

    def repeated_mark(self, ts: float) -> str:
        """" EDT" / " EST" (Anonymized: " (1st)" / " (2nd)") for a time in the hour the clocks went back over, which
        happened twice; "" for any other time."""
        d = self._dt(ts)
        if d.replace(fold=1 - d.fold).utcoffset() == d.utcoffset():
            return ""
        name = d.tzname() if self.level != ANONYMIZED else None
        return f" {name}" if name else (" (2nd)" if d.fold else " (1st)")

    def when(self, ts: float, style: str = "full") -> str:
        """style: full (weekday, date and clock), dayclock, clock, day. Anonymized: "Day n" and the clock. A time in
        an hour that happened twice (the clocks went back) carries its repeated_mark."""
        d = self._dt(ts)
        clock = d.strftime("%H:%M:%S") if style in ("dayclock", "clock_s") else d.strftime("%H:%M")
        clock += self.repeated_mark(ts) if style != "day" else ""
        if style in ("clock", "clock_s"):
            return clock
        if self.level == ANONYMIZED:
            return f"Day {self.day_number(ts)}" if style == "day" else f"Day {self.day_number(ts)} {clock}"
        if style == "day":
            return d.strftime("%a %m-%d")
        if style == "dayclock":
            return f"{d.strftime('%a')} {clock}"
        return f"{d.strftime('%a %Y-%m-%d')} {clock}"

    def day(self, iso_date: str) -> str:
        """A calendar day of the window ("2026-10-28") as "Tue 10-28", or "Day n" at Anonymized."""
        d = datetime.strptime(iso_date, "%Y-%m-%d")
        if self.level == ANONYMIZED:
            return f"Day {(d.date() - self._dt(self.day_zero).date()).days + 1}"
        return d.strftime("%a %m-%d")

    def zone(self, ts: float) -> str:
        off = self._dt(ts).utcoffset() or timedelta(0)
        mins = int(off.total_seconds() // 60)
        sign = "+" if mins >= 0 else "-"
        return f"UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"

    # -- the final scan -------------------------------------------------------------------------------------------
    def scan(self, doc: str) -> str:
        """The whole document once more. Replaces what it finds and records it in `hits`."""
        def hit(rule):
            def sub(m):
                self.hits[rule] = self.hits.get(rule, 0) + 1
                return REMOVED
            return sub

        for rule, pattern, _ in SCAN_RULES[self.level]:
            doc = re.sub(pattern, hit(rule), doc, flags=re.I)
        return self._sweep.apply(doc, lambda repl: hit("identifier")(None))


_CAUSE_WORD = {"router": "Router", "gateway": "Gateway", "line": "Line", "isp": "ISP", "internet": "Internet", "dns": "DNS",
               "load": "Load", "service": "App", "unknown": "Unknown"}
