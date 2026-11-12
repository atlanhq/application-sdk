"""Tests for ObjectStoreMetricExporter."""

from __future__ import annotations

import gzip
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import orjson
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
    MetricExportResult,
)
from opentelemetry.sdk.resources import Resource

from application_sdk.observability._objectstore_metric_exporter import (
    ObjectStoreMetricExporter,
    _serialize_metrics_data,
)
from application_sdk.observability._objectstore_metric_reader import (
    create_objectstore_metric_reader,
)
from application_sdk.observability.observability import (
    OBSERVABILITY_S3_PREFIX_MAP,
    AtlanObservability,
)
from application_sdk.storage.ops import BoundStore


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


_EXPORTER_MOD = "application_sdk.observability._objectstore_metric_exporter"
_OBS_MOD = "application_sdk.observability.observability"
_PREFIX = OBSERVABILITY_S3_PREFIX_MAP["prometheus-metrics"]


@pytest.fixture(autouse=True)
def _fresh_shared_stores():
    """The shared store cache and its log-once state are process-wide."""
    AtlanObservability._reset_for_testing()
    yield
    AtlanObservability._reset_for_testing()


def _record_counter(provider: MeterProvider, reader: InMemoryMetricReader, name: str):
    provider.get_meter("test").create_counter(name).add(1, {"k": "v"})
    return reader.get_metrics_data()


class _UploadSpy:
    """Fake ``upload_file``: records each call and the uploaded file's records."""

    def __init__(self, fail_for: object | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.records: list[dict] = []
        self._fail_for = fail_for

    async def __call__(self, key, local_path, store=None, **_kw):
        self.calls.append((key, str(local_path), store))
        with gzip.open(local_path, "rb") as fh:
            self.records.extend(orjson.loads(line) for line in fh if line.strip())
        if self._fail_for is not None and store is self._fail_for:
            raise OSError("upload boom")


def _patch_stores(
    upload: _UploadSpy,
    *,
    deployment: object | None = None,
    upstream: object | None = None,
    atlan_upload: bool = False,
):
    stack = ExitStack()
    stack.enter_context(patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True))
    stack.enter_context(patch(f"{_OBS_MOD}.ENABLE_ATLAN_UPLOAD", atlan_upload))
    stack.enter_context(patch("application_sdk.storage.upload_file", new=upload))
    if deployment is not None:
        stack.enter_context(
            patch.object(
                AtlanObservability, "_get_deployment_store", return_value=deployment
            )
        )
    if upstream is not None:
        stack.enter_context(
            patch.object(
                AtlanObservability, "_get_upstream_store", return_value=upstream
            )
        )
    return stack


