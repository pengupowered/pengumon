"""Themes (docs/themes.md): every theme folder passes the rules every theme must hold, custom themes are checked
before they reach the page, and the page applies the default theme before its first paint.

A new theme folder is tested here without a new test: `test_every_theme_folder` runs each one through the checks."""

import json
import re
from html.parser import HTMLParser

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from pengumon.config import AppConfig
from pengumon.main import app
from pengumon.state import STATE_ATTR, AppState
from pengumon.web import theme_checks, themes
from pengumon.web.assets import ASSET_VERSION
from pengumon.web.settings import DisplayConfig, ThemeItem

FOLDERS = sorted(p for p in themes.THEMES_DIR.iterdir() if p.is_dir())


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _nord() -> themes.Theme:
    return next(t for t in themes.builtin_themes() if t.id == "nord")


def _custom(**over) -> dict:
    """A custom theme as Settings' Duplicate makes one (from Nord)."""
    return {**_nord().exported(), "id": "custom-test", "name": "Test", **over}


# ---------- every theme folder ----------

@pytest.mark.parametrize("folder", FOLDERS, ids=[f.name for f in FOLDERS])
def test_every_theme_folder(folder):
    """The folder's theme.json, skin and effect follow the contract, and every mode passes the checks: text contrast,
    status colors apart, the series colors apart (also under deuteranopia and protanopia)."""
    theme, problems = themes.check_theme(json.loads((folder / "theme.json").read_text(encoding="utf-8")), custom=False,
                                         folder=folder)
    assert problems == []
    assert theme.id in themes.builtin_ids()  # discovered (a folder that fails discovery is left out of the page)
    if theme.skin:
        assert themes.skin_problems(theme.id, (themes.STATIC_DIR / theme.skin).read_text(encoding="utf-8")) == []
    if theme.effect:
        code = (themes.STATIC_DIR / theme.effect_module).read_text(encoding="utf-8")
        assert re.search(r"export default\s*\{[\s\S]*\bstart\s*\(", code), "default export {start(layer, ctx)}"
        assert "requestAnimationFrame" not in code and "setInterval" not in code, "effects draw in ctx.onFrame only"
    assert themes.view_problems(theme.id, folder) == []  # its card views (panels/), if any
    assert theme_checks.problems(theme) == []


def test_the_default_theme_is_tokens_css():
    default = themes.builtin_themes()[0]
    assert default.id == "default" and default.name == "Pengumon" and default.modes == ("dark", "light")
    assert default.resolved("dark")["--bg"] == "#0a0e13" and default.resolved("light")["--bg"] == "#f1f4f7"
    assert themes.theme_css(default) == ""  # nothing over tokens.css


def test_the_checks_catch_each_rule():
    """Each kind of check fails on a palette made to break it (so a theme that passes passed something)."""
    tokens = _nord().resolved("dark")
    assert theme_checks.mode_problems(tokens) == []
    broken = {
        "text": {"--ink-3": tokens["--surface-2"]},
        "on accent": {"--on-accent": tokens["--accent"]},
        "status": {"--warn": tokens["--good"]},
        "series": {"--s2": tokens["--s1"]},
        # red and green that only color-blind eyes confuse: apart for normal vision, together under deuteranopia
        "color blind": {"--s1": "#c8553d", "--s2": "#6f9a3b"},
    }
    found = {k: theme_checks.mode_problems({**tokens, **v}) for k, v in broken.items()}
    assert any("--ink-3 on" in p for p in found["text"])
    assert any("--on-accent on --accent" in p for p in found["on accent"])
    assert any("--good and --warn" in p for p in found["status"])
    assert any("--s1 and --s2 are" in p and "under" not in p for p in found["series"])
    assert any("under deuteranopia" in p for p in found["color blind"])
    assert not any("--s1 and --s2 are" in p and "under" not in p for p in found["color blind"])


# ---------- custom themes (Settings > Display) ----------

