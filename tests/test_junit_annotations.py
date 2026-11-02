"""scripts/junit-annotations.py: pytest failures as GitHub error annotations (CI's readable-without-login report)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("junit_annotations", ROOT / "scripts" / "junit-annotations.py")
ja = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ja)

REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" tests="3">
  <testcase classname="tests.test_x" name="test_ok" file="tests/test_x.py" line="3" time="0.1"/>
  <testcase classname="tests.test_x" name="test_bad[a,b]" file="tests\\test_x.py" line="9" time="0.1">
    <failure message="AssertionError: 1 == 2">def test_bad():
&gt;       assert 1 == 2
E       AssertionError: 1 == 2 (100% sure)</failure></testcase>
  <testcase classname="tests.test_y" name="test_setup" time="0.0"><error message="fixture 'nope' not found">E  fixture 'nope' not found</error></testcase>
</testsuite></testsuites>
"""


def test_each_failure_becomes_one_error_line(tmp_path, capsys):
    report = tmp_path / "unit.xml"
    report.write_text(REPORT, encoding="utf-8")
    assert ja.main(["--title-prefix", "linux: ", str(report)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    first, second = lines
    # file with forward slashes, 1-based line, title properties escaped (",", ":"), message newlines as %0A
    assert first.startswith("::error file=tests/test_x.py,line=10,title=linux%3A failure%3A tests.test_x.test_bad[a%2Cb]::")
    assert "%0A" in first and "\n" not in first and "(100%25 sure)" in first
    assert second.startswith("::error title=linux%3A error%3A tests.test_y.test_setup::")


def test_long_messages_keep_their_start_and_end():
    text = "start " + "x" * 5000 + " the assertion"
    trimmed = ja.trim(text)
    assert len(trimmed) < 1100 and trimmed.startswith("start ") and trimmed.endswith(" the assertion")


def test_ten_per_step_and_the_last_step_names_the_rest(tmp_path, capsys):
    """GitHub keeps 10 error annotations per step: three calls show the first 29 and name the other 6."""
    cases = "".join(f'<testcase classname="t" name="c{i}" file="t.py" line="{i}"><failure message="m{i}"/></testcase>'
                    for i in range(35))
    report = tmp_path / "many.xml"
    report.write_text(f"<testsuite>{cases}</testsuite>", encoding="utf-8")
    shown = []
    for start, last in ((0, False), (10, False), (20, True)):
        ja.main(["--start", str(start), *(["--last"] if last else []), str(report)])
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 10
        shown += lines
    assert [line.split("::")[-1] for line in shown[:29]] == [f"m{i}" for i in range(29)]
    assert shown[-1].startswith("::error title=6 more failed tests::t.c29, t.c30")
    ja.main(["--start", "30", "--last", str(report)])  # past the end of the list: nothing more to say
    assert len(capsys.readouterr().out.splitlines()) == 5


def test_a_missing_report_is_skipped_and_the_exit_is_zero(tmp_path):
    run = subprocess.run([sys.executable, str(ROOT / "scripts" / "junit-annotations.py"), str(tmp_path / "none.xml")],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout == "" and "skipped" in run.stderr
