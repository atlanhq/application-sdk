"""Tests for ObjectStoreMetricExporter."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    InMemoryMetricReader,
)
from opentelemetry.sdk.resources import Resource

from application_sdk.observability._objectstore_metric_exporter import (
    ObjectStoreMetricExporter,
    _serialize_metrics_data,
)
from application_sdk.observability._objectstore_metric_reader import (
    create_objectstore_metric_reader,
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
    def test_export_writes_ndjson_gz(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        mock_store = MagicMock()
        exporter._deployment_store = mock_store

        meter = provider.get_meter("test")
        counter = meter.create_counter("export.test")
        counter.add(1, {"k": "v"})

        metrics_data = in_memory_reader.get_metrics_data()
        result = exporter.export(metrics_data)

        from opentelemetry.sdk.metrics.export import MetricExportResult

        assert result == MetricExportResult.SUCCESS
        assert mock_store.put.called
        # Verify the local file was cleaned up
        uploaded_path = mock_store.put.call_args[0][1]
        assert not os.path.exists(uploaded_path)

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
    def test_export_swallows_upload_failure(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        mock_store = MagicMock()
        mock_store.put.side_effect = Exception("upload boom")
        exporter._deployment_store = mock_store

        meter = provider.get_meter("test")
        counter = meter.create_counter("fail.test")
        counter.add(1)

        metrics_data = in_memory_reader.get_metrics_data()
        from opentelemetry.sdk.metrics.export import MetricExportResult

        result = exporter.export(metrics_data)
        assert result == MetricExportResult.SUCCESS


class TestExporterTemporality:
    """Verify delta temporality preference."""

    def test_preferred_temporality_uses_otel_instrument_classes(self) -> None:
        e = ObjectStoreMetricExporter()
        assert e._preferred_temporality.get(Counter) == AggregationTemporality.DELTA
        assert e._preferred_temporality.get(UpDownCounter) == (
            AggregationTemporality.DELTA
        )
        assert e._preferred_temporality.get(ObservableCounter) == (
            AggregationTemporality.DELTA
        )
        assert e._preferred_temporality.get(ObservableUpDownCounter) == (
            AggregationTemporality.DELTA
        )
        assert e._preferred_temporality.get(Histogram) == AggregationTemporality.DELTA

    def test_reader_registers_with_real_meter_provider(self) -> None:
        reader = create_objectstore_metric_reader(export_interval_millis=3_600_000)
        provider = MeterProvider(metric_readers=[reader])
        try:
            assert provider.get_meter("test")
        finally:
            provider.shutdown()


_EXPORTER_MOD = "application_sdk.observability._objectstore_metric_exporter"


class TestSdrUpstreamExport:
    """SDR-mode (ENABLE_ATLAN_UPLOAD=true) upstream-store coverage.

    The existing ``TestExport`` cases exercise only the deployment store. In SDR
    mode the exporter must *also* push each metric file to the upstream (Atlan)
    store, under an ``sdr/prometheus-metrics`` path, and swallow upstream upload
    failures so metric collection is never blocked.
    """

    @patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", True)
    @patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True)
    def test_export_uploads_to_both_stores_under_sdr_path(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        deployment_store = MagicMock(name="deployment_store")
        upstream_store = MagicMock(name="upstream_store")
        exporter._deployment_store = deployment_store
        exporter._upstream_store = upstream_store

        meter = provider.get_meter("test")
        counter = meter.create_counter("sdr.export.test")
        counter.add(1, {"k": "v"})

        metrics_data = in_memory_reader.get_metrics_data()
        result = exporter.export(metrics_data)

        from opentelemetry.sdk.metrics.export import MetricExportResult

        assert result == MetricExportResult.SUCCESS
        # Both stores receive exactly one put with the identical remote key.
        assert deployment_store.put.call_count == 1
        assert upstream_store.put.call_count == 1
        dep_key = deployment_store.put.call_args[0][0]
        up_key = upstream_store.put.call_args[0][0]
        assert dep_key == up_key
        assert "prometheus-metrics" in up_key
        # The local file is written under the SDR subdirectory.
        local_path = str(upstream_store.put.call_args[0][1])
        assert os.path.join("sdr", "prometheus-metrics") in local_path
        # Local file cleaned up afterwards.
        assert not os.path.exists(local_path)

    @patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", True)
    @patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True)
    def test_export_swallows_upstream_upload_failure(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        deployment_store = MagicMock(name="deployment_store")
        upstream_store = MagicMock(name="upstream_store")
        upstream_store.put.side_effect = Exception("upstream boom")
        exporter._deployment_store = deployment_store
        exporter._upstream_store = upstream_store

        meter = provider.get_meter("test")
        counter = meter.create_counter("sdr.fail.test")
        counter.add(1)

        metrics_data = in_memory_reader.get_metrics_data()
        from opentelemetry.sdk.metrics.export import MetricExportResult

        # Upstream raising must not block metric collection.
        result = exporter.export(metrics_data)
        assert result == MetricExportResult.SUCCESS
        # Deployment upload was still attempted; upstream failure was swallowed.
        assert deployment_store.put.call_count == 1
        assert upstream_store.put.call_count == 1

    def test_stores_resolved_lazily_not_at_init(self, tmp_path) -> None:
        """Construction must not touch bindings (BLDX-1567 boot race).

        Resolving eagerly in ``__init__`` meant a metrics-flush daemon thread
        that constructed the exporter before the Dapr ``objectstore`` component
        existed permanently disabled uploads. Stores must stay ``None`` until
        the first ``_ensure_stores`` / ``export``.
        """
        with patch.object(ObjectStoreMetricExporter, "_resolve_store") as resolve_spy:
            e = ObjectStoreMetricExporter(data_dir=str(tmp_path))
        resolve_spy.assert_not_called()
        assert e._deployment_store is None
        assert e._upstream_store is None

    def test_ensure_stores_resolves_upstream_only_in_sdr_mode(self, tmp_path) -> None:
        """``_ensure_stores`` resolves the upstream store only in SDR mode."""
        from application_sdk.constants import (
            DEPLOYMENT_OBJECT_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )

        resolved: dict[str, object] = {
            DEPLOYMENT_OBJECT_STORE_NAME: MagicMock(name="deployment"),
            UPSTREAM_OBJECT_STORE_NAME: MagicMock(name="upstream"),
        }

        def _fake_resolve(_self, name):
            return resolved[name]

        # SDR mode: both stores resolved on first ensure.
        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", True),
            patch.object(ObjectStoreMetricExporter, "_resolve_store", _fake_resolve),
        ):
            e = ObjectStoreMetricExporter(data_dir=str(tmp_path))
            e._ensure_stores()
        assert e._deployment_store is resolved[DEPLOYMENT_OBJECT_STORE_NAME]
        assert e._upstream_store is resolved[UPSTREAM_OBJECT_STORE_NAME]

        # Non-SDR mode: upstream store is never built.
        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", False),
            patch.object(ObjectStoreMetricExporter, "_resolve_store", _fake_resolve),
        ):
            e2 = ObjectStoreMetricExporter(data_dir=str(tmp_path))
            e2._ensure_stores()
        assert e2._deployment_store is resolved[DEPLOYMENT_OBJECT_STORE_NAME]
        assert e2._upstream_store is None

    def test_ensure_stores_retries_until_binding_appears(self, tmp_path) -> None:
        """A transient missing binding must not latch uploads off (BLDX-1567).

        First resolution returns ``None`` (component not written yet); a later
        one succeeds. The store must then be cached and not re-resolved.
        """
        store = MagicMock(name="deployment")
        # None on the first attempt (boot race), then the real store.
        attempts = iter([None, store, store])

        def _fake_resolve(_self, _name):
            return next(attempts)

        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", False),
            patch.object(ObjectStoreMetricExporter, "_resolve_store", _fake_resolve),
        ):
            e = ObjectStoreMetricExporter(data_dir=str(tmp_path))
            e._ensure_stores()
            assert e._deployment_store is None  # still absent → retried later
            e._ensure_stores()
            assert e._deployment_store is store  # healed on retry
            e._ensure_stores()
            assert e._deployment_store is store  # cached, not re-resolved

    def test_non_transient_error_logs_once_and_stops_retrying(self, tmp_path) -> None:
        """A non-transient error must be logged once, not every export (BLDX-1567).

        ``StorageConfigError`` (e.g. an unsupported binding type) will never
        heal on retry. The exporter must record the store name, stop retrying
        it, and warn exactly once — not re-log a full traceback every flush.
        """
        from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
        from application_sdk.storage.errors import StorageConfigError

        boom = StorageConfigError("Unsupported binding type: 'nope'")

        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", False),
            patch(f"{_EXPORTER_MOD}.logger") as log,
            patch(
                "application_sdk.storage.binding.create_store_from_binding",
                side_effect=boom,
            ) as create,
        ):
            e = ObjectStoreMetricExporter(data_dir=str(tmp_path))
            e._ensure_stores()
            e._ensure_stores()
            e._ensure_stores()

        # Resolution attempted once, then skipped (name latched off).
        assert create.call_count == 1
        assert DEPLOYMENT_OBJECT_STORE_NAME in e._skip_stores
        assert e._deployment_store is None
        assert log.warning.call_count == 1
        assert log.debug.call_count == 0

    def test_binding_broken_is_transient_and_retried_quietly(self, tmp_path) -> None:
        """A present-but-unresolved binding is a transient boot state (BLDX-1567).

        ``StorageBindingBrokenError`` (placeholders / secret env not yet
        substituted) heals once Dapr finishes boot, so it must be retried
        quietly (debug, never warning) and not latched into ``_skip_stores``.
        """
        from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME
        from application_sdk.storage.errors import StorageBindingBrokenError

        store = MagicMock(name="deployment")
        # Broken on the first attempt (secret env not substituted yet), then healed.
        attempts = [
            StorageBindingBrokenError("unresolved", binding_name="x"),
            store,
        ]

        def _create(_name):
            outcome = attempts.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_EXPORTER_MOD}.ENABLE_ATLAN_UPLOAD", False),
            patch(f"{_EXPORTER_MOD}.logger") as log,
            patch(
                "application_sdk.storage.binding.create_store_from_binding",
                side_effect=_create,
            ),
        ):
            e = ObjectStoreMetricExporter(data_dir=str(tmp_path))
            e._ensure_stores()
            assert e._deployment_store is None  # broken → deferred, not skipped
            assert DEPLOYMENT_OBJECT_STORE_NAME not in e._skip_stores
            e._ensure_stores()
            assert e._deployment_store is store  # healed on retry

        assert log.warning.call_count == 0  # transient must never warn
        assert log.debug.call_count == 1  # logged once, at debug