def test_a_duplicated_theme_is_a_valid_custom_theme():
    item = ThemeItem.model_validate(_custom())
    assert item.id == "custom-test" and theme_checks.problems(themes.custom_themes([item])[0]) == []


@pytest.mark.parametrize("change, words", [
    ({"tokens": {"dark": {**_nord().exported()["tokens"]["dark"], "--bg": "red; } body { display: none"}, "light": _nord().exported()["tokens"]["light"]}}, "--bg: must be a color"),
    ({"tokens": {"dark": {**_nord().exported()["tokens"]["dark"], "--accent-bg": "url(https://example.com/x)"}, "light": _nord().exported()["tokens"]["light"]}}, "--accent-bg: must be a color"),
    ({"tokens": {"dark": {**_nord().exported()["tokens"]["dark"], "--font": "Georgia"}, "light": _nord().exported()["tokens"]["light"]}}, "colors and text sizes and corner radii only"),
    ({"tokens": {"dark": {**_nord().exported()["tokens"]["dark"], "--made-up": "#000000"}, "light": _nord().exported()["tokens"]["light"]}}, "--made-up: not a theme token"),
    ({"tokens": {"dark": {**_nord().exported()["tokens"]["dark"], "--fs-md": "90px"}, "light": _nord().exported()["tokens"]["light"]}}, "--fs-md: must be between"),
    ({"tokens": {"dark": {"--bg": "#000000"}, "light": _nord().exported()["tokens"]["light"]}}, "missing colors"),
    ({"id": "nord"}, "starts with 'custom-'"),
    ({"id": 'custom-x"]{}'}, "id: lower-case letters"),
])
def test_a_custom_theme_takes_known_colors_and_sizes_only(change, words):
    """Its values go into the page's <style> as they are: anything but a checked color or size is refused."""
    with pytest.raises(ValidationError) as e:
        ThemeItem.model_validate(_custom(**change))
    assert words in str(e.value)


@pytest.mark.parametrize("theme", themes.builtin_themes(), ids=lambda t: t.id)
def test_every_theme_exports_as_a_custom_theme_as_it_is(theme):
    """Export's file is a custom theme once it has a custom id: nothing in it to strip first."""
    item = ThemeItem.model_validate({**theme.exported(), "id": f"custom-{theme.id}"})
    assert item.model_dump() == {**theme.exported(), "id": f"custom-{theme.id}"}


def test_a_theme_id_with_a_line_break_is_refused():
    """The id goes into the page's <style> as a selector: `$` would have let a trailing line break through."""
    with pytest.raises(ValidationError, match="id: lower-case letters"):
        ThemeItem.model_validate(_custom(id="custom-test\n"))
    assert "effect: null or" in " ".join(themes.check_theme({**_nord().exported(), "effect": {"id": "rain\n"}},
                                                             custom=False)[1])


def test_a_theme_or_card_color_with_a_line_break_is_refused():
    from pengumon.web.settings import CardColors
    with pytest.raises(ValidationError, match="a theme id"):
        DisplayConfig(theme="nord\n")
    with pytest.raises(ValidationError, match="a theme id"):
        CardColors(theme="nord\n")
    with pytest.raises(ValidationError, match="a color like"):
        CardColors(custom={"--accent": "#112233\n"})
    assert CardColors(theme="nord", custom={"--accent": "#112233"}).custom == {"--accent": "#112233"}


