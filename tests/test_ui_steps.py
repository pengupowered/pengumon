"""Guards on the browser test's step files (tests/ui/guards.mjs): no duplicated top-level helpers, no shadowing of lib.mjs,
a file per existing panel or dialog. They need node only (no browser), so they run in scripts/test-ui.sh's container."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent / "ui"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node (scripts/test-ui.sh)")

STEP = 'export const subject = "core";\nexport const variants = ["full"];\n{extra}\nexport const steps = [];\n'


def check(tmp_path, files: dict[str, str]) -> subprocess.CompletedProcess:
    steps = tmp_path / "steps"
    steps.mkdir()
    for name, extra in files.items():
        (steps / name).write_text(STEP.format(extra=extra), encoding="utf-8")
    return subprocess.run(["node", str(UI / "guards.mjs"), str(steps)], capture_output=True, text=True)


def test_the_real_step_files_pass(tmp_path):
    run = subprocess.run(["node", str(UI / "guards.mjs"), str(UI / "steps")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_two_files_declaring_the_same_helper_fail(tmp_path):
    run = check(tmp_path, {"a.mjs": "const sameHelper = 1;", "b.mjs": "const sameHelper = 2;"})
    assert run.returncode == 1 and "sameHelper" in run.stderr


def test_a_file_shadowing_a_lib_helper_fails(tmp_path):
    run = check(tmp_path, {"a.mjs": "const click = () => 1;"})
    assert run.returncode == 1 and "lib.mjs already provides" in run.stderr


def test_distinct_helpers_pass(tmp_path):
    assert check(tmp_path, {"a.mjs": "const mine = 1;", "b.mjs": "const yours = 2;"}).returncode == 0


def subjects(modules, panels, dialogs) -> list[str]:
    script = ('import { checkSubjects } from "%s"; console.log(JSON.stringify(checkSubjects(%s, %s, %s)));'
              % ((UI / "guards.mjs").as_uri(), json.dumps(modules), json.dumps(panels), json.dumps(dialogs)))
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_a_step_file_for_a_missing_panel_fails():
    assert subjects([{"file": "ghost.mjs", "subject": "panel:ghost"}], ["line"], ["alerts"])
    assert subjects([{"file": "ghost.mjs", "subject": "dialog:ghost"}], ["line"], ["alerts"])
    assert not subjects([{"file": "line.mjs", "subject": "panel:line"}, {"file": "alerts.mjs", "subject": "dialog:alerts"}], ["line"], ["alerts"])


def test_a_file_not_named_after_its_subject_fails():
    assert subjects([{"file": "other.mjs", "subject": "panel:line"}], ["line"], [])
