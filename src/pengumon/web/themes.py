"""Themes: the page's colors, fonts and shapes, discovered from their folders at start (docs/themes.md).

Every theme lives in its own folder, web/static/themes/<id>/, with
  theme.json  {"id", "name", "description", "modes": ["dark"] | ["light"] | ["dark", "light"],
               "tokens": {"dark": {"--bg": "#...", ...}, "light": {...}}, "effect": null | {"id", "default_on"},
               "tags": [...], "chart": optional {"line_width", "curve", "grid", "points", "fill", "spark"}}
  skin.css    optional: shape and texture rules, every selector under :root[data-look="<id>"]
  <effect id>.js  optional: the animated effect, an ES module (default export {start(layer, ctx) -> stop})
  panels/<panel id>.js (+ .css)  optional: the theme's own view of that card (js/cards.js; default export {mount})
  panels/<panel id>.css alone      optional: the theme's restyling of that card's standard view
so adding a theme is adding a folder. The tokens are the custom properties of tokens.css; the theme `default` is
tokens.css itself (its theme.json carries no tokens). Custom themes (Settings > Display, `display.themes`) are
checked by the same rules, narrowed: colors and a few sizes only, no skin, no effect, no views and no chart style.

The page applies a theme as <html data-look="<id>">, next to data-theme (light or dark). A built-in theme's token
blocks, skin and card views' stylesheets are one generated stylesheet (`look_css`, served under the tree hash at
themes/<id>/theme.css), which the page links only while the theme is in use; a custom theme's block is written into the
page (`custom_css`).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
THEMES_DIR = STATIC_DIR / "themes"
TOKENS_CSS = STATIC_DIR / "tokens.css"
MODES = ("dark", "light")
DEFAULT_ID = "default"
# Every value pattern here is used with fullmatch: `$` with match() would let a trailing "\n" through.
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
CUSTOM_PREFIX = "custom-"
MAX_CUSTOM = 30

# ---------- colors ----------

_HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
HEX6 = re.compile(r"#[0-9a-fA-F]{6}")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*(0|1|0?\.\d+|1\.0+)\s*)?\)")


def parse_color(text: str) -> Optional[Tuple[float, float, float, float]]:
    """(r, g, b, alpha) with r, g, b in 0..255 and alpha in 0..1, for #rgb, #rrggbb, #rrggbbaa, rgb() and rgba();
    None for anything else."""
    text = text.strip()
    m = _HEX.fullmatch(text)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a
    m = _RGB.fullmatch(text)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        if max(r, g, b) > 255:
            return None
        return r, g, b, float(m.group(4)) if m.group(4) is not None else 1.0
    return None


# ---------- tokens.css: the names and the default theme ----------

def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _declarations(body: str) -> Dict[str, str]:
    """`--name: value;` pairs of a rule body (other properties are left out)."""
    out: Dict[str, str] = {}
    depth, start = 0, 0
    parts = []
    for i, c in enumerate(body):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ";" and depth == 0:
            parts.append(body[start:i])
            start = i + 1
    parts.append(body[start:])
    for part in parts:
        name, sep, value = part.partition(":")
        name = name.strip()
        if sep and name.startswith("--"):
            out[name] = " ".join(value.split())
    return out


def _block(css: str, selector: str) -> str:
    """The body of the first rule whose selector is exactly `selector` (outside @media)."""
    m = re.search(r"(^|})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    if not m:
        raise ValueError(f"tokens.css has no {selector} block")
    return m.group(2)


@lru_cache(maxsize=1)
def base_tokens() -> Dict[str, Dict[str, str]]:
    """The default theme's tokens per mode, from tokens.css: dark is the bare :root block, light is dark with the
    [data-theme="light"] block over it."""
    css = _strip_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    dark = _declarations(_block(css, ":root"))
    light = {**dark, **_declarations(_block(css, ':root[data-theme="light"]'))}
    return {"dark": dark, "light": light}


@lru_cache(maxsize=1)
def color_tokens() -> Tuple[str, ...]:
    """The tokens every theme must give in each of its modes: those whose value in tokens.css is a color
    (backgrounds, ink, lines, accent, focus, status and their -bg, the heat scale --q0..--q4, series --s1..--s8)."""
    return tuple(n for n, v in base_tokens()["dark"].items() if parse_color(v))


def hex_only(name: str) -> bool:
    """A token the canvas code appends an alpha to ("#3987e5" + "26"): it must stay #rrggbb in every theme."""
    return bool(HEX6.fullmatch(base_tokens()["dark"].get(name, "")))


