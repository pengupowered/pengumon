"""Guards on how packages are laid out: a package whose modules are imported one at a time holds nothing in its
__init__.py but the docstring, so importing one module does not load the rest."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pengumon"


def test_package_inits_hold_only_their_docstring():
    """Importing one module of these packages (a settings module, a route module, a shared type) must not load the
    rest of the package: import from the submodule."""
    for package in ("api", "devices", "services", "clients", "notify", "models"):
        init = ast.parse((SRC / package / "__init__.py").read_text(encoding="utf-8"))
        assert all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in init.body), package
