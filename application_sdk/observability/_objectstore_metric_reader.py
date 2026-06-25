"""Factory for the ObjectStore-backed periodic metric reader.

Pairs :class:`ObjectStoreMetricExporter` with OTel's
``PeriodicExportingMetricReader`` so the caller just gets back a
ready-to-use reader to plug into the ``MeterProvider``.
"""

from __future__ import annotations

from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from application_sdk.observability._objectstore_metric_exporter import (
    ObjectStoreMetricExporter,
)


def create_objectstore_metric_reader(
    *,
    export_interval_millis: int = 60_000,
    export_timeout_millis: int = 30_000,
) -> PeriodicExportingMetricReader:
    """Create a ``PeriodicExportingMetricReader`` backed by ObjectStore.

    Default export interval is 60 s — deliberately longer than the
    Pushgateway interval (30 s) because ObjectStore writes are heavier
    and the data is durable.
    """
    return PeriodicExportingMetricReader(
        ObjectStoreMetricExporter(),
        export_interval_millis=export_interval_millis,
        export_timeout_millis=export_timeout_millis,
    )