# The other tokens a theme may set, by kind: (pattern, (min, max) for the number, or None). Custom themes may set
# the colors and the `CUSTOM_SIZES` only.
_PX = re.compile(r"(\d{1,4}(?:\.\d{1,2})?)px")
_MS = re.compile(r"(\d{1,4})ms")
_INT = re.compile(r"(\d{3})")
_FONT = re.compile(r"[A-Za-z0-9 ,'\"-]{1,300}")
_EASE = re.compile(r"cubic-bezier\(\s*-?[\d.]+\s*,\s*-?[\d.]+\s*,\s*-?[\d.]+\s*,\s*-?[\d.]+\s*\)")
_SHADOW = re.compile(r"[A-Za-z0-9 #.,%()\-]{1,400}")  # built-in only: no ; { } < > \ or url()

SIZE_RANGES: Dict[str, Tuple[float, float]] = {
    "--fs-xs": (9, 16), "--fs-sm": (10, 18), "--fs-md": (12, 20), "--fs-lg": (14, 26), "--fs-xl": (16, 36),
    "--fs-2xl": (20, 56),
    "--r-xs": (0, 12), "--r-sm": (0, 16), "--r-md": (0, 24), "--r-lg": (0, 32), "--r-pill": (0, 999),
    "--sp-1": (2, 8), "--sp-2": (4, 14), "--sp-3": (6, 20), "--sp-4": (8, 28), "--sp-5": (10, 32), "--sp-6": (12, 40),
    "--sp-8": (16, 56), "--sp-10": (20, 72), "--page-max": (960, 3840), "--tabbar-h": (48, 96),
}
CUSTOM_SIZES = tuple(n for n in SIZE_RANGES if n.startswith(("--fs-", "--r-")))
_RULES: Dict[str, Tuple[re.Pattern, Optional[Tuple[float, float]]]] = {
    **{n: (_PX, r) for n, r in SIZE_RANGES.items()},
    "--font": (_FONT, None), "--mono": (_FONT, None),
    "--fw-medium": (_INT, (100, 900)), "--fw-semi": (_INT, (100, 900)), "--fw-bold": (_INT, (100, 900)),
    "--t-fast": (_MS, (0, 1000)), "--t-med": (_MS, (0, 1000)), "--ease": (_EASE, None),
    "--shadow-pop": (_SHADOW, None), "--shadow-sheet": (_SHADOW, None), "--glow-warn": (_SHADOW, None),
    "--glow-crit": (_SHADOW, None),
}


def token_problem(name: str, value: Any, custom: bool) -> Optional[str]:
    """Why `value` cannot be the theme's `name`, in words; None when it can."""
    if not isinstance(value, str):
        return f"{name}: the value must be text"
    value = value.strip()
    if name in color_tokens():
        if hex_only(name):
            return None if HEX6.fullmatch(value) else f"{name}: must be a color like #1a2b3c"
        return None if parse_color(value) else f"{name}: must be a color (#1a2b3c, #1a2b3c80 or rgba(26, 43, 60, .5))"
    if custom and name not in CUSTOM_SIZES:
        known = name in base_tokens()["dark"]
        return f"{name}: a custom theme may set colors and text sizes and corner radii only" if known else f"{name}: not a theme token"
    rule = _RULES.get(name)
    if rule is None:
        return f"{name}: not a theme token" if name not in base_tokens()["dark"] else f"{name}: a theme cannot change it"
    pattern, bounds = rule
    m = pattern.fullmatch(value)
    if not m:
        return f"{name}: {value!r} is not a value it takes"
    if bounds:
        n = float(m.group(1))
        if not bounds[0] <= n <= bounds[1]:
            return f"{name}: must be between {bounds[0]:g} and {bounds[1]:g}"
    return None


# ---------- a theme ----------

