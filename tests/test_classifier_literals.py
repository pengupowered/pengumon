"""The outage levels live in one place, ClassifierConfig: a copy of one of their old literals left in the classifier
would silently ignore the setting. This fails if 15, 40, 25 or 90 (the old degraded, down, DNS-degraded and
DNS-cooldown literals) appears as a number in engine/classifier/*.py, except as the value of a named module-level
constant (UPPER_CASE), which is how the internal levels that are not settings are written."""

import ast
from pathlib import Path

import pytest

import pengumon.engine.classifier as classifier_package

OLD_LEVELS = {15, 40, 25, 90}
FILES = sorted(Path(classifier_package.__file__).parent.glob("*.py"))


def leftover_levels(source: str):
    """(line, value) of every number in OLD_LEVELS that is not the value of a module-level UPPER_CASE constant."""
    tree = ast.parse(source)
    named = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(t, ast.Name) and t.id.isupper() for t in targets) and isinstance(node.value, ast.Constant):
                named.add(id(node.value))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and type(node.value) in (int, float) and node.value in OLD_LEVELS
                and id(node) not in named):
            found.append((node.lineno, node.value))
    return found


def test_the_package_has_its_modules():
    assert {p.name for p in FILES} >= {"__init__.py", "metrics.py", "rules.py", "tracker.py"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_old_level_literal_is_left_in_the_classifier(path):
    assert leftover_levels(path.read_text(encoding="utf-8")) == [], (
        f"{path.name}: read the level from the ClassifierConfig (or name an internal constant)")


@pytest.mark.parametrize("snippet, expected", [
    ("if loss >= 15.0:\n    pass\n", [(1, 15.0)]),
    ("x = m.dns.loss_pct >= 25\n", [(1, 25)]),
    ("def f(ts, last):\n    return ts - last > 90.0\n", [(2, 90.0)]),
    ("LIMIT = 40.0\n", []),                      # a named constant is allowed
    ("limit = 40.0\n", [(1, 40.0)]),             # ... but not a lower-case name
    ("def f():\n    LIMIT = 40.0\n", [(2, 40.0)]),  # ... nor one inside a function
    ("x = 15.5 + 4\n", []),
    ("s = '15% loss'\n", []),                    # text is fine
])
def test_the_check_itself(snippet, expected):
    assert leftover_levels(snippet) == expected
