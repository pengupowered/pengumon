"""What the dashboard shows without a router or a modem: the table in docs/dashboard.md, made from what each card declares
it uses (`uses` in its panels/<id>/panel.json: each part and the device capabilities it needs). tests/test_device_preview_ui.py
checks every card's code against its declaration and the docs against this table; `python -m pengumon.web.capability_table`
prints it.

A part is "shown" in a setup when every capability it lists is offered there, "partly" when some are (the Router card with
a basic UPnP router: its WAN load, no CPU or memory), else "hidden". The setups are a router and a modem as Pengumon reads
the most from them (an ASUS router and a DOCSIS gateway: every capability) with one taken away, or the router basic (a
UPnP router: its WAN counters only), as Settings > Display > "Preview without a device" shows them.
"""

import json
import re
from pathlib import Path
from typing import Dict, FrozenSet, List, Tuple

from pengumon.capabilities import Capability

STATIC = Path(__file__).parent / "static"
ALL = frozenset(c.value for c in Capability)
ROUTER = frozenset({"wan_counters", "router_system", "client_inventory", "mesh", "traffic_usage"})
MODEM = frozenset({"access_line"})
SETUPS: Tuple[Tuple[str, FrozenSet[str]], ...] = (
    ("No router", ALL - ROUTER),
    ("No modem", ALL - MODEM),
    ("Basic UPnP router", (ALL - ROUTER) | {"wan_counters"}),
    ("Neither", ALL - ROUTER - MODEM),
)
START = "<!-- capability-table:start (made by python -m pengumon.web.capability_table; do not edit by hand) -->"
END = "<!-- capability-table:end -->"


def declarations(static: Path = STATIC) -> List[Tuple[str, str, List[Dict]]]:
    """(panel id, card title, its `uses`) for each card that declares any, in the cards' default order."""
    out = []
    for folder in sorted((static / "panels").iterdir()):
        manifest_path = folder / "panel.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        title = re.search(r"title:\s*'([^']+)'", (folder / "panel.js").read_text(encoding="utf-8"))
        if manifest.get("uses"):
            out.append((float(manifest.get("order", 1000)), folder.name, title.group(1) if title else folder.name,
                        manifest["uses"]))
    return [(pid, title, uses) for _, pid, title, uses in sorted(out)]


def shown(needs: List[str], offered: FrozenSet[str]) -> str:
    have = [c for c in needs if c in offered]
    return "shown" if len(have) == len(needs) else "partly" if have else "hidden"


def in_card(uses: List[Dict], use: Dict, offered: FrozenSet[str]) -> str:
    """shown() of a part, inside its card: when the card has a "The whole card" entry and that is hidden in the setup, every
    part of it is hidden too (the card is not on the dashboard)."""
    card = next((u for u in uses if u["part"].startswith("The whole card")), None)
    if card is not None and use is not card and shown(card["capabilities"], offered) == "hidden":
        return "hidden"
    return shown(use["capabilities"], offered)


def table(static: Path = STATIC) -> str:
    head = "| Card | Part | Needs | " + " | ".join(name for name, _ in SETUPS) + " |"
    rule = "| --- | --- | --- | " + " | ".join("---" for _ in SETUPS) + " |"
    rows = [f"| {title} | {use['part']} | {', '.join(f'`{c}`' for c in use['capabilities'])} | "
            + " | ".join(in_card(uses, use, offered) for _, offered in SETUPS) + " |"
            for _, title, uses in declarations(static) for use in uses]
    return "\n".join([START, head, rule, *rows, END])


if __name__ == "__main__":
    print(table())
