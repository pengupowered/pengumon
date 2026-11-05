"""Guards on where the config sections live: each feature's section model sits next to the feature, config.py keeps
only AppConfig (one line per section), the two core sections and the YAML loading, and no feature imports
pengumon.config to reach its own settings."""

import ast
import os
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from pengumon import config
from pengumon.config import AppConfig

SRC = Path(__file__).resolve().parent.parent / "src" / "pengumon"
CORE_SECTIONS = {"server", "database"}  # belong to no single feature, so config.py defines them
SETTINGS_MODULES = [
    "pengumon.config", "pengumon.engine.prober_settings", "pengumon.engine.classifier_settings",
    "pengumon.engine.load_settings", "pengumon.services.settings", "pengumon.clients.settings",
    "pengumon.web.settings", "pengumon.devices.settings", "pengumon.secrets", "pengumon.engine.netinfo",
    "pengumon.notify.settings", "pengumon.engine.discovery.settings", "pengumon.lines.levels", "pengumon.devices.levels",
    "pengumon.reports.settings", "pengumon.settings.general", "pengumon.settings.registry",
    "pengumon.settings.lenient", "pengumon.settings.importer", "pengumon.settings.manager",
]


def _section_model(annotation):
    """The model of a section: the annotation itself, or the item type of a List[...] section."""
    args = typing.get_args(annotation)
    return args[0] if args else annotation


def test_every_feature_section_is_defined_outside_config_py():
    in_config = [name for name, field in AppConfig.model_fields.items()
                 if _section_model(field.annotation).__module__ == config.__name__]
    assert sorted(in_config) == sorted(CORE_SECTIONS)


def _runtime_imports_of_config(tree: ast.AST):
    """Line numbers of imports of pengumon.config outside an `if TYPE_CHECKING:` block."""
    type_checking = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            type_checking |= {id(n) for n in ast.walk(node)}
    for node in ast.walk(tree):
        if id(node) in type_checking:
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (
                node.module == "pengumon.config" or (node.module == "pengumon" and
                                                     any(a.name == "config" for a in node.names))):
            yield node.lineno
        elif isinstance(node, ast.Import) and any(a.name == "pengumon.config" for a in node.names):
            yield node.lineno


def test_only_the_app_imports_pengumon_config():
    """A feature reads its section from the AppConfig it is handed and imports its own settings module."""
    offenders = [f"{path.relative_to(SRC)}:{line}" for path in sorted(SRC.rglob("*.py"))
                 for line in _runtime_imports_of_config(ast.parse(path.read_text(encoding="utf-8")))
                 # the app, its command, and the settings store (it describes, imports and builds AppConfig)
                 if path.relative_to(SRC).as_posix() not in ("main.py", "cli.py", "settings/registry.py",
                                                              "settings/importer.py", "settings/manager.py",
                                                              "settings/validate.py", "settings/check.py")]
    assert offenders == []


@pytest.mark.parametrize("module", SETTINGS_MODULES)
def test_each_settings_module_imports_first_without_a_cycle(module):
    """Imported first in a fresh interpreter, so an import cycle that a different import order hides shows up."""
    run = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60,
                         env={"PYTHONPATH": str(SRC.parent), "PATH": "/usr/bin:/bin",
                              # Windows: without SYSTEMROOT, Winsock (asyncio's _overlapped) cannot load at all, and
                              # without the profile variables there is no home directory
                              **{k: os.environ[k] for k in ("SYSTEMROOT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                                                            "LOCALAPPDATA", "APPDATA", "TEMP", "TMP")
                                 if k in os.environ}})
    assert run.returncode == 0, run.stderr[-2000:]
