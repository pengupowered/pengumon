"""The static files: served under /static/v/<content hash>/ (cached for good) and under plain /static (old URLs).

The hash covers the whole shipped tree, so a redeploy with changed content changes every asset URL and the long
cache on that prefix can never serve stale files. The panel and dialog folders (pengumon.ui) hold templates and
manifests next to their assets; those are never served as files.
"""

import hashlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from pengumon.ui import NOT_SERVED

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
        return await files.get_response(asset_path, request.scope)


def mount_static(app: FastAPI) -> None:
    """Both static routes, the versioned one first (the plain mount would otherwise swallow its paths)."""
    add_versioned_static(app, STATIC_DIR, ASSET_VERSION)
    app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")
