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


# --- the final scan's patterns, form by form -------------------------------------------------------

import time  # noqa: E402

import pytest  # noqa: E402

from pengumon.reports.redact import ANONYMIZED, FULL, LEVELS, REMOVED, VAGUE  # noqa: E402

# Every written form of a MAC address: each must go at every level, with the text around it kept.
MAC_FORMS = [
    ("02:00:00:00:00:01", REMOVED),
    ("src:02:00:00:00:00:01 x", f"src:{REMOVED} x"),          # a ":" before it: a router log line
    ("MAC:AA:AA:AA:DD:EE:FF dropped", f"MAC:{REMOVED} dropped"),
    ("host-02:00:00:00:00:01", f"host-{REMOVED}"),             # a letter and a dash before it
    ("mac=02-00-00-00-00-01", f"mac={REMOVED}"),               # dashes: once cut in two by the address rule
    ("mac=10-00-00-01-00-02", f"mac={REMOVED}"),               # pairs that read as a 10.x address at Vague
    ("aabbccddeeff", REMOVED),                                  # twelve bare hex digits
    ("MAC AABBCCDDEEFF.", f"MAC {REMOVED}."),
    ("0200.0000.0001", REMOVED),                                # Cisco's dotted form
    ("(02-00-00-AA-BB-01)", f"({REMOVED})"),
    ("02:00:00:00:00:01: link up", f"{REMOVED}: link up"),
]
# Not MAC addresses: left alone by the MAC rule (at Full, where no address rule runs).
NOT_MACS = ["2001:db8:aa:aa:aa:dd:ee:ff", "fe80::aa:aa:aa:dd:ee:ff", "02:00:00:ff:fe:00:00:01", "123456789012",
            "1234567890abc", "12:30:45", "2026-11-10T12:00:00-05:00", "aa:bb:cc:dd:ee", "fade"]


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,expected", MAC_FORMS)
def test_the_final_scan_removes_a_mac_in_every_written_form(level, text, expected):
    rx = Redactor(level)
    assert rx.scan(text) == expected
    assert rx.hits == {"mac": 1}
    assert Redactor(level).text(text) == expected          # quoted text (titles, module cells) gets the same rules


@pytest.mark.parametrize("text", NOT_MACS)
def test_the_mac_rule_leaves_addresses_times_and_numbers_alone(text):
    rx = Redactor(FULL)
    assert rx.scan(text) == text and rx.hits == {}


@pytest.mark.parametrize("name", ["x.fritz.box", "MyPhone.example.xyz", "printer.localdomain", "nas.home.arpa",
                                  "Living-Room.mesh.internal", "cam1.example.cloud"])
def test_anonymized_removes_any_host_name(name):
    rx = Redactor(ANONYMIZED)
    assert rx.scan(f"reached {name}, then lost") == f"reached {REMOVED}, then lost" and rx.hits == {"host name": 1}


@pytest.mark.parametrize("text", ["Pengumon 0.27.0", "loss 3.14%", "e.g. the modem", "i.e. the line", "Wi-Fi 2.4 GHz",
                                  "v1.2.3", "12.5ms at 21:14"])
def test_the_host_name_rule_leaves_versions_numbers_and_abbreviations_alone(text):
    rx = Redactor(ANONYMIZED)
    assert rx.scan(text) == text and rx.hits == {}


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,secret", [
    ("GET http://192.0.2.9/api?token=Cnry0abcd&x=1", "Cnry0abcd"),
    ("login failed: password=Cnry1hunter2", "Cnry1hunter2"),
    ("url /cb?api_key=Cnry2XYZ", "Cnry2XYZ"), ("?a=1&key=Cnry3abc", "Cnry3abc"),
    ("access_token=Cnry4tok rejected", "Cnry4tok"), ("http://admin:Cnry5pw@192.0.2.9/", "Cnry5pw")])
def test_the_final_scan_removes_secrets_in_urls_and_messages_at_every_level(level, text, secret):
    rx = Redactor(level)
    out = rx.scan(text)
    assert secret not in out and rx.hits.get("secret") == 1, out


def test_the_secret_rule_leaves_pengumons_words_alone():
    for text in ("the role key of a mesh node", "a token bucket", "check its username and password in Settings"):
        assert Redactor(FULL).scan(text) == text


@pytest.mark.parametrize("level", LEVELS)
def test_the_scan_stays_linear_on_long_hostile_input(level):
    """Each input is 200,000 characters of a shape that makes a careless pattern backtrack (a run of hex pairs, a
    dotted run with no name at its end, digits that never become an address). The bound is far above the linear
    time and far below a quadratic one (the old host-name rule took minutes on the dotted run)."""
    inputs = ["0:" * 100_000, "aa:" * 66_666, "aa-" * 66_666, "a" * 200_000, "0" * 200_000, "a." * 100_000,
              "ab.ab." * 33_333 + "1", "aaaa." * 40_000, "1." * 100_000, "-a" * 100_000, "key" * 66_666]
    started = time.monotonic()
    for text in inputs:
        Redactor(level).scan(text)
    assert time.monotonic() - started < 10.0
