"""The dashboard ships no inline scripts or handlers, so the CSP can forbid them; assets are cache-busted ES modules."""

import re
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from pengumon.main import app
from pengumon.web.assets import ASSET_VERSION

WEB = Path(__file__).resolve().parents[1] / "src" / "pengumon" / "web"
STATIC = WEB / "static"
JS = STATIC / "js"  # the core and the shared library
# every module: the core and shared library, and each panel's and dialog's folder (pengumon.ui)
JS_ROOTS = (JS, STATIC / "panels", STATIC / "dialogs")
SHARED_CSS = ("tokens.css", "components.css", "page.css", "style.css")


def all_js() -> list[Path]:
    return sorted(p for root in JS_ROOTS for p in root.rglob("*.js"))
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
# the event each delegated attribute listens for (dispatch.js)
ACTION_KINDS = {"action": "click", "input": "input", "change": "change", "submit": "submit"}


def js_files() -> list[str]:
    """Every module (core, shared, panel and dialog folders), as paths relative to the web folder."""
    return [p.relative_to(WEB).as_posix() for p in all_js()]


def html_files() -> list[str]:
    """The page, its partials and every panel's and dialog's markup, as paths relative to the web folder."""
    roots = (WEB / "templates", STATIC / "panels", STATIC / "dialogs")
    return sorted(p.relative_to(WEB).as_posix() for root in roots for p in root.rglob("*.html"))


def template_text() -> str:
    """The markup of the page and all its partials, as one text."""
    return "\n".join((WEB / p).read_text(encoding="utf-8") for p in html_files())


@pytest.mark.parametrize("path", [*html_files(), *js_files()])
def test_no_inline_event_handler_attributes(path):
    text = (WEB / path).read_text(encoding="utf-8")
    assert INLINE_HANDLER.findall(text) == []
    assert "javascript:" not in text


def test_template_has_no_inline_script_blocks():
    assert not re.search(r"<script(?![^>]*\bsrc=)", template_text(), re.IGNORECASE)


@pytest.mark.asyncio
async def test_csp_forbids_inline_script_and_external_connect():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        csp = (await ac.get("/")).headers["content-security-policy"]
    directives = {d.strip().split(" ", 1)[0]: d.strip().split(" ", 1)[1:] for d in csp.split(";") if d.strip()}
    assert directives["script-src"] == ["'self'"]
    assert directives["connect-src"] == ["'self'"]
    assert "'unsafe-inline'" in directives["style-src"][0]
    # the dashboard has no <object>, <base> or foreign forms, so they are shut off
    assert directives["object-src"] == ["'none'"] and directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'self'"] and directives["frame-ancestors"] == ["'self'"]


