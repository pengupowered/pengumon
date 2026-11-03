"""README.md is also the PyPI project description, and PyPI cannot resolve relative paths or GitHub-only syntax:
every image and link must be absolute (or an in-page #anchor, or mailto:), and no GitHub alert marker may appear."""

import re
from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def _targets(text: str) -> list[str]:
    """Every link and image target: Markdown `](...)` and HTML `href=`/`src=`, outside fenced code blocks."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    found = re.findall(r"\]\(\s*<?([^)\s>]+)", text)
    found += re.findall(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", text)
    return found


def test_readme_has_targets():
    assert len(_targets(README)) > 20  # the scan itself works


def test_readme_links_and_images_are_absolute():
    relative = [t for t in _targets(README) if not re.match(r"(https?://|mailto:|#)", t)]
    assert not relative, f"relative targets break on PyPI: {relative}"


def test_readme_has_no_github_alerts():
    assert not re.search(r"^\s*>\s*\[!\w+\]", README, flags=re.M)
