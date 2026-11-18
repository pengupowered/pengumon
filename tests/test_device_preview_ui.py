"""Each dashboard card declares the device capabilities its parts use (`uses` in panels/<id>/panel.json); the preview
without a device and the docs table come from those declarations (pengumon.web.capability_table). These tests fail when
a card's code asks for a capability or a feature it does not declare, when a declaration names no real capability, and
when docs/dashboard.md's table is not the one the declarations make."""

import re
from pathlib import Path

import pytest

from pengumon.capabilities import Capability
from pengumon.capabilities.features import FEATURES
from pengumon.web import capability_table

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "pengumon" / "web" / "static"
CAPABILITIES = {c.value for c in Capability}
FEATURE_NEEDS = {name: {c.value for c in needs} for name, needs in FEATURES.items()}
# The calls by which a card's code reads what the devices offer (js/devices.js, js/main.js's ctx, a device's `offers`)
CALLS = re.compile(r"(?:primaryDevice|featureAvailable|devicesOffering|offers\.includes|offers\.some)\(([^()]*)\)"
                   r"|ctx\.reason\(\[([^\]]*)\]|feature === ('[a-z_]+')")
NAME = re.compile(r"'([a-z_]+)'")


def asked_for(text: str) -> set:
    """The capabilities a card's code asks for: the names in those calls, a feature taken as the capabilities it needs."""
    out = set()
    for match in CALLS.finditer(text):
        for name in NAME.findall(" ".join(g for g in match.groups() if g)):
            if name in CAPABILITIES:
                out.add(name)
            elif name in FEATURE_NEEDS:
                out |= FEATURE_NEEDS[name]
    return out


def panels():
    return sorted(p for p in (STATIC / "panels").iterdir() if (p / "panel.json").is_file())


@pytest.mark.parametrize("panel", panels(), ids=lambda p: p.name)
def test_a_card_declares_every_capability_its_code_asks_for(panel):
    import json
    declared = {c for use in json.loads((panel / "panel.json").read_text(encoding="utf-8")).get("uses", [])
                for c in use["capabilities"]}
    asked = set()
    for js in panel.glob("*.js"):
        asked |= asked_for(js.read_text(encoding="utf-8"))
    missing = asked - declared
    assert not missing, f"{panel.name} asks for {sorted(missing)}: add them to `uses` in its panel.json"


def test_every_declaration_names_real_capabilities_and_says_what_the_part_is():
    found = capability_table.declarations(STATIC)
    assert {pid for pid, _, _ in found} >= {"line", "router", "traffic", "latency", "incidents", "diagnosis", "speed"}
    for pid, _, uses in found:
        for use in uses:
            assert use["part"].strip() and set(use["capabilities"]) <= CAPABILITIES and use["capabilities"], (pid, use)


def test_the_check_finds_a_capability_asked_for_in_each_way():
    code = ("primaryDevice('wan_counters'); ctx.featureAvailable('line_health'); ctx.devicesOffering('mesh', 'x');"
            " device.offers.includes('router_system'); ctx.reason(['traffic'], 'x'); f.feature === 'clients';"
            " mark(el, 'traffic_usage')")  # (a plain string is not a read)
    assert asked_for(code) == {"wan_counters", "access_line", "mesh", "router_system", "traffic_usage", "client_inventory"}
    assert asked_for("mark(el('p'), 'traffic')") == set()


def test_the_docs_table_is_the_one_the_declarations_make():
    docs = (ROOT / "docs" / "dashboard.md").read_text(encoding="utf-8")
    table = capability_table.table(STATIC)
    assert table in docs, "docs/dashboard.md: run `python -m pengumon.web.capability_table` and put its table there"


def test_a_part_is_shown_partly_or_hidden_by_what_a_setup_offers():
    assert capability_table.shown(["wan_counters"], frozenset({"wan_counters"})) == "shown"
    assert capability_table.shown(["wan_counters", "router_system"], frozenset({"wan_counters"})) == "partly"
    assert capability_table.shown(["access_line"], frozenset({"wan_counters"})) == "hidden"
