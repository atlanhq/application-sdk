"""OTel-native metrics API for app authors.

Apps can emit their own counters, histograms, gauges, and up-down counters
through the same MeterProvider that the SDK's interceptors use:

    from application_sdk.observability import metrics

    requests = metrics.create_counter(
        "myapp.requests",
        description="Requests handled",
        unit="1",
    )
    requests.add(1, {"endpoint": "/foo"})

    latency = metrics.create_histogram(
        "myapp.request.duration",
        description="Request latency",
        unit="s",
    )
    latency.record(0.42, {"endpoint": "/foo"})

The instruments write through the global ``MeterProvider`` configured in
``application_sdk.observability.metrics_adaptor``. They are scraped from
``/metrics`` on long-running servers and pushed via Pushgateway from
short-lived workers — no per-call configuration required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence

from opentelemetry import metrics as _otel_metrics
from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Meter,
    NoOpMeter,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    Observation,
    UpDownCounter,
)

#: Type for observable-instrument callbacks. OTel passes the callback a
#: ``CallbackOptions`` and expects an iterable of ``Observation`` back.
ObservableCallback = Callable[[CallbackOptions], Iterable[Observation]]

_logger = logging.getLogger(__name__)
_warned_noop_meter = False


def _meter() -> Meter:
    """Resolve the global meter, warning once if ``MeterProvider`` is the no-op
    variant. The no-op provider silently drops every observation, which is
    surprising when ``create_*`` is called before ``run_main()`` initialises
    metrics (e.g. in tests, CLI scripts, early-import paths)."""
    meter = _otel_metrics.get_meter("application_sdk.user")
    global _warned_noop_meter
    if not _warned_noop_meter and isinstance(meter, NoOpMeter):
        _warned_noop_meter = True
        _logger.warning(
            "MeterProvider is the no-op default — instruments created now will "
            "drop every observation. Initialise the SDK via run_main() or "
            "AtlanMetricsAdapter() before creating instruments."
        )
    return meter


def create_counter(name: str, *, unit: str = "", description: str = "") -> Counter:
    return _meter().create_counter(name=name, unit=unit, description=description)


def create_up_down_counter(
    name: str, *, unit: str = "", description: str = ""
) -> UpDownCounter:
    return _meter().create_up_down_counter(
        name=name, unit=unit, description=description
    )


def create_histogram(name: str, *, unit: str = "", description: str = "") -> Histogram:
    return _meter().create_histogram(name=name, unit=unit, description=description)


def create_observable_counter(
    name: str,
    callbacks: Sequence[ObservableCallback],
    *,
    unit: str = "",
    description: str = "",
) -> ObservableCounter:
    return _meter().create_observable_counter(
        name=name, callbacks=callbacks, unit=unit, description=description
    )


def create_observable_up_down_counter(
    name: str,
    callbacks: Sequence[ObservableCallback],
    *,
    unit: str = "",
    description: str = "",
) -> ObservableUpDownCounter:
    return _meter().create_observable_up_down_counter(
        name=name, callbacks=callbacks, unit=unit, description=description
    )


def create_observable_gauge(
    name: str,
    callbacks: Sequence[ObservableCallback],
    *,
    unit: str = "",
    description: str = "",
) -> ObservableGauge:
    return _meter().create_observable_gauge(
        name=name, callbacks=callbacks, unit=unit, description=description
    )


__all__ = [
    "create_counter",
    "create_histogram",
    "create_observable_counter",
    "create_observable_gauge",
    "create_observable_up_down_counter",
    "create_up_down_counter",
]
