"""The dashboard's panels and dialogs, discovered from their folders at start.

Every panel lives in its own folder, web/static/panels/<id>/, with
  panel.json   its manifest: {"order": <number>} (its default place; the layout config and the browser reorder it)
  panel.html   its partial: exactly one <section data-panel="<id>">
  panel.js     its module (the registry contract: default export {id, title, tab, ...})
  *.css        its styles; any other *.html is a dialog the panel owns (included outside <main>)
and every dialog that no panel owns lives in web/static/dialogs/<id>/ with dialog.html, dialog.js and *.css (each
optional). The page includes, links and loads whatever is there, so adding a panel or a dialog is adding a folder:
no shared file names it. Folders live under static/ so a module's relative imports mean the same on disk and in the
URL; their .html and .json are templates and data, never served as files (web/assets.py refuses them).
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Tuple

STATIC_DIR = Path(__file__).resolve().parent / "static"
NOT_SERVED = (".html", ".json")  # under panels/ and dialogs/: templates and manifests


@dataclass(frozen=True)
class Part:
    """One panel or dialog folder. Paths are relative to web/static (template names and asset URLs alike)."""

    id: str
    order: float
    template: str  # the panel's partial, or the dialog's markup ("" for a dialog that builds its own)
    dialogs: Tuple[str, ...]  # a panel's own dialog templates
    css: Tuple[str, ...]
    module: str  # "" when the folder has no module


@dataclass(frozen=True)
class Ui:
    panels: Tuple[Part, ...]
    dialogs: Tuple[Part, ...]

    @property
    def css(self) -> Tuple[str, ...]:
        return tuple(c for p in (*self.panels, *self.dialogs) for c in p.css)

    @property
    def dialog_templates(self) -> Tuple[str, ...]:
        return tuple(t for p in self.panels for t in p.dialogs) + tuple(d.template for d in self.dialogs if d.template)

    @property
    def modules(self) -> dict:
        """What the page loads (body data-modules): the dialogs' modules, then the panels' in their default order."""
        return {"dialogs": [d.module for d in self.dialogs if d.module], "panels": [p.module for p in self.panels]}


def _rel(path: Path, static: Path) -> str:
    return path.relative_to(static).as_posix()


def discover(static: Path = STATIC_DIR) -> Ui:
    panels = []
    for folder in sorted((static / "panels").iterdir()):
        if not folder.is_dir():
            continue
        manifest = json.loads((folder / "panel.json").read_text(encoding="utf-8"))
        panels.append(Part(
            id=folder.name, order=float(manifest.get("order", 1000)),
            template=_rel(folder / "panel.html", static),
            dialogs=tuple(_rel(p, static) for p in sorted(folder.glob("*.html")) if p.name != "panel.html"),
            css=tuple(_rel(p, static) for p in sorted(folder.glob("*.css"))),
            module=_rel(folder / "panel.js", static)))
    panels.sort(key=lambda p: (p.order, p.id))
    dialogs = []
    for folder in sorted((static / "dialogs").iterdir()):
        if not folder.is_dir():
            continue
        dialogs.append(Part(
            id=folder.name, order=0,
            template=_rel(folder / "dialog.html", static) if (folder / "dialog.html").is_file() else "",
            dialogs=(), css=tuple(_rel(p, static) for p in sorted(folder.glob("*.css"))),
            module=_rel(folder / "dialog.js", static) if (folder / "dialog.js").is_file() else ""))
    return Ui(panels=tuple(panels), dialogs=tuple(dialogs))


@lru_cache(maxsize=1)
def ui() -> Ui:
    return discover()


def panel_ids() -> Tuple[str, ...]:
    """The panel ids in their default order (what the `dashboard:` config accepts)."""
    return tuple(p.id for p in ui().panels)
