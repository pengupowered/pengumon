"""db/repository.py stays a composition: the class is built from the per-domain mixins and holds no SQL and no
methods of its own. A new query goes in the module of its domain (db/probes.py, db/incidents.py, ...)."""

import ast
import re
from pathlib import Path

from pengumon.db import repository
from pengumon.db.repository import DatabaseRepository

SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA|REPLACE INTO)\b", re.IGNORECASE)
TREE = ast.parse(Path(repository.__file__).read_text())


def test_repository_py_holds_no_sql():
    strings = [n.value for n in ast.walk(TREE) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert [s for s in strings if SQL.search(s)] == []


def test_repository_class_defines_no_methods_of_its_own():
    cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "DatabaseRepository")
    assert [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] == []


def test_every_base_is_a_domain_mixin_from_the_db_package():
    assert all(b.__module__.startswith("pengumon.db.") for b in DatabaseRepository.__bases__)