@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    description: str
    modes: Tuple[str, ...]
    tokens: Dict[str, Dict[str, str]]  # mode -> the tokens it sets (for `default`: every token of tokens.css)
    tags: Tuple[str, ...] = ()
    effect: Optional[Dict[str, Any]] = None  # {"id": file stem, "default_on": bool}
    skin: str = ""  # path of skin.css relative to static/, "" for none
    builtin: bool = True
    views: Tuple[str, ...] = ()  # the panel ids the theme draws a view of (panels/<id>.js)
    view_css: Tuple[str, ...] = ()  # their stylesheets, paths relative to static/
    chart: Optional[Dict[str, Any]] = None  # chart style (CHART_STYLE), None for the charts' own

    def view_module(self, panel: str) -> str:
        return f"themes/{self.id}/panels/{panel}.js"

    @property
    def effect_module(self) -> str:
        return f"themes/{self.id}/{self.effect['id']}.js" if self.effect else ""

    def resolved(self, mode: str) -> Dict[str, str]:
        """Every token in `mode` (the theme's own over the default's); a mode the theme lacks gives its only one."""
        mode = mode if mode in self.modes else self.modes[0]
        return {**base_tokens()[mode], **self.tokens.get(mode, {})}

    def exported(self) -> Dict[str, Any]:
        """The theme as a theme.json a custom theme can be made from: its colors (all of them, so it stands alone)
        and the text sizes and radii it changes; fonts, shadows, skin, effect and chart style stay behind. With a
        "custom-" id it is a valid ThemeItem as it is (only the fields a custom theme has)."""
        tokens = {}
        for mode in self.modes:
            mine = self.resolved(mode)
            own = self.tokens.get(mode, {})
            tokens[mode] = {**{n: mine[n] for n in color_tokens()}, **{n: own[n] for n in CUSTOM_SIZES if n in own}}
        return {"id": self.id, "name": self.name, "description": self.description, "modes": list(self.modes),
                "tokens": tokens, "tags": list(self.tags)}


# What "chart" in theme.json may set (docs/themes.md, "Chart style"): js/chart-theme.js applies it to every Chart.js
# chart. name -> the values it takes: a (min, max) range for a number, a tuple of words, or bool.
CHART_STYLE: Dict[str, Any] = {
    "line_width": (0.5, 6),  # px of a chart's main lines (the charts' own: 2); thinner lines keep their proportion
    "curve": ("smooth", "straight", "stepped"),  # smooth: the chart's own tension; straight: none; stepped: steps
    "grid": bool,  # false: no grid lines in the plot area
    "points": (0, 6),  # a dot of this radius on every reading (0: none, the charts' own)
    "fill": bool,  # true: the area under a series line is washed with its color
    "spark": ("line", "bars"),  # the sparklines (the last probes of a path node, a hop): a line, or a bar per probe
}


