"""Validation messages in plain words, for the Settings page and its import notes. The checks are written for the code
that raises them ("degraded_loss_pct must be below down_loss_pct", "Input should be greater than 0"); a person looking at
the page knows the settings by their labels and numbers by "at least"."""

import re
from collections import Counter
from typing import Dict, Optional

_NUMBER = (
    (re.compile(r"Input should be greater than or equal to (\S+)"), r"Enter at least \1"),
    (re.compile(r"Input should be less than or equal to (\S+)"), r"Enter at most \1"),
    (re.compile(r"Input should be greater than (\S+)"), r"Enter more than \1"),
    (re.compile(r"Input should be less than (\S+)"), r"Enter less than \1"),
    (re.compile(r"Input should be a valid (number|integer)[^.]*"), r"Enter a \1"),
)
_SUBJECT = (  # a message that starts with a field name of a list item
    (re.compile(r"^grafana_url\b"), "The Grafana address"),
    (re.compile(r"^url\b"), "The address"),
    (re.compile(r"^nameserver\b"), "The name server"),
    (re.compile(r"^color\b"), "The colour"),
)
_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_labels: Optional[Dict[str, str]] = None


def _leaf_labels() -> Dict[str, str]:
    """Last part of a setting's key -> its label, for the names that only one setting has."""
    global _labels
    if _labels is None:
        from pengumon.settings import registry
        leaf = Counter(s.key.rsplit(".", 1)[-1] for s in registry.SETTINGS)
        _labels = {s.key.rsplit(".", 1)[-1]: s.label for s in registry.SETTINGS if leaf[s.key.rsplit(".", 1)[-1]] == 1}
    return _labels


def plain(text: str, scope: str = "") -> str:
    """`text` with the code's words swapped for the page's: numbers as "Enter at least 1", a setting's name as its label.
    `scope` is the config section the message comes from ("classifier"): a name that two settings share
    (`degraded_loss_pct` is in classifier and uptime) is the one under that section, by its full key."""
    for pattern, repl in _NUMBER:
        text = pattern.sub(repl, text)
    text = re.sub(r"^[a-z_]+: (?=[a-z_]+ must)", "", text)  # "line_signal: x must be below y"
    for pattern, subject in _SUBJECT:
        text = pattern.sub(subject, text, count=1)
    labels = _leaf_labels()
    from pengumon.settings import registry

    def label(m):
        name = m.group(0)
        spec = registry.BY_KEY.get(f"{scope}.{name}") if scope else None
        found = spec.label if spec else labels.get(name)
        return f"“{found}”" if found else name

    text = _NAME.sub(label, text)
    return text[:1].upper() + text[1:] if text else text
