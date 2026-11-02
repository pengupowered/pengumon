"""Golden files, one per scenario: tests/golden/<suite>/<scenario>.json.

A characterization test builds {scenario: value} and calls check_golden(suite, scenarios). Each value is stored in
its own file, so two branches that change different scenarios do not touch the same file.

Regenerate only on purpose: PENGUMON_REGEN_GOLDEN=1 python -m pytest tests/test_<suite>.py rewrites the suite's
files (and removes files of scenarios that no longer exist). Review the diff before committing it.

A mismatch is reported per scenario with the first differences, as paths into the JSON.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

GOLDEN_DIR = Path(__file__).parent / "golden"
SHOWN_DIFFERENCES = 4  # per scenario
SHOWN_SCENARIOS = 4


def dump(value: Any) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def first_differences(a: Any, b: Any, path: str, out: List[str]) -> None:
    if type(a) is not type(b):
        out.append(f"{path or '/'}: {a!r} != {b!r}")
    elif isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            first_differences(a.get(key), b.get(key), f"{path}/{key}", out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path or '/'}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            first_differences(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path or '/'}: {a!r} != {b!r}")


def check_golden(suite: str, scenarios: Dict[str, Any]) -> None:
    folder = GOLDEN_DIR / suite
    actual = {name: dump(value) for name, value in scenarios.items()}
    if os.environ.get("PENGUMON_REGEN_GOLDEN") == "1":
        folder.mkdir(parents=True, exist_ok=True)
        for stale in folder.glob("*.json"):
            if stale.stem not in actual:
                stale.unlink()
        for name, text in actual.items():
            (folder / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
    recorded = {path.stem: path for path in folder.glob("*.json")} if folder.is_dir() else {}
    problems: List[str] = []
    for name in sorted(set(recorded) - set(actual)):
        problems.append(f"{suite}/{name}: recorded but no longer produced")
    for name in sorted(set(actual) - set(recorded)):
        problems.append(f"{suite}/{name}: produced but not recorded")
    different = 0
    for name in sorted(set(actual) & set(recorded)):
        expected_text = recorded[name].read_text(encoding="utf-8")
        if expected_text == actual[name]:
            continue
        different += 1
        if different > SHOWN_SCENARIOS:
            continue
        diffs: List[str] = []
        first_differences(json.loads(expected_text), json.loads(actual[name]), "", diffs)
        if not diffs:  # same values, different formatting
            diffs = ["formatting differs"]
        problems.append(f"{suite}/{name} differs:\n  " + "\n  ".join(diffs[:SHOWN_DIFFERENCES]))
    if different > SHOWN_SCENARIOS:
        problems.append(f"... and {different - SHOWN_SCENARIOS} more scenarios differ")
    if problems:
        raise AssertionError(f"{suite} golden differs:\n" + "\n".join(problems))