def chart_problems(chart: Any) -> List[str]:
    if not isinstance(chart, dict) or not chart:
        return ['chart: null or an object of "line_width", "curve", "grid", "points", "fill" and "spark"']
    problems = []
    for name, value in chart.items():
        rule = CHART_STYLE.get(name)
        if rule is None:
            problems.append(f"chart: unknown field {name!r}")
        elif rule is bool:
            if not isinstance(value, bool):
                problems.append(f"chart.{name}: true or false")
        elif isinstance(rule[0], str):
            if value not in rule:
                problems.append(f"chart.{name}: one of {', '.join(rule)}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not rule[0] <= value <= rule[1]:
            problems.append(f"chart.{name}: a number from {rule[0]:g} to {rule[1]:g}")
    return problems


def check_theme(raw: Any, *, custom: bool, folder: Optional[Path] = None) -> Tuple[Optional[Theme], List[str]]:
    """(the theme, []) or (None, [problems in words]). `folder`: a built-in theme's folder (its id must match the
    folder name and its effect file must exist there). A custom theme's id starts with "custom-"."""
    problems: List[str] = []
    if not isinstance(raw, dict):
        return None, ["a theme is a JSON object"]
    allowed = {"id", "name", "description", "modes", "tokens", "effect", "tags", "chart"}
    extra = sorted(set(raw) - allowed)
    if extra:
        problems.append(f"unknown field {extra[0]!r}")
    tid = raw.get("id")
    if not isinstance(tid, str) or not ID_RE.fullmatch(tid):
        problems.append("id: lower-case letters, digits and dashes, at most 40")
    elif custom and not tid.startswith(CUSTOM_PREFIX):
        problems.append(f"id: a custom theme's id starts with {CUSTOM_PREFIX!r}")
    elif not custom and tid.startswith(CUSTOM_PREFIX):
        problems.append(f"id: {CUSTOM_PREFIX!r} is for custom themes")
    elif folder is not None and tid != folder.name:
        problems.append(f"id: must be the folder's name, {folder.name!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        problems.append("name: 1 to 40 characters")
    description = raw.get("description", "")
    if not isinstance(description, str) or len(description) > 200:
        problems.append("description: at most 200 characters")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and 0 < len(t) <= 24 for t in tags) or len(tags) > 8:
        problems.append("tags: at most 8 short words")
    modes = raw.get("modes")
    if not isinstance(modes, list) or not modes or len(set(modes)) != len(modes) or not set(modes) <= set(MODES):
        problems.append('modes: ["dark"], ["light"] or ["dark", "light"]')
        modes = []
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        problems.append("tokens: an object with the tokens of each mode")
        tokens = {}
    is_default = not custom and tid == DEFAULT_ID
    if is_default:
        if tokens:
            problems.append("tokens: the default theme's tokens are tokens.css; its theme.json gives none")
    else:
        for mode in sorted(set(tokens) - set(modes)):
            problems.append(f"tokens: {mode!r} is not one of the theme's modes")
        for mode in modes:
            given = tokens.get(mode)
            if not isinstance(given, dict):
                problems.append(f"tokens.{mode}: missing")
                continue
            for n, v in given.items():
                why = token_problem(n, v, custom)
                if why:
                    problems.append(f"tokens.{mode}: {why}")
            missing = [n for n in color_tokens() if n not in given]
            if missing:
                problems.append(f"tokens.{mode}: missing colors {', '.join(missing)}")
    effect = raw.get("effect")
    if effect is not None:
        if custom:
            problems.append("effect: a custom theme has no effect")
        elif not isinstance(effect, dict) or not isinstance(effect.get("id"), str) or not ID_RE.fullmatch(effect["id"]) \
                or not isinstance(effect.get("default_on", True), bool) or set(effect) - {"id", "default_on"}:
            problems.append('effect: null or {"id": "<file stem>", "default_on": true}')
        elif folder is not None and not (folder / f"{effect['id']}.js").is_file():
            problems.append(f"effect: {effect['id']}.js is not in the theme's folder")
    chart = raw.get("chart")
    if chart is not None:
        problems.extend(["chart: a custom theme has no chart style"] if custom else chart_problems(chart))
    if problems:
        return None, problems
    if is_default:
        tokens = dict(base_tokens())
    skin = ""
    if folder is not None and (folder / "skin.css").is_file():
        skin = f"themes/{folder.name}/skin.css"
    views = tuple(sorted(p.stem for p in (folder / "panels").glob("*.js"))) if folder is not None else ()
    view_css = tuple(f"themes/{folder.name}/panels/{p.name}" for p in sorted((folder / "panels").glob("*.css"))) if folder is not None else ()
    return Theme(id=tid, name=name.strip(), description=description, modes=tuple(m for m in MODES if m in modes),
                 tokens={m: {n: v.strip() for n, v in tokens[m].items()} for m in modes}, tags=tuple(tags),
                 effect=({"id": effect["id"], "default_on": effect.get("default_on", True)} if effect else None),
                 skin=skin, builtin=not custom, views=views, view_css=view_css,
                 chart=dict(chart) if chart and not custom else None), []


# ---------- skins ----------

def skin_problems(theme_id: str, css: str, card: str = "") -> List[str]:
    """A skin may only style its own theme: every selector starts with :root[data-look="<id>"] (inside @media and
    @supports too); @keyframes are allowed; no @import, @font-face or url() to anywhere but the theme's folder. With a
    `card` (a card's stylesheet) every selector is also scoped to that card: :root[data-look="<id>"] [data-panel="<card>"],
    so a card's rules never reach another card."""
    scope = f':root[data-look="{theme_id}"]'
    if card:
        scope += f' [data-panel="{card}"]'
    problems: List[str] = []
    text = _strip_comments(css)
    if re.search(r"@import|@font-face", text, re.IGNORECASE):
        problems.append("skin.css: no @import or @font-face (themes use the system's fonts)")
    if re.search(r"url\(\s*['\"]?\s*(?:[a-z]+:|//)", text, re.IGNORECASE):
        problems.append("skin.css: url() only to files of the theme's own folder")

    def walk(body: str) -> None:
        i = 0
        while i < len(body):
            brace = body.find("{", i)
            if brace < 0:
                if body[i:].strip():
                    problems.append(f"skin.css: stray text {body[i:].strip()[:40]!r}")
                return
            prelude = body[i:brace].strip()
            depth, j = 1, brace + 1
            while j < len(body) and depth:
                depth += {"{": 1, "}": -1}.get(body[j], 0)
                j += 1
            inner = body[brace + 1:j - 1]
            if prelude.startswith("@"):
                if re.match(r"@(media|supports|container)\b", prelude):
                    walk(inner)
                elif not re.match(r"@(-webkit-)?keyframes\b", prelude):
                    problems.append(f"skin.css: {prelude.split()[0]} is not allowed")
            else:
                for selector in (s.strip() for s in prelude.split(",")):
                    if not selector.startswith(scope):
                        problems.append(f"skin.css: {selector[:60]!r} is not under {scope}")
            i = j

    walk(text)
    return problems


# ---------- views of cards ----------

# What a view module may not do: it draws from the view model; fetching, timers and storage are the model's.
_VIEW_FORBIDDEN = ("fetch(", "XMLHttpRequest", "WebSocket", "setInterval", "requestAnimationFrame", "localStorage",
                   "sessionStorage", "eval(", "new Function")


def view_problems(theme_id: str, folder: Path) -> List[str]:
    """A theme's cards (panels/<panel id>.js and .css): each names a card the page has; a view (.js) is an ES module with
    a default export {mount(root, ctx)} and does none of _VIEW_FORBIDDEN; a stylesheet (.css, with its view or alone: then
    it restyles the card's standard view) follows the skin rules with no url() at all (it is served inside the theme's one
    stylesheet, look_css, whose URL is not the panels/ folder)."""
    from pengumon.web.ui import panel_ids  # the cards the page has (ui imports nothing from here)
    problems: List[str] = []
    panels = folder / "panels"
    if not panels.is_dir():
        return problems
    for path in sorted(panels.iterdir()):
        name = f"panels/{path.name}"
        if path.suffix not in (".js", ".css") or not path.is_file():
            problems.append(f"{name}: a card is drawn by <panel id>.js and restyled by <panel id>.css")
            continue
        if path.stem not in panel_ids():
            problems.append(f"{name}: there is no card {path.stem!r} (cards: {', '.join(panel_ids())})")
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".css":
            problems.extend(p.replace("skin.css", name) for p in skin_problems(theme_id, text, path.stem))
            if re.search(r"url\(", _strip_comments(text), re.IGNORECASE):
                problems.append(f"{name}: no url()")
            continue
        if not re.search(r"export default\s*\{[\s\S]*\bmount\s*\(", text):
            problems.append(f"{name}: the default export is {{mount(root, ctx)}}")
        for word in _VIEW_FORBIDDEN:
            if word in text:
                problems.append(f"{name}: a view does not use {word.rstrip('(')} (the model fetches, keeps time and stores)")
    return problems


# ---------- discovery ----------

@lru_cache(maxsize=1)
def builtin_themes() -> Tuple[Theme, ...]:
    """The theme folders, `default` first, then by name. A folder with a broken theme.json is left out (logged);
    tests/test_themes.py fails on it."""
    found: List[Theme] = []
    for folder in sorted(p for p in THEMES_DIR.iterdir() if p.is_dir()) if THEMES_DIR.is_dir() else []:
        try:
            raw = json.loads((folder / "theme.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.error(f"Theme {folder.name}: theme.json could not be read ({e})")
            continue
        theme, problems = check_theme(raw, custom=False, folder=folder)
        if theme and theme.skin:
            problems = skin_problems(theme.id, (STATIC_DIR / theme.skin).read_text(encoding="utf-8"))
        if theme and not problems:
            problems = view_problems(theme.id, folder)
        if problems or theme is None:
            logger.error(f"Theme {folder.name} left out: {'; '.join(problems)}")
            continue
        found.append(theme)
    found.sort(key=lambda t: (t.id != DEFAULT_ID, t.name.lower()))
    return tuple(found)


def builtin_ids() -> Tuple[str, ...]:
    return tuple(t.id for t in builtin_themes())


def custom_themes(items: List[Any]) -> List[Theme]:
    """The saved custom themes (display.themes items, models or dicts) as Themes; a broken one is left out."""
    out = []
    for item in items or []:
        raw = item.model_dump() if hasattr(item, "model_dump") else item
        theme, _ = check_theme(raw, custom=True)
        if theme:
            out.append(theme)
    return out


def all_themes(custom_items: List[Any] = ()) -> List[Theme]:
    return [*builtin_themes(), *custom_themes(list(custom_items))]


# ---------- CSS ----------

def _rule(selector: str, scheme: str, tokens: Dict[str, str]) -> str:
    body = "".join(f" {n}: {v};" for n, v in tokens.items())
    return f"{selector} {{ color-scheme: {scheme};{body} }}\n"


def theme_css(theme: Theme) -> str:
    """The token blocks of one theme. One mode: one block. Two: dark on the theme's selector, light where the system
    asks for light (unless the person picked dark) and where the person picked light; the more specific selectors win
    over tokens.css whatever the order."""
    if theme.id == DEFAULT_ID:
        return ""
    look = f':root[data-look="{theme.id}"]'
    if len(theme.modes) == 1:
        return _rule(look, theme.modes[0], theme.tokens[theme.modes[0]])
    light = theme.tokens["light"]
    return (_rule(look, "dark", theme.tokens["dark"])
            + "@media (prefers-color-scheme: light) {\n  "
            + _rule(f'{look}:not([data-theme="dark"])', "light", light) + "}\n"
            + _rule(f'{look}[data-theme="light"]', "light", light))


def look_css(theme: Theme) -> str:
    """Everything a built-in theme adds to the page's styles, as one stylesheet: its token blocks, its skin, then its card
    views' stylesheets, every rule scoped to the theme (js/theme-boot.js links it after the page's own stylesheets).
    "" for a theme with none (the default)."""
    files = ((theme.skin,) if theme.skin else ()) + theme.view_css
    body = theme_css(theme) + "".join(
        f"/* {css} */\n{(STATIC_DIR / css).read_text(encoding='utf-8').rstrip()}\n" for css in files)
    return f"/* generated from static/themes/{theme.id}/ (pengumon.web.themes) */\n{body}" if body else ""


@lru_cache(maxsize=1)
def look_stylesheets() -> Dict[str, str]:
    """theme id -> its stylesheet (look_css), for the built-in themes that have one; served at
    /static/v/<tree>/themes/<id>/theme.css, so a skin's url() to a file of its folder still resolves there."""
    return {t.id: css for t in builtin_themes() if (css := look_css(t))}


def custom_css(every: List[Theme]) -> str:
    """The custom themes' token blocks (of `every`, all_themes' list), written into the page's <style> as they are:
    their ids and values passed check_theme (an id, colors and sizes), which leaves no way to end the element or the
    rule."""
    css = "".join(theme_css(t) for t in every if not t.builtin)
    return "" if "<" in css else css  # cannot happen (checked values); a belt for the <style>


# ---------- what the page and the API get ----------

def page_list(themes: List[Theme], version: str) -> List[Dict[str, Any]]:
    """The short list the page starts with (<html data-looks>): enough to apply a theme before the first paint (its
    stylesheet, when it has one), to name the themes in the header menu, and to find a theme's card views and chart
    style (only when it has them)."""
    out = []
    for t in themes:
        entry = {"id": t.id, "name": t.name, "modes": list(t.modes), "custom": not t.builtin,
                 "effect": f"/static/v/{version}/{t.effect_module}" if t.effect else None,
                 "effect_on": bool(t.effect and t.effect["default_on"])}
        if t.builtin and t.id in look_stylesheets():  # its stylesheet (look_css), linked only while it is in use
            entry["css"] = f"/static/v/{version}/themes/{t.id}/theme.css"
        if t.views:  # the cards it draws a view of, by URL (js/cards.js loads the current theme's)
            entry["views"] = {p: f"/static/v/{version}/{t.view_module(p)}" for p in t.views}
        if t.chart:  # its chart style (js/chart-theme.js)
            entry["chart"] = dict(t.chart)
        out.append(entry)
    return out


def api_entry(theme: Theme, version: str) -> Dict[str, Any]:
    """One theme for GET /api/themes: the page list's fields, the description and tags, and the theme as an
    importable theme.json (`theme`: its colors per mode, for Export, Duplicate and a card's colors)."""
    entry = page_list([theme], version)[0]
    return {**entry, "description": theme.description, "tags": list(theme.tags), "skin": bool(theme.skin),
            "theme": theme.exported()}


def resolve_default(theme_id: str, every: List[Theme]) -> str:
    """`theme_id` when a theme of `every` (all_themes' list) has it, else the default theme's id."""
    return theme_id if any(t.id == theme_id for t in every) else DEFAULT_ID
