"""Hash-prefixed static serving: /static/v/<hash>/ is immutable, old ?v= URLs and unversioned paths keep working."""

import hashlib
import re

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import AsyncClient, ASGITransport

from pengumon.main import app, security_config
from pengumon.web.assets import ASSET_VERSION, STATIC_DIR, add_versioned_static, asset_version
from pengumon.security import SecurityConfig, SecurityMiddleware


def client(target=None):
    return AsyncClient(transport=ASGITransport(app=target or app), base_url="http://test")


_GUARD = SecurityMiddleware(None, security_config)  # only its header rules are used here


def _cache_control(path: str) -> str:
    return dict(_GUARD._security_headers(path)).get(b"cache-control", b"").decode()


# --- header matrix ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/api/status", "no-store"),
    ("/static/v/abc1234567/js/main.js", "public, max-age=31536000, immutable"),
    ("/static/style.css", ""),  # old URLs: cached (or not) by VersionedStaticFiles
    ("/", ""),
])
def test_cache_header_matrix(path, expected):
    assert _cache_control(path) == expected


def test_versioned_errors_are_no_store_unit():
    assert dict(_GUARD._security_headers("/static/v/x/a.js", 404))[b"cache-control"] == b"no-store"
    ok = dict(_GUARD._security_headers("/static/v/x/a.js", 200))[b"cache-control"]
    assert ok == b"public, max-age=31536000, immutable"
    assert dict(_GUARD._security_headers("/static/v/x/a.js", 304))[b"cache-control"] == ok


# --- real app --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_versioned_asset_is_served_immutable():
    async with client() as ac:
        plain = await ac.get("/static/style.css")
        versioned = await ac.get(f"/static/v/{ASSET_VERSION}/style.css")
        head = await ac.head(f"/static/v/{ASSET_VERSION}/style.css")
    assert versioned.status_code == 200
    assert versioned.content == plain.content
    assert versioned.headers.get_list("cache-control") == ["public, max-age=31536000, immutable"]
    assert head.status_code == 200


@pytest.mark.asyncio
async def test_old_v_query_urls_keep_their_day_cache():
    async with client() as ac:
        old = await ac.get("/static/style.css?v=0123456789ab")
        plain = await ac.get("/static/style.css")
    assert old.status_code == 200
    assert old.headers["cache-control"] == "public, max-age=86400"
    assert "cache-control" not in plain.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/static/v/0000000000/style.css",
    f"/static/v/{ASSET_VERSION}/nope.js",
    # traversal: each of these resolves to a file that exists outside static/ (see the test below), so a 404 is the guard
    f"/static/v/{ASSET_VERSION}/..%2Ftemplates%2Findex.html",
    f"/static/v/{ASSET_VERSION}/%2e%2e/templates/index.html",
    f"/static/v/{ASSET_VERSION}/%2e%2e/%2e%2e/main.py",
    f"/static/v/{ASSET_VERSION}/..%5Ctemplates%5Cindex.html",
    f"/static/v/{ASSET_VERSION}/",
])
async def test_versioned_path_rejects_wrong_hash_missing_and_traversal(path):
    async with client() as ac:
        res = await ac.get(path)
    assert res.status_code == 404
    assert res.headers["cache-control"] == "no-store"


def test_traversal_cases_point_at_files_that_exist():
    # otherwise the 404s above would prove nothing: the targets must be real files next to (not inside) static/
    assert (STATIC_DIR / ".." / "templates" / "index.html").resolve().is_file()
    assert (STATIC_DIR / ".." / ".." / "main.py").resolve().is_file()


@pytest.mark.asyncio
async def test_versioned_asset_is_public_when_a_token_is_configured(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "s3cret-token")
    async with client() as ac:
        res = await ac.get(f"/static/v/{ASSET_VERSION}/style.css")
    assert res.status_code == 200


def test_versioned_route_is_not_in_openapi():
    assert not any(p.startswith("/static") for p in app.openapi()["paths"])


@pytest.mark.asyncio
async def test_index_references_every_asset_under_the_tree_hash():
    async with client() as ac:
        page = (await ac.get("/")).text
    assert f'href="/static/v/{ASSET_VERSION}/style.css"' in page
    assert f'src="/static/v/{ASSET_VERSION}/vendor/chart.umd.min.js"' in page
    assert "?v=" not in page


# --- reusable pieces against a throwaway directory ---------------------------------------------

def test_asset_version_covers_subdirectories(tmp_path):
    (tmp_path / "a.js").write_text("a")
    before = asset_version(tmp_path)
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "probe.js").write_text("x")
    after = asset_version(tmp_path)
    assert before != after
    (tmp_path / "js" / "probe.js").write_text("y")
    assert asset_version(tmp_path) != after


@pytest.mark.asyncio
async def test_served_tree_hash_matches_an_independent_rebuild():
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
            digest.update(path.read_bytes())
    async with client() as ac:
        page = (await ac.get("/")).text
    assert re.search(r"/static/v/([0-9a-f]{12})/js/main\.js", page).group(1) == digest.hexdigest()[:12]


@pytest.mark.asyncio
async def test_versioned_module_directory_on_throwaway_app(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "probe.js").write_text("export const probe = 1;\n")
    version = asset_version(tmp_path)
    sub = FastAPI()
    add_versioned_static(sub, tmp_path, version)
    sub.mount("/static", StaticFiles(directory=str(tmp_path)), name="static")
    wrapped = SecurityMiddleware(sub, cfg=SecurityConfig())
    async with client(wrapped) as ac:
        ok = await ac.get(f"/static/v/{version}/js/probe.js")
        other = await ac.get("/static/v/deadbeef00/js/probe.js")
        plain = await ac.get("/static/js/probe.js")
    assert ok.status_code == 200 and "probe" in ok.text
    assert "immutable" in ok.headers["cache-control"]
    assert other.status_code == 404
    assert plain.status_code == 200
