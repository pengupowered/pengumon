"""Turns the failures in pytest JUnit XML reports into GitHub Actions error annotations, so a failed CI run can be read
without signing in: annotations are public through the REST API (check-runs/<job id>/annotations), logs are not.

    python scripts/junit-annotations.py [--title-prefix TEXT] [--start N] [--last] report.xml [more.xml ...]

Prints one `::error file=...,line=...,title=...::message` line per failed or errored test case, with the message
trimmed to about 1000 characters (its start and its end, where pytest puts the assertion). GitHub keeps only 10
error annotations per step, so one call prints at most 10, from failure number --start on; CI calls it three times
(--start 0, 10, 20) for the first 30. With --last, when more failures follow, the tenth line instead names the rest.
Missing or unreadable reports are skipped with a note on stderr. Standard library only; always exits 0 (the test step
itself carries the failure). Use pytest's `-o junit_family=xunit1`, which records each case's file and line.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from typing import Iterator, Optional, Tuple

PER_STEP = 10  # GitHub's limit of error annotations per step
MAX_MESSAGE = 1000


def escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(text: str) -> str:
    return escape_data(text).replace(":", "%3A").replace(",", "%2C")


def trim(text: str, limit: int = MAX_MESSAGE) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit * 3 // 10
    return f"{text[:head]}\n[... {len(text) - limit} characters cut ...]\n{text[-(limit - head):]}"


def failures(path: str) -> Iterator[Tuple[Optional[str], Optional[str], str, str]]:
    """(file, line, title, message) of each failed or errored case in one report."""
    for case in ET.parse(path).getroot().iter("testcase"):
        for problem in list(case):
            if problem.tag not in ("failure", "error"):
                continue
            name = ".".join(p for p in (case.get("classname"), case.get("name")) if p)
            summary = problem.get("message") or ""
            body = problem.text or ""
            message = summary if not body else (body if summary and summary in body else f"{summary}\n{body}")
            file = case.get("file")
            yield (file.replace("\\", "/") if file else None, case.get("line"),
                   f"{problem.tag}: {name}", message)


def annotation(file: Optional[str], line: Optional[str], title: str, message: str) -> str:
    props = []
    if file:
        props.append(f"file={escape_property(file)}")
        if line and line.isdigit():
            props.append(f"line={int(line) + 1}")  # pytest counts lines from 0
    props.append(f"title={escape_property(title[:200])}")
    return f"::error {','.join(props)}::{escape_data(trim(message))}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title-prefix", default="")
    parser.add_argument("--start", type=int, default=0, help="the first failure to print (0-based)")
    parser.add_argument("--last", action="store_true", help="name the failures after these in a last line")
    parser.add_argument("reports", nargs="+")
    args = parser.parse_args(argv)
    found = []
    for path in args.reports:
        try:
            found += list(failures(path))
        except (OSError, ET.ParseError) as exc:
            print(f"junit-annotations: skipped {path}: {exc}", file=sys.stderr)
    chunk = found[args.start:args.start + PER_STEP]
    rest = found[args.start + PER_STEP:]
    if args.last and rest:
        chunk, rest = chunk[:PER_STEP - 1], found[args.start + PER_STEP - 1:]
    for file, line, title, message in chunk:
        print(annotation(file, line, f"{args.title_prefix}{title}", message))
    if args.last and rest:
        names = ", ".join(title.split(": ", 1)[-1] for _f, _l, title, _m in rest)
        print(f"::error title={escape_property(args.title_prefix + f'{len(rest)} more failed tests')}::"
              f"{escape_data(trim(names, 3000))}")
    print(f"junit-annotations: {len(found)} failed tests", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