@pytest.mark.asyncio
async def test_each_custom_theme_is_checked_once_per_page_and_per_api_answer(monkeypatch):
    from pengumon.web.pages import page_theme
    display = DisplayConfig(theme="custom-test", themes=[ThemeItem.model_validate(_custom()),
                                                         ThemeItem.model_validate(_custom(id="custom-two"))])
    themes.builtin_themes()  # discovered once at start (cached), not per request
    checked = []
    check = themes.check_theme
    monkeypatch.setattr(themes, "check_theme", lambda raw, **kw: checked.append(raw["id"]) or check(raw, **kw))
    page = page_theme(display)
    assert page["look"] == "custom-test" and 'data-look="custom-two"' in page["custom_css"]
    assert sorted(checked) == ["custom-test", "custom-two"]
    checked.clear()
    setattr(app.state, STATE_ATTR, AppState(config=AppConfig(display=display)))
    try:
        async with client() as ac:
            assert (await ac.get("/api/themes")).json()["default"] == "custom-test"
    finally:
        delattr(app.state, STATE_ATTR)
    assert sorted(checked) == ["custom-test", "custom-two"]


def test_custom_themes_are_written_into_the_page_scoped_to_their_id():
    css = themes.custom_css(themes.all_themes([ThemeItem.model_validate(_custom())]))
    assert css.startswith(':root[data-look="custom-test"] { color-scheme: dark;')
    assert ':root[data-look="custom-test"][data-theme="light"] { color-scheme: light;' in css
    assert "<" not in css and css.count("{") == css.count("}")


# ---------- skins ----------

def test_a_skin_may_style_its_own_theme_only():
    ok = ':root[data-look="x"] .panel { border: 0; }\n@media (max-width: 720px) { :root[data-look="x"] .btn { gap: 2px; } }'
    assert themes.skin_problems("x", ok) == []
    assert themes.skin_problems("x", ".panel { border: 0; }")  # every page, every theme
    assert themes.skin_problems("x", ':root[data-look="y"] .panel { border: 0; }')  # another theme
    assert themes.skin_problems("x", ':root[data-look="x"] .a, .b { border: 0; }')  # the second selector
    assert themes.skin_problems("x", '@media screen { .b { border: 0; } }')
    assert themes.skin_problems("x", '@import url(x.css);')
    assert themes.skin_problems("x", ':root[data-look="x"] body { background: url(https://example.com/a.png); }')


# ---------- views of cards and chart style ----------

def test_a_theme_view_draws_a_card_the_page_has_and_only_draws(tmp_path):
    """panels/<panel id>.js (+ .css) is a view of a card that exists, with a default export {mount}; it fetches, keeps
    time and stores nothing (the model does); its stylesheet is scoped like a skin and has no url(). A stylesheet alone
    restyles the card's standard view."""
    folder = tmp_path / "x"
    (folder / "panels").mkdir(parents=True)
    view = "export default {\n  mount(root, ctx) { return { update() {}, unmount() {} }; }\n};\n"
    (folder / "panels" / "services.js").write_text(view)
    (folder / "panels" / "services.css").write_text(':root[data-look="x"] [data-panel="services"] [data-view="x"] .a { color: var(--accent); }')
    (folder / "panels" / "strips.css").write_text(':root[data-look="x"] [data-panel="strips"] .s-pct { color: var(--accent); }')
    assert themes.view_problems("x", folder) == []
    (folder / "panels" / "nosuchcard.js").write_text(view)
    (folder / "panels" / "latency.js").write_text("export default { draw() {} };")
    (folder / "panels" / "diagnosis.js").write_text(view.replace("return", "fetch('/api/x'); setInterval(() => {}, 9); return"))
    (folder / "panels" / "matrix.css").write_text(".a { color: red; }")
    (folder / "panels" / "heat.js").write_text(view)
    (folder / "panels" / "heat.css").write_text(':root[data-look="x"] [data-panel="heat"] .a { background: url(a.png); }')
    (folder / "panels" / "report.css").write_text(':root[data-look="x"] .a { color: red; }\n:root[data-look="x"] [data-panel="heat"] .b { color: red; }')  # on the theme, not on its card
    found = themes.view_problems("x", folder)
    assert any("panels/nosuchcard.js: there is no card 'nosuchcard'" in p for p in found)
    assert any("panels/latency.js: the default export is {mount(root, ctx)}" in p for p in found)
    assert any("panels/diagnosis.js: a view does not use fetch" in p for p in found)
    assert any("panels/diagnosis.js: a view does not use setInterval" in p for p in found)
    assert any("panels/matrix.css: '.a' is not under" in p for p in found)
    assert any("panels/heat.css: no url()" in p for p in found)
    assert any("panels/report.css: ':root[data-look=\"x\"] .a' is not under :root[data-look=\"x\"] [data-panel=\"report\"]" in p for p in found)
    assert any("panels/report.css: ':root[data-look=\"x\"] [data-panel=\"heat\"] .b' is not under" in p for p in found)  # another card's
    assert not any(p.startswith(("panels/services.", "panels/strips.")) for p in found)


