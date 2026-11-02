"""db/migrations/ holds one module per schema version: versions are unique, contiguous from 1 and equal to the number
in the module name, so two branches that add "the next" migration collide here, at once, instead of in production."""

import importlib
import pkgutil
import re

from pengumon.db import migrations

MODULE = re.compile(r"^v(\d{3})_[a-z0-9_]+$")


def _modules():
    return [(info.name, importlib.import_module(f"{migrations.__name__}.{info.name}"))
            for info in pkgutil.iter_modules(migrations.__path__) if MODULE.match(info.name)]


def test_every_version_module_is_named_and_numbered_alike():
    mods = _modules()
    assert mods
    assert [(name, int(MODULE.match(name).group(1))) for name, _ in mods] == [(name, m.VERSION) for name, m in mods]


def test_versions_are_unique_and_contiguous_from_one():
    versions = sorted(m.VERSION for _, m in _modules())
    assert versions == list(range(1, len(versions) + 1))


def test_the_runner_list_is_those_modules_in_order_and_latest_is_the_highest():
    assert [v for v, _ in migrations.MIGRATIONS] == list(range(1, len(migrations.MIGRATIONS) + 1))
    assert migrations.LATEST_VERSION == len(migrations.MIGRATIONS)
    assert all(callable(step) for _, step in migrations.MIGRATIONS)


def test_no_stray_module_escapes_the_naming_rule():
    """A file like migration_12.py would be silently ignored by the discovery: only common.py may be unnumbered."""
    others = [i.name for i in pkgutil.iter_modules(migrations.__path__) if not MODULE.match(i.name)]
    assert others == ["common"]
