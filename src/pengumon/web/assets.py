"""The static files: served under /static/v/<content hash>/ (cached for good) and under plain /static (old URLs).

The hash covers the whole shipped tree, so a redeploy with changed content changes every asset URL and the long
cache on that prefix can never serve stale files. The panel and dialog folders (pengumon.web.ui) hold templates and
manifests next to their assets; those are never served as files.
"""

import hashlib
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pengumon.web.themes import look_stylesheets
from pengumon.web.ui import NOT_SERVED, ui

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


def not_served(asset_path: str) -> bool:
    """The panel and dialog folders hold templates and manifests next to their assets; those are not files to serve.
    Starlette hands the plain /static mount an OS path (backslashes on Windows), the versioned route a URL path."""
    asset_path = asset_path.replace("\\", "/")
    return asset_path.startswith(("panels/", "dialogs/")) and asset_path.endswith(NOT_SERVED)


class VersionedStaticFiles(StaticFiles):
    """Static files for old URLs: cached for a day only when requested with a ?v= (the pre-/static/v/ template
    put one on every asset); an unversioned request keeps the browser revalidating."""

    async def get_response(self, path, scope):
        if not_served(path):
            raise HTTPException(status_code=404)
        response = await super().get_response(path, scope)
        if response.status_code == 200 and b"v=" in scope.get("query_string", b""):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response