def test_chart_style_takes_known_fields_in_range_and_never_from_a_custom_theme():
    raw = _nord().exported()
    ok, problems = themes.check_theme({**raw, "chart": {"line_width": 3, "curve": "stepped", "grid": False, "points": 2, "fill": True}}, custom=False)
    assert problems == [] and ok.chart == {"line_width": 3, "curve": "stepped", "grid": False, "points": 2, "fill": True}
    _, problems = themes.check_theme({**raw, "chart": {"line_width": 9, "curve": "wavy", "grid": "no", "glow": 1}}, custom=False)
    assert problems == ["chart.line_width: a number from 0.5 to 6", "chart.curve: one of smooth, straight, stepped",
                        "chart.grid: true or false", "chart: unknown field 'glow'"]
    _, problems = themes.check_theme({**_custom(), "chart": {"line_width": 3}}, custom=True)
    assert problems == ["chart: a custom theme has no chart style"]
    assert "chart" not in next(t for t in themes.builtin_themes() if t.id == "starship").exported()  # Export: colors only


# ---------- the page ----------

@pytest.mark.asyncio
async def test_each_theme_has_one_stylesheet_generated_and_served_under_the_tree_hash():
    async with client() as ac:
        nord = await ac.get(f"/static/v/{ASSET_VERSION}/themes/nord/theme.css")
        starship = await ac.get(f"/static/v/{ASSET_VERSION}/themes/starship/theme.css")
        stale = await ac.get("/static/v/0000000000ab/themes/nord/theme.css")
        default = await ac.get(f"/static/v/{ASSET_VERSION}/themes/default/theme.css")  # tokens.css is the default
    assert nord.status_code == 200 and nord.headers["content-type"].startswith("text/css")
    assert nord.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert stale.status_code == 404 and default.status_code == 404
    assert ':root[data-look="nord"] { color-scheme: dark; --bg: #242933;' in nord.text
    assert '@media (prefers-color-scheme: light) {\n  :root[data-look="nord"]:not([data-theme="dark"]) { color-scheme: light;' in nord.text
    # a theme's tokens, then its skin, then its card views' styles; nothing of another theme
    text = starship.text
    assert text.index(':root[data-look="starship"] { color-scheme:') < text.index("/* themes/starship/skin.css */") \
        < text.index("/* themes/starship/panels/services.css */")
    assert themes.view_problems("starship", themes.THEMES_DIR / "starship") == []
    for theme_id, css in (("nord", nord.text), ("starship", text)):
        for prelude in (p.strip() for p in re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", css, flags=re.S))):
            if not re.match(r"@|from\b|to\b|\d", prelude):  # at-rules and keyframe steps
                assert prelude.startswith(f':root[data-look="{theme_id}"]'), prelude


@pytest.mark.asyncio
async def test_the_page_starts_with_the_default_theme_from_settings_before_its_first_paint():
    from html import unescape
    custom = ThemeItem.model_validate(_custom())
    config = AppConfig(display=DisplayConfig(theme="digital-rain", effects=False, themes=[custom]))
    setattr(app.state, STATE_ATTR, AppState(config=config))
    try:
        async with client() as ac:
            html = (await ac.get("/")).text
            api = (await ac.get("/api/themes")).json()
    finally:
        delattr(app.state, STATE_ATTR)
    root = re.search(r"<html\b[^>]*>", html).group(0)
    assert 'data-look="digital-rain"' in root and 'data-look-default="digital-rain"' in root
    assert 'data-theme="dark"' in root  # a theme with one look forces it
    assert 'data-effects="off"' in root
    looks = json.loads(unescape(re.search(r"data-looks='([^']*)'", root).group(1)))
    assert [t["id"] for t in looks][:1] == ["default"] and {"nord", "digital-rain", "custom-test"} <= {t["id"] for t in looks}
    rain = next(t for t in looks if t["id"] == "digital-rain")
    assert rain["effect"] == f"/static/v/{ASSET_VERSION}/themes/digital-rain/effect.js" and rain["modes"] == ["dark"]
    # a theme's card views and chart style, for js/cards.js and js/chart-theme.js (only the themes that have them)
    amber = next(t for t in looks if t["id"] == "amber-crt")
    assert amber["views"]["incidents"] == f"/static/v/{ASSET_VERSION}/themes/amber-crt/panels/incidents.js"
    starship = next(t for t in looks if t["id"] == "starship")  # restyles every card (panels/*.css alone): no views
    assert "views" not in starship and starship["chart"]["curve"] == "straight"
    assert "views" not in next(t for t in looks if t["id"] == "nord") and "chart" not in next(t for t in looks if t["id"] == "nord")
    # each built-in theme's one stylesheet, which theme-boot.js links for the theme in use (none for the default and the
    # custom themes: tokens.css and the page's <style>)
    assert rain["css"] == f"/static/v/{ASSET_VERSION}/themes/digital-rain/theme.css"
    assert "css" not in looks[0] and "css" not in next(t for t in looks if t["id"] == "custom-test")
    head = html.split("</head>", 1)[0]
    # the page links no theme's styles itself, and the boot script comes after its own stylesheets (it links the theme's
    # stylesheet where it stands)
    assert "themes/" not in "".join(re.findall(r"<link\b[^>]*>", head)) and "views.css" not in head
    assert head.index("tokens.css") < head.index('id="customThemeCss"') < head.rindex('rel="stylesheet"') < head.index("js/theme-boot.js")
    assert ':root[data-look="custom-test"]' in head.split('id="customThemeCss">', 1)[1].split("</style>", 1)[0]
    assert api["default"] == "digital-rain" and api["effects"] is False
    assert api["looks"] == looks  # js/themes.js replaces its list with this one: the same function builds both
    entry = next(t for t in api["themes"] if t["id"] == "custom-test")
    assert entry["custom"] is True and entry["theme"]["tokens"]["dark"]["--bg"] == "#242933"


def test_theme_boot_links_every_theme_file_the_list_names_and_nothing_else():
    # js/theme-boot.js links a stylesheet and preloads views only from our own theme folders: every URL the list holds
    # must pass its check (or that theme would quietly lose its looks), and a URL from anywhere else must not
    source = (themes.THEMES_DIR.parent / "js" / "theme-boot.js").read_text()
    ours = re.compile(re.search(r"var ours = function \(url\) \{\s*return typeof url === 'string' && /(.+)/\.test\(url\);",
                                source).group(1).replace(r"\/", "/"))
    urls = [u for t in themes.page_list(themes.all_themes(), ASSET_VERSION)
            for u in [t.get("css"), *t.get("views", {}).values()] if u]
    assert len(urls) > 3 and all(ours.fullmatch(u) for u in urls)
    for foreign in ("https://example.com/themes/x/theme.css", "//example.com/static/v/1/themes/x/theme.css",
                    "javascript:alert(1)//.js", f"/static/v/{ASSET_VERSION}/themes/x/../../js/main.js",
                    f"/static/v/{ASSET_VERSION}/js/main.js", f'/static/v/{ASSET_VERSION}/themes/x/"><b>.css'):
        assert not ours.fullmatch(foreign), foreign


@pytest.mark.asyncio
async def test_a_default_theme_that_is_gone_shows_the_standard_one():
    setattr(app.state, STATE_ATTR, AppState(config=AppConfig(display=DisplayConfig(theme="custom-removed"))))
    try:
        async with client() as ac:
            html = (await ac.get("/")).text
    finally:
        delattr(app.state, STATE_ATTR)
    root = re.search(r"<html\b[^>]*>", html).group(0)
    assert 'data-look="default"' in root and "data-theme" not in root


# ---------- page hooks a skin may rely on (docs/themes.md) ----------

class _Tree(HTMLParser):
    """The page as a tree of {tag, cls, attrs, kids} (a template's content as its children, as the browser clones it)."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.root = {"tag": "#root", "cls": set(), "attrs": {}, "kids": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "cls": set((dict(attrs).get("class") or "").split()), "attrs": dict(attrs), "kids": []}
        self.stack[-1]["kids"].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        while len(self.stack) > 1 and self.stack.pop()["tag"] != tag:
            pass


def _all(node, test):
    for kid in node["kids"]:
        if test(kid):
            yield kid
        yield from _all(kid, test)


def _has(cls, tag=None):
    return lambda n: cls in n["cls"] and (tag is None or n["tag"] == tag)


@pytest.mark.asyncio
async def test_the_page_keeps_the_hooks_a_skin_relies_on():
    """docs/themes.md "Page hooks a skin may rely on": the Starship frame (and any skin) styles these parts of the page
    and of every card and dialog; renaming or moving one breaks the frame, so it fails here first."""
    async with client() as ac:
        html = (await ac.get("/")).text
    tree = _Tree()
    tree.feed(html)
    page = tree.root
    topbar = [n for n in _all(page, _has("topbar", "header"))]
    assert len(topbar) == 1 and any("tb-inner" in k["cls"] for k in topbar[0]["kids"])
    assert list(_all(page, _has("fx-layer"))) and list(_all(page, _has("glow"))) and list(_all(page, _has("tabbar", "nav")))
    main = [n for n in _all(page, _has("page", "main"))]
    assert len(main) == 1 and main[0]["kids"]
    cards = 0
    for section in main[0]["kids"]:
        assert section["tag"] == "section" and "data-panel" in section["attrs"], section["tag"]
        for card in (c for k in section["kids"] for c in ([k] if k["tag"] != "template" else k["kids"])):
            if "panel" not in card["cls"]:
                continue
            cards += 1
            kids = card["kids"]
            assert card["tag"] == "article" and kids[0]["tag"] == "header" and "panel-h" in kids[0]["cls"], section["attrs"]
            assert list(_all(kids[0], _has("panel-t"))), section["attrs"]
            assert any("panel-b" in k["cls"] for k in kids), section["attrs"]
            # the foot, the body and the header are the card's own children, never nested deeper
            assert not list(_all(card, lambda n: all(n is not k for k in kids) and n["cls"] & {"panel-h", "panel-b", "panel-f"})), section["attrs"]
    assert cards >= len(main[0]["kids"])
    dialogs = [n for n in _all(page, lambda n: "modal-content" in n["cls"] or (n["tag"] == "dialog" and "modal" in n["cls"]))]
    assert dialogs
    for dialog in dialogs:
        head = [n for n in _all(dialog, _has("modal-h"))]
        assert len(head) == 1 and list(_all(head[0], _has("modal-t"))), dialog["attrs"]
        assert all(any(c is k for k in head[0]["kids"]) for c in _all(dialog, _has("m-close"))), dialog["attrs"]
    # every card's header gets its tools box with the card's menu button last (js/layout.js addMenuButton)
    layout = (themes.THEMES_DIR.parent / "js" / "layout.js").read_text(encoding="utf-8")
    assert "className: 'panel-tools'" in layout and "'btn icon sm panel-menu-btn'" in layout and "tools.appendChild(b)" in layout