@pytest.mark.asyncio
async def test_asset_urls_carry_the_tree_hash_and_cache_only_when_versioned():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
        version = re.search(r"/static/v/([0-9a-f]{12})/style\.css", html).group(1)
        assert version == ASSET_VERSION
        for rel in ("style.css", "js/main.js"):
            assert f"/static/v/{version}/{rel}" in html
            versioned = await ac.get(f"/static/v/{version}/{rel}")
            assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
            assert "max-age" not in (await ac.get(f"/static/{rel}")).headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_the_app_has_a_favicon_that_needs_no_sign_in():
    """Browsers ask for /favicon.ico on every visit (it used to 404); the page names the SVG mark as well."""
    from pengumon.security import PUBLIC_PATHS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        icon = await ac.get("/favicon.ico")
        html = (await ac.get("/")).text
        svg = await ac.get(f"/static/v/{ASSET_VERSION}/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/x-icon"
    assert icon.content[:4] == b"\x00\x00\x01\x00" and b"\x89PNG" in icon.content  # an ICO holding PNGs
    assert f'href="/static/v/{ASSET_VERSION}/favicon.svg"' in html and 'href="/favicon.ico"' in html
    assert svg.status_code == 200 and "svg" in svg.headers["content-type"]
    assert "/favicon.ico" in PUBLIC_PATHS  # reachable from the sign-in page too


@pytest.mark.asyncio
async def test_the_app_can_be_added_to_a_home_screen():
    """The phone home-screen icon and the web app manifest, whose icons resolve next to it."""
    import json
    base = f"/static/v/{ASSET_VERSION}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
        manifest = await ac.get(f"{base}/manifest.webmanifest")
        icons = [await ac.get(f"{base}/{i['src']}") for i in json.loads(manifest.text)["icons"]]
        touch = await ac.get(f"{base}/icons/app-180.png")
    assert f'rel="apple-touch-icon" href="{base}/icons/app-180.png"' in html
    assert f'rel="manifest" href="{base}/manifest.webmanifest"' in html
    assert manifest.status_code == 200 and json.loads(manifest.text)["start_url"] == "/"
    assert all(r.status_code == 200 and r.content[:4] == b"\x89PNG" for r in icons + [touch])


@pytest.mark.asyncio
async def test_dashboard_script_is_an_es_module_under_the_tree_hash():
    assert not (WEB / "static" / "app.js").exists()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
        scripts = re.findall(r"<script\b[^>]*>", html)
        # Chart.js stays a classic script (it defines the global `Chart`), then the one module entry point
        assert scripts == [f'<script src="/static/v/{ASSET_VERSION}/vendor/chart.umd.min.js">',
                           f'<script type="module" src="/static/v/{ASSET_VERSION}/js/main.js">']
        assert "app.js" not in html
        # every module's imports resolve to a shipped file, served as JavaScript (a wrong MIME type blocks a module)
        entries = [("main.js", await ac.get(f"/static/v/{ASSET_VERSION}/js/main.js"))]
        for path in all_js():
            for imported in re.findall(r"(?:from|import) '(\.{1,2}/[\w./-]+\.js)'", path.read_text(encoding="utf-8")):
                assert (path.parent / imported).resolve().is_file(), f"{path.relative_to(STATIC)} imports the missing {imported}"
        for name in sorted(p.relative_to(STATIC).as_posix() for p in all_js()):
            entries.append((name, await ac.get(f"/static/v/{ASSET_VERSION}/{name}")))
        for name, response in entries:
            assert response.status_code == 200 and "javascript" in response.headers["content-type"], name


def registered_actions() -> dict[str, set[str]]:
    """Event kind -> action names in the registerActions(kind, {...}) maps of the modules."""
    found: dict[str, set[str]] = {kind: set() for kind in ACTION_KINDS.values()}
    for path in all_js():
        text = path.read_text(encoding="utf-8")
        for kind, body in re.findall(r"registerActions\('(\w+)', \{(.*?)\}\);", text, re.DOTALL):
            found[kind].update(re.findall(r"(?:^|\{)\s*(\w+)\s*:\s*\(", body, re.MULTILINE))
    return found


def used_actions() -> dict[str, dict[str, set[str]]]:
    """Source -> event kind -> action names the markup can ask for: data-action="x" attributes in the template and
    in HTML strings built by the modules, and dataset.action = 'x' / setAttribute('data-action', 'x') in code."""
    sources = {f: (WEB / f).read_text(encoding="utf-8") for f in [*html_files(), *js_files()]}
    used: dict[str, dict[str, set[str]]] = {}
    for source, text in sources.items():
        names: dict[str, set[str]] = {kind: set() for kind in ACTION_KINDS.values()}
        for attr, name in re.findall(r"""\bdata-(action|input|change|submit)\s*=\s*["'](\w+)["']""", text):
            names[ACTION_KINDS[attr]].add(name)
        for attr, name in re.findall(r"""\bdataset\.(action|input|change|submit)\s*=\s*["'](\w+)["']""", text):
            names[ACTION_KINDS[attr]].add(name)
        for attr, name in re.findall(r"""setAttribute\(\s*["']data-(action|input|change|submit)["']\s*,\s*["'](\w+)["']""", text):
            names[ACTION_KINDS[attr]].add(name)
        used[source] = names
    return used


def test_every_delegated_action_in_the_markup_is_registered():
    registered = registered_actions()
    assert all(registered.values()), registered  # the parser found a map for each event
    used = used_actions()
    # the template and the incident table rows really do use the attributes this test looks for
    assert used["templates/index.html"]["click"] and used["static/panels/incidents/incidents.js"]["click"] == {"inspectIncident"}
    for source, kinds in used.items():
        for kind, names in kinds.items():
            assert names <= registered[kind], f"{source}: no {kind} action registered for {sorted(names - registered[kind])}"


# Element ids the code looks up (getElementById, openModal/closeModal, data-arg="...Modal") that the template does not declare and the JS does not build itself.
# Keep this empty unless an id is genuinely dynamic (and say why next to the entry).
DYNAMIC_ID_ALLOWLIST: set[str] = set()


def test_every_getelementbyid_literal_exists_in_the_template_or_is_built_by_the_js():
    """A renamed or misspelled id makes getElementById return null and the handler throws (the modem form once read
    modemUser/modemPass while the template had modemUsername/modemPassword)."""
    html = template_text()
    known = set(re.findall(r"""\bid\s*=\s*["']([^"'$]+)["']""", html))
    assert {"setupUsername", "setupPassword"} <= known  # the scan sees the template's ids
    lookups: dict[str, set[str]] = {}
    for path in all_js():
        text = path.read_text(encoding="utf-8")
        # ids the module builds: id="x" in HTML strings, el.id = 'x', setAttribute('id', 'x')
        known |= set(re.findall(r"""\bid\s*=\s*\\?["']([\w-]+)\\?["']""", text))
        known |= set(re.findall(r"""\.id\s*=\s*["']([\w-]+)["']""", text))
        known |= set(re.findall(r"""setAttribute\(\s*["']id["']\s*,\s*["']([\w-]+)["']""", text))
        for name in re.findall(r"""getElementById\(\s*["']([^"']+)["']\s*\)""", text):
            lookups.setdefault(name, set()).add(path.relative_to(STATIC).as_posix())
        # openModal / closeModal look their modal up by id too
        for name in re.findall(r"""(?:open|close)Modal\(\s*["']([^"']+)["']\s*\)""", text):
            lookups.setdefault(name, set()).add(path.relative_to(STATIC).as_posix())
        for name in re.findall(r"""data-arg\s*=\s*["'](\w+Modal)["']""", text):
            lookups.setdefault(name, set()).add(path.relative_to(STATIC).as_posix())
    for name in re.findall(r"""data-arg\s*=\s*["'](\w+Modal)["']""", html):  # buttons in the template
        lookups.setdefault(name, set()).add("index.html")
    assert len(lookups) > 50  # the scan finds the lookups
    assert {"deviceSetupModal", "incidentInspectorModal"} <= set(lookups)  # including the modal ones
    missing = {name: sorted(files) for name, files in lookups.items() if name not in known | DYNAMIC_ID_ALLOWLIST}
    assert not missing, f"getElementById ids that exist nowhere: {missing}"


# --- nothing of the owner's setup is baked into the dashboard ---------------------------------------------------------

VENDOR_OR_FIXED_HOST = re.compile(r"xfinity|asus|zenwifi|comcast|10\.0\.0\.1|192\.168\.50", re.IGNORECASE)


@pytest.mark.parametrize("path", [*html_files(), *js_files()])
def test_the_dashboard_names_no_vendor_and_no_fixed_host(path):
    """Vendor names come from config (line.isp_name, the targets' names), hosts and models from /api/devices."""
    text = (WEB / path).read_text(encoding="utf-8")
    assert VENDOR_OR_FIXED_HOST.findall(text) == []


def test_util_js_has_no_literal_target_ids():
    text = (JS / "util.js").read_text(encoding="utf-8")
    assert re.search(r"_dns|_router|_gateway|_rur|_ibone", text) is None
    assert "canonicalTargetOrder = []" in text


def test_the_dashboard_no_longer_calls_the_removed_endpoints():
    for path in js_files():
        assert not re.search(r"/api/(modem|router)/", (WEB / path).read_text(encoding="utf-8")), path


# --- metric help popovers (help.js) and the mobile tab bar (tabs.js) -------------------------------------------------

HELP_KEY = re.compile(r"""data-help=["']([A-Za-z0-9_.]+)["']|dataset\.help\s*=\s*["']([A-Za-z0-9_.]+)["']""")
HELP_MAP_KEY = re.compile(r"^  '([A-Za-z0-9_.]+)':", re.MULTILINE)


def _help_texts() -> str:
    """Every registerHelp({...}) block: each panel registers its own texts (its folder's help.js)."""
    return "\n".join(body for p in all_js() for body in re.findall(r"registerHelp\(\{\n(.*?)\n\}\);", p.read_text(encoding="utf-8"), re.DOTALL))


def _help_map_keys() -> list[str]:
    return HELP_MAP_KEY.findall(_help_texts())


def _used_help_keys() -> dict[str, set[str]]:
    used: dict[str, set[str]] = {}
    for path in [*html_files(), *js_files()]:
        if path.endswith("/help.js"):
            continue
        keys = {a or b for a, b in HELP_KEY.findall((WEB / path).read_text(encoding="utf-8"))}
        if keys:
            used[path] = keys
    return used


def test_every_data_help_key_exists_in_the_help_map():
    known = set(_help_map_keys())
    assert known, "help.js map not found"
    used = _used_help_keys()
    assert "static/panels/matrix/panel.html" in used
    for path, keys in used.items():
        assert keys <= known, f"{path} uses help keys missing from help.js: {sorted(keys - known)}"


def test_help_map_has_no_duplicates_and_every_entry_is_used():
    keys = _help_map_keys()
    assert len(keys) == len(set(keys))
    used = set().union(*_used_help_keys().values())
    assert set(keys) - used == set(), "help texts nothing points at"


# What each panel's help.js must explain, and the code constants whose numbers its texts quote. A panel with a help.js
# that is missing here fails test_every_help_file_is_listed, so a new panel states its keys when it is added.
PANEL_HELP = {
    "matrix": {"keys": {"hop.category", "hop.loss", "hop.latency", "hop.jitter"}, "thresholds": lambda: ()},
    "diagnosis": {"keys": {"diagnosis"}, "thresholds": lambda: ()},
    "incidents": {"keys": {"line.load"}, "thresholds": lambda: ()},
    "services": {"keys": {"service.status", "service.latency"}, "thresholds": lambda: ()},
    "line": {"keys": {"docsis.dspower", "docsis.snr", "docsis.uspower", "docsis.uncorr", "channel.heatmap"},
             "thresholds": lambda: _docsis_thresholds()},
    "router": {"keys": {"line.capacity", "router.cpu", "router.mem", "router.wan", "router.clients", "router.history",
                        "clients.band", "clients.signal", "clients.speed", "clients.connected", "clients.node"},
               "thresholds": lambda: _router_thresholds()},
}


def _docsis_thresholds():
    from pengumon.lines import docsis
    return (docsis.DS_SNR_WARNING_DB, docsis.DS_SNR_CRITICAL_DB, docsis.US_POWER_WARNING_DBMV, docsis.US_POWER_CRITICAL_DBMV,
            docsis.DS_POWER_WARNING_ABS_DBMV, docsis.US_POWER_LOW_WARNING_DBMV, docsis.US_POWER_LOW_CRITICAL_DBMV,
            docsis.RECENT_UNCORRECTABLES_CRITICAL, docsis.UNCORRECTABLE_WARNING_PER_MINUTE)


def _router_thresholds():
    from pengumon.devices.monitors import ROUTER_CPU_WARNING_PCT, ROUTER_MEM_CRITICAL_PCT, ROUTER_MEM_WARNING_PCT, WAN_LOAD_CRITICAL_PCT
    return (ROUTER_CPU_WARNING_PCT, ROUTER_MEM_WARNING_PCT, ROUTER_MEM_CRITICAL_PCT, WAN_LOAD_CRITICAL_PCT)


def _panel_help(panel: str) -> str:
    return "\n".join(re.findall(r"registerHelp\(\{\n(.*?)\n\}\);", (STATIC / "panels" / panel / "help.js").read_text(encoding="utf-8"), re.DOTALL))


def test_every_help_file_is_listed():
    assert {p.parent.name for p in (STATIC / "panels").glob("*/help.js")} == set(PANEL_HELP)


@pytest.mark.parametrize("panel", sorted(PANEL_HELP))
def test_a_panels_help_covers_its_metrics(panel):
    assert PANEL_HELP[panel]["keys"] <= set(HELP_MAP_KEY.findall(_panel_help(panel)))


@pytest.mark.parametrize("panel", sorted(PANEL_HELP))
def test_a_panels_help_quotes_the_thresholds_its_code_applies(panel):
    text = _panel_help(panel)
    for number in PANEL_HELP[panel]["thresholds"]():
        assert f"{number:g}" in text, f"{panel}/help.js does not mention the threshold {number:g}"


def test_info_buttons_are_plain_buttons_and_metrics_keep_no_title_hint():
    html = template_text()
    for tag in re.findall(r"<button[^>]*\bclass=\"info\"[^>]*>", html):
        assert 'type="button"' in tag and 'data-action="toggleHelp"' in tag and "aria-label=" in tag
    # the metric hints are popovers now; what keeps a title is an icon or short button
    assert not re.search(r'class="metric-stat"[^>]*\btitle=', html)
    assert 'class="info"' in html


def registry_entries() -> dict[str, str]:
    """Panel id -> its tab, from the default export of each discovered panel's panel.js, in the default order."""
    from pengumon.ui import discover
    entries: dict[str, str] = {}
    for part in discover().panels:
        text = (STATIC / part.module).read_text(encoding="utf-8")
        found = re.search(r"export default \{\s*id: '(\w+)',\s*title: '[^']+',\s*tab: '(\w+)'", text)
        assert found, f"{part.module} has no registry entry"
        entries[found.group(1)] = found.group(2)
    return entries


def test_tabs_are_named_consistently():
    html = template_text()
    entries = registry_entries()
    bar = re.findall(r'data-action="selectTab" data-arg="(\w+)"', html)
    assert len(bar) == len(set(bar)) and bar
    assert set(entries.values()) == set(bar)  # every panel's tab is on the bar, every tab has a panel
    assert "<nav" in html and 'id="mobileTabs"' in html
    # the tab belongs to the registry, not to the markup
    assert not re.search(r"\bdata-tab=", html)


def test_every_panel_folder_has_the_contract_and_is_a_config_id():
    """A panel is a folder: panel.json, panel.html (one section of that panel), panel.js (its registry entry)."""
    from pengumon.ui import discover, panel_ids
    from pengumon.web.settings import DashboardConfig
    ui = discover()
    folders = sorted(p.name for p in (STATIC / "panels").iterdir() if p.is_dir())
    assert sorted(p.id for p in ui.panels) == folders and len(folders) >= 10
    assert list(panel_ids()) == [p.id for p in ui.panels]  # the ids the config accepts, in their default order
    assert DashboardConfig(order=list(panel_ids())).order == list(panel_ids())  # every one of them is accepted
    assert list(registry_entries()) == list(panel_ids())  # each module's id is its folder's name
    for part in ui.panels:
        folder = STATIC / "panels" / part.id
        assert {"panel.json", "panel.html", "panel.js"} <= {p.name for p in folder.iterdir()}, part.id
        assert re.findall(r'<section\b[^>]*\bdata-panel="(\w+)"', (folder / "panel.html").read_text(encoding="utf-8")) == [part.id]
    assert re.search(r"<body\b[^>]*\bdata-layout='\{\{ layout\|tojson \}\}'", template_text())


def test_no_shared_file_names_a_panel():
    """Adding, removing or renaming a panel touches its own folder only: the page, the config, the module loader,
    the core modules and the shared styles never name one."""
    from pengumon.ui import discover
    ids = [p.id for p in discover().panels]
    shared = {
        "templates/index.html": (WEB / "templates" / "index.html").read_text(encoding="utf-8"),
        "config.py": (WEB.parent / "config.py").read_text(encoding="utf-8"),
        "static/js/panels/index.js": (JS / "panels" / "index.js").read_text(encoding="utf-8"),
        **{f"static/{c}": (STATIC / c).read_text(encoding="utf-8") for c in SHARED_CSS},
        **{f"static/js/{m}": (JS / m).read_text(encoding="utf-8") for m in CORE_MODULES},
    }
    for name, text in shared.items():
        for pid in ids:
            # (a tab name such as data-arg="line" on the tab bar is a tab, not the panel of that name)
            for pattern in (rf"data-panel=[\"']?{pid}\b", rf"panels/{pid}\b", rf"""(?<!data-arg=)["']{pid}["']""", rf"\./{pid}\.js"):
                assert not re.search(pattern, text), f"{name} names the panel {pid} ({pattern})"


@pytest.mark.asyncio
async def test_the_page_includes_links_and_loads_every_discovered_folder():
    import json
    from html import unescape
    from pengumon.ui import discover
    ui = discover()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        html = (await ac.get("/")).text
        for part in ui.panels:
            assert f'data-panel="{part.id}"' in html
            for css in part.css:
                assert f'href="/static/v/{ASSET_VERSION}/{css}"' in html
        modules = json.loads(unescape(re.search(r"<body\b[^>]*\bdata-modules='([^']*)'", html).group(1)))
        assert modules["panels"] == [p.module for p in ui.panels]
        assert modules["dialogs"] == [d.module for d in ui.dialogs if d.module] and len(modules["dialogs"]) >= 5
        for d in ui.dialogs:
            if d.template:
                dialog_id = re.search(r'<div class="modal" id="(\w+)"', (STATIC / d.template).read_text(encoding="utf-8")).group(1)
                assert f'id="{dialog_id}"' in html
        # templates and manifests in the folders are not files to serve
        for path in ("panels/line/panel.html", "panels/line/panel.json", "dialogs/trace/dialog.html"):
            assert (await ac.get(f"/static/v/{ASSET_VERSION}/{path}")).status_code == 404
            assert (await ac.get(f"/static/{path}")).status_code == 404
        assert (await ac.get(f"/static/v/{ASSET_VERSION}/panels/line/panel.js")).status_code == 200


@pytest.mark.asyncio
async def test_the_page_carries_the_configured_default_layout_in_a_csp_safe_attribute():
    import json
    from html import unescape
    from pengumon.api.state import STATE_ATTR, AppState
    from pengumon.config import AppConfig
    config = AppConfig(dashboard={"order": ["matrix", "diagnosis"], "hidden": ["report"]})
    setattr(app.state, STATE_ATTR, AppState(config=config))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            html = (await ac.get("/")).text
    finally:
        delattr(app.state, STATE_ATTR)
    raw = re.search(r"<body\b[^>]*\bdata-layout='([^']*)'", html).group(1)
    assert json.loads(unescape(raw)) == {"order": ["matrix", "diagnosis"], "hidden": ["report"]}
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)


