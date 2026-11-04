"""The AI pack's three detail levels, on hand-made facts with a canary in every free-text field (render_pack) and
through build_ai_pack with the real contributions on a seeded day (built_pack)."""

import dataclasses
import re

from ai_pack_helpers import HOME_CANARIES, MODEL, NEVER_SHOWN, PROVIDER_CANARIES, built_pack, canary_facts
from pengumon.reports.ai_pack import HopFact, render_pack
from pengumon.reports.redact import Redactor


def sections(md):
    return {part.split("\n", 1)[0]: part for part in md.split("\n## ")}


def present(md, needles):
    low = md.lower()
    return [n for n in needles if n.lower() in low]


def scan_hits(built):
    return {k: v for k, v in built["redactions"].items() if k.startswith("scan:")}


async def test_full_names_what_the_app_shows_and_pengumons_own_sentences_name_nothing(tmp_path, monkeypatch):
    pack = render_pack(canary_facts(), "full")
    md = pack.markdown
    for shown in ("Canary-Router-Name", "192.168.88.1", "Canary-Modem-Name", "po-1.edge.example-isp.net", "Canary-Mesh-Kitchen",
                  MODEL, "Canary-App", "2026-10-28"):
        assert shown in md, shown
    assert not present(md, NEVER_SHOWN)
    secs = sections(md)
    own = [secs["Findings"], secs["Close-ups (the 1 most significant)"].split("1-minute")[0],
           secs["Your devices"].split("| Node")[0]]
    for text in own:
        assert not present(text, HOME_CANARIES), present(text, HOME_CANARIES)
    # an event phrase in the incident table speaks in the role word, the Wi-Fi table adds the label to it
    assert "Mesh node 2 at 0 clients" in md and "Canary-Mesh-Hall (Mesh node 2)" in md
    assert pack.hits == {}
    assert "02:00:00:aa:bb:01" not in md                      # a MAC in a title: at no level
    built = await built_pack(tmp_path, monkeypatch, "full")
    md = built["markdown"]
    assert MODEL in md and "02:00:00:aa:bb:02" not in md and scan_hits(built) == {"scan:mac": 1}   # the scan at Full
    assert "0000a1" not in md                                 # a mesh node id: at no level, by the hints
    assert not present(md, ["Canary-Client-Phone", "02-00-00-AA-BB-01"])   # anything per client: at no level
    assert "CanaryPass" not in md                             # a password in a URL: at no level


async def test_vague_has_no_home_value_in_any_spelling_and_keeps_the_provider(tmp_path, monkeypatch):
    pack = render_pack(canary_facts(leaky=True), "vague")
    md = pack.markdown
    assert not present(md, HOME_CANARIES + NEVER_SHOWN), present(md, HOME_CANARIES + NEVER_SHOWN)
    assert not re.search(r"192[.\-_]168|02:00:00|canary", md, re.I)
    assert not present(md, ["Canary-Public-Name"])
    assert all(p in md for p in PROVIDER_CANARIES) and MODEL in md
    assert "Fri 21:14:03" in md and "Wed 2026-10-28 14:00" in md and "local time (UTC-05:00," in md
    # one string per role, everywhere
    assert md.count("| Router |") >= 1 and "Router" in pack.roles and "Mesh node 2" in pack.roles
    assert "Mesh node 2 at 0 clients" in md and re.search(r"\| Mesh node 2 \| Wi-Fi 5 GHz", md)
    assert pack.hits == {}, "the final scan found something the rules should have caught"
    built = await built_pack(tmp_path, monkeypatch, "vague")
    md = built["markdown"]
    assert not scan_hits(built) and not present(md, HOME_CANARIES + ["canary"])
    assert not present(md, ["Canary-Old-Printer", "Canary-Renamed-Old"])   # a hop switched off, a hop's old name
    assert "2001:DB8:88:0:0::1" not in md                     # the router's address in another IPv6 spelling
    assert MODEL in md and "Example-Cable 3.1" in md          # the models are kept at Vague (hints: fields["model"])
    assert "the modem or gateway, pinged on its home-side" in md   # a device called "modem" leaves Pengumon's words alone


async def test_anonymized_has_no_name_address_date_provider_model_or_vendor(tmp_path, monkeypatch):
    pack = render_pack(canary_facts(leaky=True), "anonymized")
    md = pack.markdown
    assert not present(md, HOME_CANARIES + NEVER_SHOWN + PROVIDER_CANARIES + [MODEL, "Canary", "xfinity", "asus", "aimesh",
                                                                                "xb7", "example-isp", "comcast"])
    assert not re.search(r"\d{1,3}([.\-_])\d{1,3}\1\d{1,3}\1\d{1,3}|20\d\d-\d\d-\d\d|\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|UTC[+-]", md)
    assert "Day 1" in md and "Day 3 21:14:03" in md and "the provider" in md
    assert "Device notes: the modem is a cable gateway supplied by the provider." in md
    assert pack.hits == {}, "the final scan found something the rules should have caught"
    built = await built_pack(tmp_path, monkeypatch, "anonymized")
    md = built["markdown"]
    assert not scan_hits(built) and not present(md, [MODEL, "Example-Cable", "Canary", "0000a", "xfinity", "asus",
                                                     "aimesh", "xb7"])


def test_no_redaction_debris_and_pengumons_own_words_stay():
    """A swept name takes its brackets along and a name swept next to its address says the role once; a never-shown
    value that is a word of the level's own description ("MAC addresses") leaves that description alone."""
    rx = Redactor("vague", hops=[HopFact("r", "local", "Mesh main (192.168.88.1)", "192.168.88.1"),
                                 HopFact("g", "gateway", "Unit-7", "192.168.100.1")])
    text = rx.text("Loss at Mesh main (192.168.88.1), then at Unit-7 (192.168.100.1)")
    assert text == f"Loss at {rx.hop('r')}, then at {rx.hop('g')}"
    facts = canary_facts()
    wifi = dataclasses.replace(facts.modules[2], identifiers={**facts.modules[2].identifiers, "Mac": "never"})
    md = render_pack(dataclasses.replace(facts, modules=facts.modules[:2] + (wifi,)), "vague").markdown
    assert "device names and MAC addresses are replaced" in md


def test_vague_keeps_provider_and_public_hops_named_after_what_it_shows():
    """ISP and public hops named after their reverse name or address (as discovery names them, now or in an earlier
    path version): Vague shows those values, so neither the sweep nor the final scan replaces them; Anonymized does.
    A home hop's name is swept whatever it matches."""
    facts = canary_facts()
    hops = list(facts.hops)
    hops[2] = dataclasses.replace(hops[2], name="po-1.edge.example-isp.net")
    hops[3] = dataclasses.replace(hops[3], name="192.0.2.53", rdns="resolver.example.net")
    old = (HopFact(hops[3].id, "public", "resolver.example.net", "192.0.2.53"),)
    facts = dataclasses.replace(facts, hops=tuple(hops), known_hops=old)
    vague = render_pack(facts, "vague")
    assert "| 198.51.100.1 (po-1.edge.example-isp.net) |" in vague.markdown
    assert "| 192.0.2.53 (resolver.example.net) |" in vague.markdown and vague.hits == {}
    anon = render_pack(facts, "anonymized")
    assert not present(anon.markdown, ["example-isp", "resolver.example", "192.0.2.53"]) and anon.hits == {}
    # a home hop's name stays swept even where the provider's names contain it
    rx = Redactor("vague", hops=[HopFact("r", "local", "edge", "192.168.88.1"), hops[2]])
    assert rx.text("Packet loss at edge") == "Packet loss at Router"
