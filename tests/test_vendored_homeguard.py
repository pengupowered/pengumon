"""The vendored homeguard copy: manifest matches the files, matches upstream when it is nearby, and cannot
confuse depguard's SBOM or the Dockerfile's `pip install`."""

import hashlib
import os
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "src" / "pengumon" / "_vendor" / "homeguard"
APP_PACKAGE = "pengumon"
VENDOR_COMMAND = ("python -I <homeguard checkout>/scripts/vendor.py --dest src/pengumon/_vendor/homeguard "
                  "<this checkout>")


def _lf_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def _manifest() -> dict:
    from pengumon._vendor.homeguard import _vendored

    return {"COMMIT": _vendored.COMMIT, "FILES": _vendored.FILES}


def test_manifest_hashes_match_the_files():
    files = _manifest()["FILES"]
    for name, digest in files.items():
        assert hashlib.sha256(_lf_bytes(VENDORED / name)).hexdigest() == digest, (
            f"{name} was edited in place; change homeguard upstream and re-vendor: {VENDOR_COMMAND}")


def test_manifest_lists_exactly_the_vendored_python_files():
    on_disk = {p.name for p in VENDORED.glob("*.py")} - {"_vendored.py"}
    assert on_disk == set(_manifest()["FILES"])


def test_vendored_files_use_lf_line_endings():
    for path in VENDORED.glob("*.py"):
        assert b"\r\n" not in path.read_bytes(), path.name


def _upstream() -> Path | None:
    candidates = [os.environ.get("HOMEGUARD_HOME"), ROOT.parent / "homeguard", ROOT.parent / "development" / "homeguard"]
    for candidate in candidates:
        if candidate and (Path(candidate) / "src" / "homeguard").is_dir():
            return Path(candidate) / "src" / "homeguard"
    return None


def test_copy_matches_upstream_when_homeguard_is_nearby():
    upstream = _upstream()
    if upstream is None:
        pytest.skip("homeguard source not found next to the app (set HOMEGUARD_HOME); e.g. inside the container")
    source = {p.name: _lf_bytes(p) for p in upstream.glob("*.py") if p.name != "_vendored.py"}
    vendored = {p.name: _lf_bytes(p) for p in VENDORED.glob("*.py") if p.name != "_vendored.py"}
    assert vendored == source, f"the vendored homeguard is out of date; run: {VENDOR_COMMAND}"


def test_homeguard_is_only_importable_from_inside_the_package():
    """The wheel carries one top-level package; a top-level `homeguard` (the old src/homeguard) must not come back,
    and nothing may import it."""
    assert not (ROOT / "src" / "homeguard").exists()
    pattern = re.compile(r"^\s*(from|import)\s+homeguard\b", re.MULTILINE)
    offenders = [str(p.relative_to(ROOT)) for folder in ("src", "tests") for p in (ROOT / folder).rglob("*.py")
                 if p.name != Path(__file__).name and pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_only_the_app_package_defines_dunder_version():
    """depguard sbom reads the app version from the first src/*/__init__.py with __version__; homeguard sorts first."""
    owners = [p.parent.name for p in sorted((ROOT / "src").glob("*/__init__.py"))
              if re.search(r"^__version__\s*=", p.read_text(encoding="utf-8"), re.MULTILINE)]
    assert owners == [APP_PACKAGE]


def test_pyproject_picks_up_every_package_under_src():
    """The Dockerfile's `pip install` must include src/homeguard without a Dockerfile change."""
    find = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert find["where"] == ["src"] and "include" not in find and "exclude" not in find