# --- the core stands alone: optional panels plug in through the registry ---------------------------------------------

# The page's skeleton. These modules start the page, carry the live message, lay out and tab the panels; none of them
# may name an optional panel, a device role or a vendor/technology: a panel is added, removed or missing without
# touching them, and the page works (the smoke test's `bare` variant) with no devices at all.
CORE_MODULES = ["main.js", "live.js", "bus.js", "layout.js", "tabs.js", "dispatch.js", "devices.js", "chrome.js", "session.js"]
# what the core may import: itself, the shared helpers and the one list of panels
CORE_IMPORTABLE = {*CORE_MODULES, "util.js", "state.js", "help.js", "chart-theme.js", "panels/index.js"}
OPTIONAL_WORDS = re.compile(
    r"docsis|modem|router|mesh|grafana|topology|xfinity|asus|channel|dossier|inspector|incident|report|sparkline|"
    r"\bclients?\b|\bservices?\b|\bdiagnosis\b|\bmatrix\b|\blatency\b|\bline\b", re.IGNORECASE)


@pytest.mark.parametrize("name", CORE_MODULES)
def test_core_modules_name_no_optional_panel_and_import_only_core(name):
    text = (JS / name).read_text(encoding="utf-8")
    assert OPTIONAL_WORDS.findall(text) == []
    for imported in re.findall(r"from '(\.{1,2}/[\w./-]+\.js)'", text):
        resolved = (JS / name).parent.joinpath(imported).resolve().relative_to(JS).as_posix()
        assert resolved in CORE_IMPORTABLE, f"{name} imports {resolved}, which is not core"


