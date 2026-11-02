"""Guards on where the shared value types live: there is no catch-all `pengumon.models` module to import from, and
nothing imports a path that was removed."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pengumon"

# Import paths that no longer exist; add a path here when a module is removed or moved.
REMOVED_PATHS = {"pengumon.models"}


def _imported_modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for alias in node.names:  # `from pengumon import models`
                yield node.lineno, f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


def _python_files():
    for base in (SRC, ROOT / "tests", ROOT / "scripts"):
        if base.is_dir():
            yield from base.rglob("*.py")


def test_no_module_imports_a_removed_path():
    offenders = []
    for path in _python_files():
        if path == Path(__file__).resolve():
            continue
        for lineno, module in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if module in REMOVED_PATHS:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {module}")
    assert not offenders, "\n".join(offenders)


def test_models_is_a_package_of_shared_types_with_nothing_re_exported():
    assert not (SRC / "models.py").exists()
    init = ast.parse((SRC / "models" / "__init__.py").read_text(encoding="utf-8"))
    assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in init.body), \
        "models/__init__.py holds only its docstring: import from the submodule"
    assert sorted(p.stem for p in (SRC / "models").glob("*.py") if p.stem != "__init__") == ["hosts", "probes"]


def test_feature_package_inits_hold_only_their_docstring():
    """Importing one module of these packages (a settings module, say) must not load the rest of the package."""
    for package in ("devices", "services", "clients"):
        init = ast.parse((SRC / package / "__init__.py").read_text(encoding="utf-8"))
        assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in init.body), package
