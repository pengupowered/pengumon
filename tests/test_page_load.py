"""The cold page load: the module graph listed for modulepreload, the panels' styles as one sheet, and versioned text
assets compressed once (pengumon.web.preload, pengumon.web.assets)."""

import re
import zlib
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

from pengumon.main import app
from pengumon.web import assets, pages
from pengumon.web.assets import ASSET_VERSION, STATIC_DIR, panel_stylesheet
from pengumon.web.preload import module_closure, static_imports
from pengumon.web.ui import discover


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def page() -> str:
    async with client() as ac:
        return (await ac.get("/")).text


# --- 1. modulepreload ---------------------------------------------------------------------------

def test_static_imports_reads_every_static_form_and_nothing_else():
    source = """
// import { fake } from './comment.js';
/* import './block.js';
   export * from './block2.js'; */
import './session.js'; // side effect
import { a, b } from './a.js';
import c, {
  d,
  e as f
} from "../up/c.js";
import * as ns from './ns.js';
export { g } from './g.js';
export * from './all.js';
export const label = 'from ./not-an-import.js';
const lazy = await import('./lazy.js');
import('./also-lazy.js').then(() => {});
import { x } from 'https://example.com/x.js';
"""
    assert static_imports(source) == ["./session.js", "./a.js", "../up/c.js", "./ns.js", "./g.js", "./all.js",
                                      "https://example.com/x.js"]


def test_module_closure_follows_relative_imports_breadth_first_and_reports_what_is_missing(tmp_path):
    (tmp_path / "js" / "sub").mkdir(parents=True)
    (tmp_path / "js" / "main.js").write_text("import './sub/a.js';\nimport { b } from './b.js';\nimport('./lazy.js');\n")
    (tmp_path / "js" / "sub" / "a.js").write_text("import { b } from '../b.js';\nimport '../gone.js';\n")
    (tmp_path / "js" / "b.js").write_text("export const b = 1;\n")
    (tmp_path / "js" / "lazy.js").write_text("export const l = 1;\n")
    found, missing = module_closure(tmp_path, ["js/main.js"])
    assert found == ["js/main.js", "js/sub/a.js", "js/b.js"]  # lazy.js is a dynamic import: not followed
    assert missing == ["js/gone.js"]


def test_the_real_module_graph_resolves_to_shipped_files_and_leaves_lazy_dialogs_out():
    modules = pages.page_modules()
    entries = ["js/main.js", *modules["dialogs"], *modules["panels"]]
    found, missing = module_closure(STATIC_DIR, entries)
    assert missing == []  # every static import of the start names a shipped file
    assert set(entries) <= set(found) and len(found) == len(set(found))
    assert "js/modal.js" in found and "js/bus.js" in found  # reached through imports, not named by the page
    assert any(m not in entries for m in found) and len(found) > 50
    lazy = {entry["module"] for entry in modules["lazy"]}
    assert lazy and not lazy & set(found)  # a lazy dialog stays lazy: nothing eager imports it
    assert tuple(found) == pages.preload_modules()
    # nothing outside the start: a theme's effect or card view is loaded by import() only
    assert not [m for m in found if m.startswith("themes/")]


@pytest.mark.asyncio
async def test_the_page_preloads_exactly_that_graph_and_each_link_is_served():
    html = await page()
    hrefs = re.findall(r'<link rel="modulepreload" href="([^"]+)"', html)
    assert hrefs == [f"/static/v/{ASSET_VERSION}/{m}" for m in pages.preload_modules()]
    head = html.split("</head>", 1)[0]
    assert all(h in head for h in hrefs)  # in the head: found before the body is parsed
    assert head.index('href="/static/v/%s/panels.css"' % ASSET_VERSION) < head.index('rel="modulepreload"')
    async with client() as ac:
        for href in hrefs:
            res = await ac.get(href)
            assert res.status_code == 200 and "javascript" in res.headers["content-type"], href


# --- 2. one sheet for the panels ---------------------------------------------------------------------

def test_panel_stylesheets_are_concatenated_in_link_order_and_have_no_relative_url():
    ui = discover()
    files = [c for p in (*ui.panels, *ui.dialogs) for c in p.css]
    assert len(files) > 20
    sheet = panel_stylesheet()
    positions = [sheet.index(f"/* {css} */\n") for css in files]
    assert positions == sorted(positions)  # the order the page used to link them: the cascade is the same
    for css in files:
        text = (STATIC_DIR / css).read_text(encoding="utf-8").rstrip()
        assert text in sheet
        assert "url(" not in text and "@import" not in text and "@charset" not in text, css  # would not resolve from panels.css