def test_the_page_skeleton_has_no_optional_feature_markup():
    """What belongs to an optional feature is a partial the page includes, not part of the skeleton."""
    html = (WEB / "templates" / "index.html").read_text(encoding="utf-8")
    header = html.split("</header>", 1)[0]
    assert not re.search(r"modem|router|telemetry-links", header, re.IGNORECASE)
    assert (WEB / "templates" / "features" / "grafana.html").is_file()


def test_every_panel_module_has_the_contract():
    from pengumon.ui import discover
    for part in discover().panels:
        text = (STATIC / part.module).read_text(encoding="utf-8")
        assert re.search(r"available\s*[:(]", text), f"{part.module} has no availability function"
        assert re.search(r"\bmount\b", text), f"{part.module} mounts nothing"


def test_folders_import_only_the_core_the_shared_library_and_their_own_files():
    """A panel or dialog folder imports static/js (core and shared library) and files of its own folder, never
    another panel's or dialog's: removing a folder cannot break another one."""
    for root in (STATIC / "panels", STATIC / "dialogs"):
        for path in root.rglob("*.js"):
            folder = path.parent.resolve()
            for imported in re.findall(r"(?:from|import) '(\.{1,2}/[\w./-]+\.js)'", path.read_text(encoding="utf-8")):
                target = (path.parent / imported).resolve()
                assert target.parent == folder or JS.resolve() in target.parents, f"{path.relative_to(STATIC)} imports {imported}"


