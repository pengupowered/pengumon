"""Guards on how packages are laid out: a package whose modules are imported one at a time holds nothing in its
__init__.py but the docstring, so importing one module does not load the rest."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pengumon"


def test_package_inits_hold_only_their_docstring():
    """Importing one module of these packages (a settings module, a route module, a shared type) must not load the
    rest of the package: import from the submodule."""
    for package in ("api", "devices", "services", "clients", "notify", "models", "speedtest", "speedtest/providers"):
        init = ast.parse((SRC / package / "__init__.py").read_text(encoding="utf-8"))
        assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in init.body), package


def _imported_modules(tree: ast.AST):
    """Every module an import statement anywhere in `tree` names (also inside functions), with `from x import y` as
    both x and x.y (y may be a module)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def test_nothing_outside_api_but_main_imports_api():
    """docs/architecture.md: the api package is the top layer. What a route and a lower layer (the MCP tools, say) share
    lives in the feature packages, not in a route module."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if relative.parts[0] == "api" or relative == Path("main.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(m == "pengumon.api" or m.startswith("pengumon.api.") for m in _imported_modules(tree)):
            offenders.append(str(relative))
    assert offenders == [], f"these import pengumon.api: {offenders}"
