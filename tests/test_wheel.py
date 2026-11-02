"""The wheel: built the way the release builds it (pip wheel --no-deps --no-build-isolation), it must carry every
file the app reads at run time and nothing that is not the app. Also pyproject's declared dependencies against
requirements.in, and the console command."""

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from pengumon import __version__, ui
from pengumon.web import assets

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "pengumon"


def _names(requirement: str) -> str:
    return re.sub(r"[-_.]+", "-", re.match(r"[A-Za-z0-9_.-]+", requirement).group(0)).lower()


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    """The wheel of a copy of the tree (the build must not leave build/ or *.egg-info in the checkout)."""
    work = tmp_path_factory.mktemp("build")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        (work / name).write_bytes((ROOT / name).read_bytes())
    import shutil
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    out = work / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-cache-dir",
         "-q", "-w", str(out), str(work)],
        capture_output=True, text=True, cwd=work)
    assert result.returncode == 0, result.stdout + result.stderr
    built = list(out.glob("pengumon-*.whl"))
    assert len(built) == 1
    assert built[0].name.startswith(f"pengumon-{__version__}-"), built[0].name
    return zipfile.ZipFile(built[0])


def _in_package(directory: Path):
    return sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_wheel_carries_every_file_the_app_serves_or_renders(wheel):
    names = set(wheel.namelist())
    wanted = _in_package(ui.STATIC_DIR) + _in_package(assets.WEB_DIR / "templates")
    assert len(wanted) > 100  # the dashboard really is there to be checked
    missing = [str(p.relative_to(PACKAGE)) for p in wanted if "pengumon/" + p.relative_to(PACKAGE).as_posix() not in names]
    assert missing == []


def test_wheel_carries_every_python_module_and_the_vendored_homeguard(wheel):
    names = set(wheel.namelist())
    missing = [p.relative_to(PACKAGE).as_posix() for p in _in_package(PACKAGE)
               if p.suffix == ".py" and "pengumon/" + p.relative_to(PACKAGE).as_posix() not in names]
    assert missing == []
    assert {"pengumon/_vendor/homeguard/security.py", "pengumon/_vendor/homeguard/db.py",
            "pengumon/cli.py", "pengumon/paths.py"} <= names


def test_wheel_has_no_tests_configs_or_second_top_level_package(wheel):
    tops = {n.split("/")[0] for n in wheel.namelist()}
    assert tops == {"pengumon", f"pengumon-{__version__}.dist-info"}
    names = wheel.namelist()
    assert not [n for n in names if re.search(r"(^|/)(tests?|scripts)/", n)]
    assert not [n for n in names if Path(n).name in {"config.yaml", "config.example.yaml", ".env", "secrets.json"}]
    assert not [n for n in names if n.endswith((".pyc", ".db"))]


def test_wheel_declares_the_pengumon_command_and_the_dependencies(wheel):
    dist = f"pengumon-{__version__}.dist-info/"
    assert "pengumon = pengumon.cli:main" in wheel.read(dist + "entry_points.txt").decode()
    meta = wheel.read(dist + "METADATA").decode().replace("\r\n", "\n")  # built on Windows: CRLF line ends
    assert f"\nVersion: {__version__}\n" in meta
    requires = {_names(m) for m in re.findall(r"(?m)^Requires-Dist: (.+)$", meta) if "extra ==" not in m}
    assert {"opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http", "uvicorn", "websockets"} <= requires
    assert "uvicorn[standard]" not in meta


def test_pyproject_dependencies_cover_requirements_in():
    """requirements.in is what the image and the lock install; pyproject is what a pip/uv install of the wheel gets.
    A package only in the first one means the wheel installs and then fails to start (OpenTelemetry once did)."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    wanted = [line.strip() for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith(("#", "-"))]
    assert wanted
    assert {_names(d) for d in wanted} <= {_names(d) for d in declared}
    # same constraint text, not just the same name: a looser bound in one place would let them drift apart
    assert {d.replace(" ", "") for d in wanted} <= {d.replace(" ", "") for d in declared}