def test_registering_an_action_twice_is_refused():
    text = (JS / "dispatch.js").read_text(encoding="utf-8")
    assert "is already registered" in text and "throw new Error" in text
    # and no two modules register the same name for the same event
    seen: dict[tuple[str, str], str] = {}
    for path in all_js():
        body = path.read_text(encoding="utf-8")
        where = path.relative_to(STATIC).as_posix()
        for kind, names in re.findall(r"registerActions\('(\w+)', \{(.*?)\}\);", body, re.DOTALL):
            for name in re.findall(r"(?:^|\{)\s*(\w+)\s*:\s*\(", names, re.MULTILINE):
                other = seen.setdefault((kind, name), where)
                assert other == where, f"{kind} action {name} is registered by {other} and {where}"


def test_every_module_is_loaded():
    """A module nothing loads never runs, so the actions it registers would be missing from the page: every module is
    reached from main.js or from a discovered panel or dialog module (loaded by panels/index.js)."""
    from pengumon.ui import discover
    ui = discover()
    seen: set[Path] = set()
    todo = [JS / "main.js", *(STATIC / m for m in ui.modules["panels"] + ui.modules["dialogs"])]
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for imported in re.findall(r"(?:from|import) '(\.{1,2}/[\w./-]+\.js)'", path.read_text(encoding="utf-8")):
            todo.append((path.parent / imported).resolve())
    assert {p.resolve() for p in all_js()} <= seen