@pytest.mark.asyncio
async def test_the_page_links_the_panels_sheet_once_after_the_shared_styles():
    html = await page()
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
    base = f"/static/v/{ASSET_VERSION}/"
    assert links == [base + n for n in ("tokens.css", "components.css", "page.css", "style.css", "panels.css")]
    async with client() as ac:
        sheet = await ac.get(base + "panels.css")
        wrong = await ac.get("/static/v/0000000000ab/panels.css")
        head = await ac.head(base + "panels.css")
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/css")
    assert sheet.text == panel_stylesheet()
    assert sheet.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert wrong.status_code == 404 and head.status_code == 200


# --- 3. compressed once ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_text_asset_is_served_gzip_from_kept_bytes_and_compressed_once(monkeypatch):
    url = f"/static/v/{ASSET_VERSION}/js/modal.js"
    plain = (STATIC_DIR / "js" / "modal.js").read_bytes()
    async with client() as ac:
        first = await ac.get(url, headers={"Accept-Encoding": "gzip"})
        # a second request must not compress again: the bytes are kept
        monkeypatch.setattr(assets.zlib, "compressobj", lambda *a, **k: pytest.fail("compressed again"))
        second = await ac.get(url, headers={"Accept-Encoding": "gzip"})
    assert first.headers["content-encoding"] == "gzip" and second.headers["content-encoding"] == "gzip"
    assert first.headers["vary"] == "Accept-Encoding"
    assert first.content == plain == second.content  # decoded once by the client: not compressed twice
    assert int(first.headers["content-length"]) == len(assets._gzipped["js/modal.js"]) < len(plain)
    assert zlib.decompress(assets._gzipped["js/modal.js"], 31) == plain
    assert first.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_a_client_without_gzip_gets_the_plain_file_and_binaries_are_never_gzipped():
    plain = (STATIC_DIR / "js" / "modal.js").read_bytes()
    async with client() as ac:
        none = await ac.get(f"/static/v/{ASSET_VERSION}/js/modal.js", headers={"Accept-Encoding": "identity"})
        png = await ac.get(f"/static/v/{ASSET_VERSION}/icons/app-180.png", headers={"Accept-Encoding": "gzip"})
    for res in (none,):
        assert "content-encoding" not in res.headers and res.content == plain
        assert "Accept-Encoding" in res.headers["vary"]  # a cache keeps the plain and the gzip answer apart
    assert png.status_code == 200 and "content-encoding" not in png.headers


@pytest.mark.asyncio
async def test_gzip_answers_keep_etag_304_and_head():
    url = f"/static/v/{ASSET_VERSION}/style.css"
    async with client() as ac:
        full = await ac.get(url, headers={"Accept-Encoding": "gzip"})
        etag = full.headers["etag"]
        again = await ac.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
        plain_full = await ac.get(url, headers={"Accept-Encoding": "identity"})
        plain_again = await ac.get(url, headers={"Accept-Encoding": "identity", "If-None-Match": plain_full.headers["etag"]})
        head = await ac.head(url, headers={"Accept-Encoding": "gzip"})
    assert etag.startswith("W/")
    assert again.status_code == 304 and again.content == b""
    assert plain_again.status_code == 304
    assert head.status_code == 200 and head.content == b"" and head.headers["content-encoding"] == "gzip"
    assert head.headers["content-length"] == full.headers["content-length"]


@pytest.mark.asyncio
async def test_generated_sheets_are_gzipped_from_kept_bytes_too():
    async with client() as ac:
        for path, key in ((f"/static/v/{ASSET_VERSION}/panels.css", "panels.css"),
                          (f"/static/v/{ASSET_VERSION}/themes/nord/theme.css", "themes/nord/theme.css")):
            res = await ac.get(path, headers={"Accept-Encoding": "gzip"})
            assert res.headers["content-encoding"] == "gzip" and res.headers["vary"] == "Accept-Encoding"
            assert zlib.decompress(assets._gzipped[key], 31) == res.content
            assert int(res.headers["content-length"]) == len(assets._gzipped[key])
            plain = await ac.get(path, headers={"Accept-Encoding": "identity"})
            assert "content-encoding" not in plain.headers and plain.content == res.content


@pytest.mark.parametrize("header,expected", [
    ("gzip", True), ("gzip, deflate, br", True), ("br, GZIP;q=0.5", True), ("*", True), ("identity", False), ("", False),
    ("gzip;q=0, identity", False), ("deflate, br", False), ("gzip;q=oops", False)])
def test_accepts_gzip_reads_the_accept_encoding_list(header, expected):
    from starlette.requests import Request
    request = Request({"type": "http", "headers": [(b"accept-encoding", header.encode())] if header else []})
    assert assets.accepts_gzip(request) is expected