def asset_version(directory: Path = STATIC_DIR) -> str:
    """Content hash of the shipped static tree (every file, including subdirectories such as js/).

    The hash is the /static/v/<hash>/ path prefix, so a redeploy with changed content changes every asset URL
    and the long cache on that prefix can never serve stale files."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = asset_version()
ASSET_VERSIONS = {"tree": ASSET_VERSION}


@lru_cache(maxsize=1)
def panel_stylesheet() -> str:
    """The stylesheets of every panel and dialog folder (pengumon.web.ui, the page's `ui.css`) as one, in the order the
    page used to link them, so the cascade is the same. Served at /static/v/<tree>/panels.css: one request instead of
    one per file. A relative url() in one of them would stop resolving from there, so none may have one (the folders'
    own files are still served at their paths)."""
    return "".join(f"/* {css} */\n{(STATIC_DIR / css).read_text(encoding='utf-8').rstrip()}\n" for css in ui().css)


# Versioned answers never change, so a text asset is compressed once, the first time a client asks for it that accepts gzip,
# and the bytes are kept: the tree is a few hundred files, so the memory is bounded by its size (about 400 KB compressed).
# Level 9: it is paid once per file and per start. Keyed by the asset's path (or a generated sheet's name); one hash is
# served per process, so a key never holds two versions.
GZIP_SUFFIXES = (".js", ".css", ".svg", ".webmanifest")
GZIP_MIN_SIZE = 1024  # as GZipMiddleware (pengumon.main): below it the answer is plain for everyone
_gzipped: Dict[str, bytes] = {}


def accepts_gzip(request: Request) -> bool:
    """Whether the client's Accept-Encoding lists gzip (or *) with a quality above 0."""
    for token in request.headers.get("accept-encoding", "").split(","):
        name, _, params = token.partition(";")
        if name.strip().lower() in ("gzip", "x-gzip", "*"):
            quality = params.strip().lower().removeprefix("q=")
            try:
                return float(quality or 1) > 0
            except ValueError:
                return False
    return False


def gzipped(key: str, content: Callable[[], bytes]) -> bytes:
    """The gzip of `content()`, made once per `key` and kept."""
    packed = _gzipped.get(key)
    if packed is None:
        packer = zlib.compressobj(9, zlib.DEFLATED, 31)  # wbits 31: a gzip stream
        packed = _gzipped[key] = packer.compress(content()) + packer.flush()
    return packed


def gzip_response(request: Request, key: str, content: Callable[[], bytes], headers: Dict[str, str]) -> Response:
    """A gzip answer built from the kept bytes: its length is the stored length, and GZipMiddleware leaves it alone (it
    skips a response that already has a Content-Encoding). A HEAD gets the headers only."""
    packed = gzipped(key, content)
    headers = {**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding", "Content-Length": str(len(packed))}
    return Response(b"" if request.method == "HEAD" else packed, headers=headers)


def add_versioned_static(target: FastAPI, directory: Path, version: str) -> None:
    """Serve `directory` under /static/v/<version>/... (404 for any other version).

    ES modules import their siblings by relative URL, which carries no ?v=; loading the entry module from a
    hash-prefixed path makes every import inherit that prefix and get cache-busted with it.
    Must be registered before the plain /static mount, which would otherwise swallow the path."""
    files = StaticFiles(directory=str(directory))

    @target.api_route("/static/v/{asset_version}/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def versioned_static(asset_version: str, asset_path: str, request: Request):
        if asset_version != version or not_served(asset_path):
            raise HTTPException(status_code=404)
        response = await files.get_response(asset_path, request.scope)
        if not asset_path.endswith(GZIP_SUFFIXES):
            return response
        if (response.status_code != 200 or not isinstance(response, FileResponse) or not accepts_gzip(request)
                or "range" in request.headers or Path(response.path).stat().st_size < GZIP_MIN_SIZE):
            return response  # 304, 404, a range, a small file, or a client that takes plain bytes: StaticFiles' answer as it is
            # (GZipMiddleware adds the Vary: Accept-Encoding to the plain answers it would have compressed)
        # Same validators as StaticFiles' own answer (a revalidating browser gets its 304 from the branch above), weak
        # because the bytes differ from the plain ones.
        headers = {name: response.headers[name] for name in ("content-type", "last-modified") if name in response.headers}
        if "etag" in response.headers:
            headers["etag"] = "W/" + response.headers["etag"]
        return gzip_response(request, asset_path, lambda: Path(response.path).read_bytes(), headers)


def generated_css(request: Request, key: str, css: str) -> Response:
    """A generated stylesheet: gzip from the kept bytes for a client that accepts it, else the text."""
    headers = {"Content-Type": "text/css; charset=utf-8"}
    if accepts_gzip(request):
        return gzip_response(request, key, lambda: css.encode("utf-8"), headers)
    return Response(css, headers=headers)  # GZipMiddleware adds the Vary header to this plain answer


def add_panels_css(target: FastAPI, version: str) -> None:
    """/static/v/<version>/panels.css: the panels' and dialogs' stylesheets as one (panel_stylesheet). Registered before
    the versioned files, like the themes' sheets."""

    @target.api_route("/static/v/{asset_version}/panels.css", methods=["GET", "HEAD"], include_in_schema=False)
    async def panels_stylesheet(asset_version: str, request: Request):
        if asset_version != version:
            raise HTTPException(status_code=404)
        return generated_css(request, "panels.css", panel_stylesheet())


def add_themes_css(target: FastAPI, version: str) -> None:
    """/static/v/<version>/themes/<id>/theme.css: a built-in theme's stylesheet (its token blocks, skin and card views'
    styles), generated from its folder (pengumon.web.themes.look_css); 404 for a theme without one. Those files are part
    of the tree, so the tree hash versions these too. Registered before the versioned files, which would otherwise answer
    404 for them."""

    @target.api_route("/static/v/{asset_version}/themes/{theme_id}/theme.css", methods=["GET", "HEAD"], include_in_schema=False)
    async def theme_stylesheet(asset_version: str, theme_id: str, request: Request):
        css = look_stylesheets().get(theme_id)
        if asset_version != version or css is None:
            raise HTTPException(status_code=404)
        return generated_css(request, f"themes/{theme_id}/theme.css", css)


def mount_static(app: FastAPI) -> None:
    """The static routes: the themes' generated stylesheets, then the versioned files, then the plain mount (each would
    otherwise swallow the paths of the one before)."""
    add_themes_css(app, ASSET_VERSION)
    add_panels_css(app, ASSET_VERSION)
    add_versioned_static(app, STATIC_DIR, ASSET_VERSION)
    app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")