# --- device panels: one card per device, chosen by capability (decomposition step 6) -----------------------------------

def test_device_templates_carry_no_ids_so_a_card_can_exist_once_per_device():
    """Device cards are cloned from <template>s, one per device: an id inside would be duplicated with two routers."""
    for name in ("line", "router"):
        text = (STATIC / "panels" / name / "panel.html").read_text(encoding="utf-8")
        for body in re.findall(r"<template id=\"[\w-]+\">(.*?)</template>", text, re.DOTALL):
            assert not re.search(r"\bid=", body), f"panels/{name}.html: an id inside a device template"
            assert "data-f=" in body


def test_no_device_roles_are_left():
    """No module keeps a "the line" / "the router" singleton; a single-device view asks for the primary device."""
    assert not (JS / "roles.js").exists()
    for path in js_files():
        text = (WEB / path).read_text(encoding="utf-8")
        assert not re.search(r"state\.(lineDevice|routerDevice|clientsDevice)", text), path
    assert "export function primaryDevice" in (JS / "devices.js").read_text(encoding="utf-8")


TECHNOLOGY_WORDS = re.compile(r"docsis|modem|cable|coax|fiber|dsl|snr|dbmv|channel|uncorrectable", re.IGNORECASE)


@pytest.mark.parametrize("name", ["device-panel.js", "device-feed.js", "grades.js"])
def test_shared_device_modules_name_no_technology(name):
    text = (JS / name).read_text(encoding="utf-8")
    assert TECHNOLOGY_WORDS.findall(text) == []


