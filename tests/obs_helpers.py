"""Reading pengumon's own OpenTelemetry instruments in a test.

OpenTelemetry lets a process set its meter provider once. `install()` sets one with an InMemoryMetricReader the first
time it is called; the instruments in pengumon.observability.telemetry were made on the API's proxy meter and start
recording into it. The reader is cumulative and shared by the whole test run, so a test either uses attribute values of its
own (a device id nothing else uses) or takes `total` before and after.
"""

from typing import Any, Dict, Optional

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

_reader: Optional[InMemoryMetricReader] = None


def install() -> InMemoryMetricReader:
    global _reader
    if _reader is None:
        _reader = InMemoryMetricReader()
        metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))
    return _reader


def points(name: str) -> list:
    """Every data point of the instrument `name` (a list of (attributes dict, point))."""
    found = []
    data = install().get_metrics_data()
    for rm in (data.resource_metrics if data else ()):
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    found += [(dict(p.attributes or {}), p) for p in metric.data.data_points]
    return found


def _matches(attributes: Dict[str, Any], wanted: Dict[str, Any]) -> bool:
    return all(attributes.get(k) == v for k, v in wanted.items())


def total(name: str, **wanted) -> float:
    """The sum of a counter's points whose attributes include `wanted`; the observation count of a histogram."""
    value = 0.0
    for attributes, point in points(name):
        if _matches(attributes, wanted):
            value += point.count if hasattr(point, "count") else point.value
    return value


def histogram_sum(name: str, **wanted) -> float:
    return sum(p.sum for a, p in points(name) if _matches(a, wanted))


class FakeSpan:
    def __init__(self, name, attributes):
        self.name, self.attributes, self.status = name, dict(attributes or {}), None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_attributes(self, attributes):
        self.attributes.update(attributes)

    def set_status(self, status):
        self.status = status

    def record_exception(self, exc):
        pass


class FakeTracer:
    """Stands in for a module's `tracer`: keeps every span started, with the attributes it ended with. (A tracer
    provider is global and set once; the spans of the code under test are looked at through this instead.)"""

    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, attributes=None, **_):
        from contextlib import contextmanager

        @contextmanager
        def manager():
            span = FakeSpan(name, attributes)
            self.spans.append(span)
            yield span

        return manager()

    def named(self, name):
        return [s for s in self.spans if s.name == name]
