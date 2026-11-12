"""Prometheus text exposition (format 0.0.4), written by hand so no dependency is needed."""

import inspect
import logging
from typing import Dict, List, Optional, Tuple

from pengumon.observability.gauges import GAUGES

logger = logging.getLogger("pengumon.observability")


def escape_label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value: float) -> str:
    return repr(float(value))


class _Writer:
    def __init__(self):
        self.lines: List[str] = []

    def metric(self, name: str, help_text: str, kind: str, samples: List[Tuple[Dict[str, str], Optional[float]]]):
        """Samples with a None value are left out; a metric without samples is not declared at all."""
        samples = [(l, v) for l, v in samples if v is not None]
        if not samples:
            return
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            label_text = ""
            if labels:
                label_text = "{" + ",".join(f'{k}="{escape_label(v)}"' for k, v in labels.items()) + "}"
            self.lines.append(f"{name}{label_text} {_fmt(value)}")


async def render_metrics(state) -> str:
    """`state` is the app's pengumon.state.AppState. One block per gauge of observability.gauges.GAUGES."""
    w = _Writer()
    for gauge in GAUGES:
        try:
            samples = gauge.collect(state)
            if inspect.isawaitable(samples):
                samples = await samples
        except Exception as e:  # one broken gauge must not take the whole scrape down
            logger.warning(f"Metric {gauge.prom} skipped: {type(e).__name__}: {e}")
            continue
        w.metric(gauge.prom, gauge.help, "gauge", samples)
    return "\n".join(w.lines) + "\n"