def test_the_line_panel_takes_its_body_from_an_adapter():
    """panels/line.js knows technologies only as adapter names; the DOCSIS widgets live in lines/docsis.js."""
    text = (STATIC / "panels" / "line" / "panel.js").read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    naming = [line.strip() for line in code if re.search(r"docsis", line, re.IGNORECASE)]
    assert naming == ["import docsis from './docsis.js';", "const ADAPTERS = { docsis };",
                      "const TECH_NAMES = { docsis: 'DOCSIS', fiber: 'Fiber', dsl: 'DSL' };"]
    assert not re.search(r"snr|dbmv|uncorrectable|channels?\b", "\n".join(code), re.IGNORECASE)


def test_no_coloured_metric_is_graded_against_a_number_in_the_browser():
    """Grades come from the server (snapshot `grades`, device `ranges`); the old client thresholds are gone."""
    for path in js_files():
        text = (WEB / path).read_text(encoding="utf-8")
        assert not re.search(r"rx > 500|tx > 30|cpu_usage_pct > \d|mem_usage_pct > \d|absP [<>]=? \d|snr_db [<>] \d|dbm [<>]=? -\d", text), path



def test_no_browser_test_file_is_shipped_in_the_static_tree():
    """The browser test's step files live in tests/ui/steps, outside the tree the app serves (and hashes into the asset
    version); nothing named like a test may appear under static/."""
    stray = [p.relative_to(STATIC).as_posix() for p in STATIC.rglob("*") if p.is_file() and re.search(r"smoke|\.test\.|\.spec\.", p.name)]
    assert stray == []


@pytest.mark.asyncio
async def test_a_smoke_step_path_is_not_served():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        for url in ("/static/panels/line/smoke.mjs", f"/static/v/{ASSET_VERSION}/panels/line/smoke.mjs", "/static/dialogs/alerts/smoke.mjs"):
            assert (await client.get(url)).status_code == 404, url
