"""The version has a single source: pengumon.__version__. pyproject.toml reads it from there (dynamic version), so
the wheel, the API and the release tag check cannot disagree."""

import re
import tomllib
from pathlib import Path

from pengumon import __version__
from pengumon.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_api_version_is_the_package_version():
    assert app.version == __version__
    assert app.openapi()["info"]["version"] == __version__


def test_pyproject_takes_the_version_from_the_package():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" in pyproject["project"]["dynamic"]
    assert "version" not in pyproject["project"]  # a second, static copy could drift
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "pengumon.__version__"}


def test_release_workflow_reads_the_version_from_the_package():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "src/pengumon/__init__.py" in text
    assert not re.search(r"pyproject\.toml\s*\|\s*head", text)