class TestExport:
    """``export()`` writes through the shared observability writer."""

    def test_export_uploads_through_upload_file_with_put_attrs(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        """The deployment store is the shared ``BoundStore`` (its put
        attributes carry a binding's ``storageClass``), and the upload goes
        through ``upload_file`` rather than a raw ``store.put``."""
        bound = BoundStore(MagicMock(name="obstore"), {"Storage-Class": "STANDARD_IA"})
        upload = _UploadSpy()
        metrics_data = _record_counter(provider, in_memory_reader, "export.test")

        with _patch_stores(upload, deployment=bound):
            result = exporter.export(metrics_data)

        assert result == MetricExportResult.SUCCESS
        assert len(upload.calls) == 1
        key, local_path, store = upload.calls[0]
        assert store is bound
        assert key.startswith(_PREFIX + "/year=")
        assert key.endswith(".json.gz")
        assert [r["name"] for r in upload.records] == ["export.test"]
        # The local file was cleaned up.
        assert not os.path.exists(local_path)

    def test_export_noop_when_sink_disabled(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        metrics_data = _record_counter(provider, in_memory_reader, "noop.test")
        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", False),
            patch(f"{_OBS_MOD}._write_and_upload_batch") as write,
            patch(f"{_EXPORTER_MOD}._write_and_upload_batch", write),
        ):
            result = exporter.export(metrics_data)
        assert result == MetricExportResult.SUCCESS
        write.assert_not_called()

    def test_export_swallows_upload_failure(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
        tmp_path,
    ) -> None:
        deployment = MagicMock(name="deployment_store")
        upload = _UploadSpy(fail_for=deployment)
        metrics_data = _record_counter(provider, in_memory_reader, "fail.test")

        with _patch_stores(upload, deployment=deployment):
            result = exporter.export(metrics_data)

        assert result == MetricExportResult.SUCCESS
        assert len(upload.calls) == 1
        assert list(tmp_path.rglob("*.json.gz")) == []

    def test_export_honours_dapr_components_path(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end through the real binding and ``upload_file``: the file
        lands in the store the ``DAPR_COMPONENTS_PATH`` component names, as for
        every other observability signal. The exporter used to read
        ``./components`` only, so under embedded Dapr it never uploaded."""
        components = tmp_path / "components"
        components.mkdir()
        root = tmp_path / "objectstore"
        (components / "objectstore.yaml").write_text(
            _LOCAL_COMPONENT.format(root=root.as_posix()), encoding="utf-8"
        )
        monkeypatch.setenv("DAPR_COMPONENTS_PATH", str(components))
        metrics_data = _record_counter(provider, in_memory_reader, "dapr.path.test")

        with (
            patch(f"{_EXPORTER_MOD}.ENABLE_OBSERVABILITY_STORE_SINK", True),
            patch(f"{_OBS_MOD}.ENABLE_ATLAN_UPLOAD", False),
        ):
            exporter.export(metrics_data)

        uploaded = list(root.rglob("*.json.gz"))
        assert len(uploaded) == 1, list(root.rglob("*"))
        rel = uploaded[0].relative_to(root).as_posix()
        assert rel.startswith(_PREFIX + "/year=")
        with gzip.open(uploaded[0], "rb") as fh:
            names = [orjson.loads(line)["name"] for line in fh if line.strip()]
        assert names == ["dapr.path.test"]


_LOCAL_COMPONENT = """\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: objectstore
spec:
  version: v1
  type: bindings.localstorage
  metadata:
    - name: rootPath
      value: "{root}"
"""


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


class TestSdrUpstreamExport:
    """SDR mode (``ENABLE_ATLAN_UPLOAD=true``): both stores, one key."""

    def test_export_uploads_to_both_stores_under_same_key(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        deployment = MagicMock(name="deployment_store")
        upstream = MagicMock(name="upstream_store")
        upload = _UploadSpy()
        metrics_data = _record_counter(provider, in_memory_reader, "sdr.export")

        with _patch_stores(
            upload, deployment=deployment, upstream=upstream, atlan_upload=True
        ):
            result = exporter.export(metrics_data)

        assert result == MetricExportResult.SUCCESS
        assert [c[2] for c in upload.calls] == [deployment, upstream]
        assert upload.calls[0][0] == upload.calls[1][0]
        assert "prometheus-metrics" in upload.calls[0][0]
        assert not os.path.exists(upload.calls[0][1])

    def test_export_swallows_upstream_upload_failure(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        deployment = MagicMock(name="deployment_store")
        upstream = MagicMock(name="upstream_store")
        upload = _UploadSpy(fail_for=upstream)
        metrics_data = _record_counter(provider, in_memory_reader, "sdr.fail")

        with _patch_stores(
            upload, deployment=deployment, upstream=upstream, atlan_upload=True
        ):
            result = exporter.export(metrics_data)

        assert result == MetricExportResult.SUCCESS
        assert [c[2] for c in upload.calls] == [deployment, upstream]


class TestStoreResolution:
    """Store resolution tolerance, now in the shared writer (BLDX-1567)."""

    def test_stores_not_resolved_at_init(self, tmp_path) -> None:
        """Construction must not touch bindings: the exporter can be built
        during app-server boot, before the ``objectstore`` component exists."""
        with (
            patch.object(AtlanObservability, "_get_deployment_store") as dep,
            patch.object(AtlanObservability, "_get_upstream_store") as up,
        ):
            ObjectStoreMetricExporter(data_dir=str(tmp_path))
        dep.assert_not_called()
        up.assert_not_called()

    def test_transient_binding_gap_warned_once_then_heals(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        """A missing or still-unresolved binding is retried on every export,
        warned about once without a stacktrace, quiet at DEBUG after that, and
        its heal announced at INFO."""
        from application_sdk.storage.errors import (
            StorageBindingBrokenError,
            StorageBindingNotFoundError,
        )

        store = MagicMock(name="deployment_store")
        outcomes: list[object] = [
            StorageBindingNotFoundError("not written yet", binding_name="x"),
            StorageBindingBrokenError("unresolved", binding_name="x"),
            store,
        ]

        def _get():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        upload = _UploadSpy()
        with (
            _patch_stores(upload),
            patch.object(AtlanObservability, "_get_deployment_store", side_effect=_get),
            patch(f"{_OBS_MOD}.logging") as log,
        ):
            for i in range(3):
                exporter.export(
                    _record_counter(provider, in_memory_reader, f"heal.{i}")
                )

        assert [c[2] for c in upload.calls] == [store]
        assert log.warning.call_count == 1
        assert "exc_info" not in log.warning.call_args.kwargs
        info_msgs = [c.args[0] for c in log.info.call_args_list]
        assert any("now resolved" in m for m in info_msgs)
        debug_msgs = [c.args[0] for c in log.debug.call_args_list]
        assert any("still not resolvable" in m for m in debug_msgs)

    def test_non_transient_error_logged_once_and_not_retried(
        self,
        provider: MeterProvider,
        in_memory_reader: InMemoryMetricReader,
        exporter: ObjectStoreMetricExporter,
    ) -> None:
        """A misconfiguration (e.g. an unsupported binding type) will not heal:
        it is logged once and the store is not tried again on later exports."""
        from application_sdk.storage.errors import StorageConfigError

        upload = _UploadSpy()
        with (
            _patch_stores(upload),
            patch.object(
                AtlanObservability,
                "_get_deployment_store",
                side_effect=StorageConfigError("Unsupported binding type: 'nope'"),
            ) as get,
            patch(f"{_OBS_MOD}.logging") as log,
        ):
            for i in range(3):
                exporter.export(_record_counter(provider, in_memory_reader, f"bad.{i}"))

        assert get.call_count == 1
        assert upload.calls == []
        assert log.warning.call_count == 1
