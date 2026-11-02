"""Opt-in ESLint run over the dashboard modules (PENGUMON_UI_SMOKE=1, the same switch as the browser smoke test).

Module scripts are strict, so a forgotten import, an undeclared variable or an assignment to an imported binding is a
runtime ReferenceError/TypeError, not a quiet global; ESLint's no-undef and friends (tests/ui/eslint.config.mjs) catch
it without a browser. ESLint and `globals` are not dependencies of the project: they are pinned in
tests/ui/runner/package-lock.json and come from the directory in $PGBROWSER, next to puppeteer-core. Supported way to run it:
scripts/test-ui.sh (container built from Dockerfile.ui). Alternative: install the lockfile into a directory of your own
(`npm ci --ignore-scripts` in a copy of tests/ui/runner) and point $PGBROWSER at it. Skipped unless node and that eslint exist.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PGBROWSER = os.environ.get("PGBROWSER") or str(Path.home() / "pgbrowser")
ESLINT = Path(PGBROWSER) / "node_modules" / "eslint" / "bin" / "eslint.js"

pytestmark = pytest.mark.skipif(
    os.environ.get("PENGUMON_UI_SMOKE") != "1" or not shutil.which("node")
    or not ESLINT.is_file() or not (Path(PGBROWSER) / "node_modules" / "globals").is_dir(),
    reason="run scripts/test-ui.sh, or set PENGUMON_UI_SMOKE=1 with node and $PGBROWSER (eslint and globals)")


def test_dashboard_modules_pass_eslint():
    run = subprocess.run(
        ["node", str(ESLINT), "-c", str(ROOT / "tests" / "ui" / "eslint.config.mjs"), *(str(ROOT / "src" / "pengumon" / "web" / "static" / d) for d in ("js", "panels", "dialogs"))],
        env={**os.environ, "PGBROWSER": PGBROWSER}, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
