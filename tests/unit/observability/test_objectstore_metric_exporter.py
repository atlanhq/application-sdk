"""Tests for ObjectStoreMetricExporter."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.resources import Resource

from application_sdk.observability._objectstore_metric_exporter import (
    ObjectStoreMetricExporter,
    _serialize_metrics_data,
)


@pytest.fixture()
def resource() -> Resource:
    return Resource.create(
        {"app.name": "test-app", "app.version": "1.0.0", "service.name": "test"}
    )


@pytest.fixture()
def exporter(tmp_path) -> ObjectStoreMetricExporter:
    return ObjectStoreMetricExporter(data_dir=str(tmp_path))


@pytest.fixture()
def in_memory_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader(
        preferred_temporality={
            # Match the exporter's temporality so we can reuse the same
            # MetricsData for serialization tests.
        }
    )


@pytest.fixture()
def provider(resource: Resource, in_memory_reader: InMemoryMetricReader):
    p = MeterProvider(resource=resource, metric_readers=[in_memory_reader])
    yield p
    try:
        p.shutdown()
    except Exception:  # noqa: S110 — shutdown may fail if already cleaned up
        pass


class TestSerialization:
    """Verify MetricsData → JSON serialization."""

    def test_counter_serialized(
        self, provider: MeterProvider, in_memory_reader: InMemoryMetricReader
    ) -> None:
        meter = provider.get_meter("test")
        counter = meter.create_counter(
            "test.counter", unit="1", description="A test counter"
        )
        counter.add(5, {"endpoint": "/api"})

        metrics_data = in_memory_reader.get_metrics_data()
        records = _serialize_metrics_data(metrics_data)

        assert len(records) == 1
        r = records[0]
        assert r["name"] == "test.counter"
        assert r["type"] == "sum"
        assert r["value"] == 5
        assert r["attributes"]["endpoint"] == "/api"
        assert r["resource_attributes"]["app.name"] == "test-app"
        assert r["description"] == "A test counter"
        assert r["unit"] == "1"

    def test_histogram_serialized(
        self, provider: MeterProvider, in_memory_reader: InMemoryMetricReader
    ) -> None:
        meter = provider.get_meter("test")
        hist = meter.create_histogram("test.duration", unit="s")
        hist.record(0.5, {"status": "ok"})
        hist.record(1.5, {"status": "ok"})

        metrics_data = in_memory_reader.get_metrics_data()
        records = _serialize_metrics_data(metrics_data)

        hist_records = [r for r in records if r["name"] == "test.duration"]
        assert len(hist_records) == 1
        r = hist_records[0]
        assert r["type"] == "histogram"
        assert r["value"]["count"] == 2
        assert r["value"]["sum"] == 2.0
        assert isinstance(r["value"]["bucket_counts"], list)
        assert isinstance(r["value"]["explicit_bounds"], list)

    def test_gauge_serialized(
        self, provider: MeterProvider, in_memory_reader: InMemoryMetricReader
    ) -> None:
        meter = provider.get_meter("test")
        gauge = meter.create_gauge("test.gauge")
        gauge.set(42.0, {"region": "us-east"})

        metrics_data = in_memory_reader.get_metrics_data()
        records = _serialize_metrics_data(metrics_data)

        gauge_records = [r for r in records if r["name"] == "test.gauge"]
        assert len(gauge_records) == 1
        r = gauge_records[0]
        assert r["type"] == "gauge"
        assert r["value"] == 42.0

    def test_empty_metrics_returns_empty_list(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
    ) -> None:
        # No metrics recorded — should produce an empty list.
        metrics_data = in_memory_reader.get_metrics_data()
        records = _serialize_metrics_data(metrics_data)
        assert records == []


class TestExport:
    """Verify the export() method writes files and calls upload."""

    @patch(
        "application_sdk.observability._objectstore_metric_exporter.ENABLE_OBSERVABILITY_STORE_SINK",
        True,
    )
    @patch(
        "application_sdk.observability._objectstore_metric_exporter._upload_sync",
    )
    def test_export_writes_ndjson_gz(
        self,
        mock_upload: AsyncMock,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        meter = provider.get_meter("test")
        counter = meter.create_counter("export.test")
        counter.add(1, {"k": "v"})

        metrics_data = in_memory_reader.get_metrics_data()
        result = exporter.export(metrics_data)

        from opentelemetry.sdk.metrics.export import MetricExportResult

        assert result == MetricExportResult.SUCCESS
        assert mock_upload.called
        # Verify the local file was cleaned up
        local_path = mock_upload.call_args[0][0]
        assert not os.path.exists(local_path)

    @patch(
        "application_sdk.observability._objectstore_metric_exporter.ENABLE_OBSERVABILITY_STORE_SINK",
        False,
    )
    def test_export_noop_when_sink_disabled(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        meter = provider.get_meter("test")
        counter = meter.create_counter("noop.test")
        counter.add(1)

        metrics_data = in_memory_reader.get_metrics_data()
        from opentelemetry.sdk.metrics.export import MetricExportResult

        result = exporter.export(metrics_data)
        assert result == MetricExportResult.SUCCESS

    @patch(
        "application_sdk.observability._objectstore_metric_exporter.ENABLE_OBSERVABILITY_STORE_SINK",
        True,
    )
    @patch(
        "application_sdk.observability._objectstore_metric_exporter._upload_sync",
        side_effect=Exception("upload boom"),
    )
    def test_export_swallows_upload_failure(
        self,
        mock_upload: AsyncMock,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        meter = provider.get_meter("test")
        counter = meter.create_counter("fail.test")
        counter.add(1)

        metrics_data = in_memory_reader.get_metrics_data()
        from opentelemetry.sdk.metrics.export import MetricExportResult

        result = exporter.export(metrics_data)
        assert result == MetricExportResult.SUCCESS


class TestExporterTemporality:
    """Verify delta temporality preference."""

    def test_preferred_temporality_is_delta_for_sum(self) -> None:
        from opentelemetry.sdk.metrics.export import Sum

        e = ObjectStoreMetricExporter()
        assert e._preferred_temporality.get(Sum) == AggregationTemporality.DELTA
