from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.observability.metrics_adaptor import (
    AtlanMetricsAdapter,
    get_metrics,
)
from application_sdk.observability.models import MetricRecord, MetricType


@pytest.fixture
def mock_metrics():
    """Create a mock metrics instance."""
    with mock.patch("opentelemetry.metrics.set_meter_provider"):
        with mock.patch("opentelemetry.sdk.metrics.MeterProvider") as mock_provider:
            mock_meter = mock.MagicMock()
            mock_provider.return_value.get_meter.return_value = mock_meter
            yield mock_meter


@contextmanager
def create_metrics_adapter() -> Generator[AtlanMetricsAdapter, None, None]:
    """Create a metrics adapter instance with mocked environment."""
    with mock.patch.dict(
        "os.environ",
        {
            "ENABLE_OTLP_METRICS": "true",  # Enable OTLP for testing
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "METRICS_BATCH_SIZE": "100",
            "METRICS_FLUSH_INTERVAL_SECONDS": "1",
            "METRICS_RETENTION_DAYS": "7",
            "METRICS_CLEANUP_ENABLED": "true",
            "METRICS_FILE_NAME": "metrics.parquet",
        },
    ):
        # Create mock meter first
        mock_meter = mock.MagicMock()

        # Mock the meter provider setup
        with mock.patch("opentelemetry.metrics.set_meter_provider"):
            with mock.patch("opentelemetry.sdk.metrics.MeterProvider") as mock_provider:
                mock_provider.return_value.get_meter.return_value = mock_meter

                # Create adapter after mocks are in place
                adapter = AtlanMetricsAdapter()
                # Set the meter directly
                adapter.meter = mock_meter
                yield adapter


@pytest.fixture
def metrics_adapter():
    """Fixture for non-hypothesis tests."""
    with create_metrics_adapter() as adapter:
        yield adapter


def test_process_record_with_metric_record():
    """Test process_record() method with MetricRecord instance."""
    with create_metrics_adapter() as metrics_adapter:
        record = MetricRecord(
            timestamp=datetime.now().timestamp(),
            name="test_metric",
            value=42.0,
            type=MetricType.COUNTER,
            labels={"test": "label"},
            description="Test metric",
            unit="count",
        )
        processed = metrics_adapter.process_record(record)
        assert processed["name"] == "test_metric"
        assert processed["value"] == 42.0
        assert processed["type"] == MetricType.COUNTER.value
        assert processed["labels"] == {"test": "label"}
        assert processed["description"] == "Test metric"
        assert processed["unit"] == "count"


def test_process_record_with_dict():
    """Test process_record() method with dictionary input."""
    with create_metrics_adapter() as metrics_adapter:
        record = {
            "timestamp": datetime.now().timestamp(),
            "name": "test_metric",
            "value": 42.0,
            "type": MetricType.COUNTER.value,
            "labels": {"test": "label"},
            "description": "Test metric",
            "unit": "count",
        }
        processed = metrics_adapter.process_record(record)
        assert processed == record


@given(
    st.text(min_size=1),
    st.floats(),
    st.sampled_from(MetricType),
)
def test_record_metric_with_various_inputs(
    name: str, value: float, metric_type: MetricType
):
    """Test record_metric() method with various inputs."""
    with create_metrics_adapter() as metrics_adapter:
        labels = {"test": "label"}
        description = "Test metric"
        unit = "count"

        metrics_adapter.record_metric(
            name=name,
            value=value,
            metric_type=metric_type,
            labels=labels,
            description=description,
            unit=unit,
        )

        # Verify the metric was added to the buffer
        assert len(metrics_adapter._buffer) == 1
        buffered_metric = metrics_adapter._buffer[0]
        assert buffered_metric["name"] == name
        # Handle NaN values in comparison
        if value != value:  # Check if value is NaN
            assert (
                buffered_metric["value"] != buffered_metric["value"]
            )  # Both should be NaN
        else:
            assert buffered_metric["value"] == value
        assert buffered_metric["type"] == metric_type.value
        assert buffered_metric["labels"] == labels
        assert buffered_metric["description"] == description
        assert buffered_metric["unit"] == unit


def test_export_record_with_otlp_enabled():
    """Test export_record() method when OTLP is enabled."""
    with mock.patch(
        "application_sdk.observability.metrics_adaptor.ENABLE_OTLP_METRICS", True
    ):
        with create_metrics_adapter() as metrics_adapter:
            record = MetricRecord(
                timestamp=datetime.now().timestamp(),
                name="test_metric",
                value=42.0,
                type=MetricType.COUNTER,
                labels={"test": "label"},
                description="Test metric",
                unit="count",
            )
            with mock.patch.object(metrics_adapter, "_send_to_otel") as mock_send:
                with mock.patch.object(metrics_adapter, "_log_to_console") as mock_log:
                    metrics_adapter.export_record(record)
                    mock_send.assert_called_once_with(record)
                    mock_log.assert_called_once_with(record)


def test_export_record_with_otlp_disabled():
    """Test export_record() method when OTLP is disabled."""
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch.object(metrics_adapter, "_send_to_otel") as mock_send:
            with mock.patch.object(metrics_adapter, "_log_to_console") as mock_log:
                record = MetricRecord(
                    timestamp=datetime.now().timestamp(),
                    name="test_metric",
                    value=42.0,
                    type=MetricType.COUNTER,
                    labels={"test": "label"},
                    description="Test metric",
                    unit="count",
                )
                metrics_adapter.export_record(record)
                mock_send.assert_not_called()
                mock_log.assert_called_once_with(record)


def test_send_to_otel_counter():
    """Test _send_to_otel() method with counter metric."""
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch.object(metrics_adapter.meter, "create_counter") as mock_create:
            mock_counter = mock.MagicMock()
            mock_create.return_value = mock_counter

            record = MetricRecord(
                timestamp=datetime.now().timestamp(),
                name="test_counter",
                value=42.0,
                type=MetricType.COUNTER,
                labels={"test": "label"},
                description="Test counter",
                unit="count",
            )
            metrics_adapter._send_to_otel(record)

            mock_create.assert_called_once_with(
                name="test_counter",
                description="Test counter",
                unit="count",
            )
            mock_counter.add.assert_called_once_with(42.0, {"test": "label"})


def test_send_to_otel_gauge():
    """Test _send_to_otel() method with gauge metric."""
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch.object(
            metrics_adapter.meter, "create_observable_gauge"
        ) as mock_create:
            mock_gauge = mock.MagicMock()
            mock_create.return_value = mock_gauge

            record = MetricRecord(
                timestamp=datetime.now().timestamp(),
                name="test_gauge",
                value=42.0,
                type=MetricType.GAUGE,
                labels={"test": "label"},
                description="Test gauge",
                unit="count",
            )
            metrics_adapter._send_to_otel(record)

            mock_create.assert_called_once_with(
                name="test_gauge",
                description="Test gauge",
                unit="count",
            )
            mock_gauge.add.assert_called_once_with(42.0, {"test": "label"})


def test_send_to_otel_histogram():
    """Test _send_to_otel() method with histogram metric."""
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch.object(
            metrics_adapter.meter, "create_histogram"
        ) as mock_create:
            mock_histogram = mock.MagicMock()
            mock_create.return_value = mock_histogram

            record = MetricRecord(
                timestamp=datetime.now().timestamp(),
                name="test_histogram",
                value=42.0,
                type=MetricType.HISTOGRAM,
                labels={"test": "label"},
                description="Test histogram",
                unit="count",
            )
            metrics_adapter._send_to_otel(record)

            mock_create.assert_called_once_with(
                name="test_histogram",
                description="Test histogram",
                unit="count",
            )
            mock_histogram.record.assert_called_once_with(42.0, {"test": "label"})


def test_log_to_console():
    """Test _log_to_console() method."""
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.get_logger"
        ) as mock_get_logger:
            mock_logger = mock.MagicMock()
            mock_get_logger.return_value = mock_logger

            # Create a test metric record
            metric_record = MetricRecord(
                timestamp=datetime.now().timestamp(),
                name="test_metric",
                value=42.0,
                type=MetricType.GAUGE,
                labels={"test": "label"},
                description="Test metric",
                unit="count",
            )

            # Call the method
            metrics_adapter._log_to_console(metric_record)

            # Verify the logger was called with the correct message
            mock_logger.metric.assert_called_once()
            log_message = mock_logger.metric.call_args[0][0]
            assert "test_metric = 42.0 (gauge)" in log_message
            assert "Labels: {'test': 'label'}" in log_message
            assert "Description: Test metric" in log_message
            assert "Unit: count" in log_message


def test_get_metrics():
    """Test get_metrics function creates and caches metrics instance."""
    metrics1 = get_metrics()
    metrics2 = get_metrics()
    assert metrics1 is metrics2
    assert isinstance(metrics1, AtlanMetricsAdapter)


def test_export_record_with_segment_write_key():
    """Test export_record() sends to Segment when write key is present.

    Segment is automatically enabled when ATLAN_SEGMENT_WRITE_KEY is set.
    """
    with mock.patch.dict(
        "os.environ",
        {
            "ATLAN_SEGMENT_WRITE_KEY": "test_key",
            "ATLAN_SEGMENT_API_URL": "https://api.segment.io/v1/batch",
            "METRICS_BATCH_SIZE": "100",
            "METRICS_FLUSH_INTERVAL_SECONDS": "1",
            "METRICS_RETENTION_DAYS": "7",
            "METRICS_CLEANUP_ENABLED": "true",
            "METRICS_FILE_NAME": "metrics.parquet",
        },
    ):
        with mock.patch("opentelemetry.metrics.set_meter_provider"):
            with mock.patch("opentelemetry.sdk.metrics.MeterProvider"):
                with mock.patch(
                    "application_sdk.observability.metrics_adaptor.SegmentClient"
                ) as mock_segment_client_class:
                    mock_segment_client = mock.MagicMock()
                    mock_segment_client_class.return_value = mock_segment_client

                    adapter = AtlanMetricsAdapter()

                    record = MetricRecord(
                        timestamp=datetime.now().timestamp(),
                        name="test_metric",
                        value=42.0,
                        type=MetricType.COUNTER,
                        labels={"test": "label"},
                        description="Test metric",
                        unit="count",
                    )
                    with mock.patch.object(
                        adapter.segment_client, "send_metric"
                    ) as mock_send:
                        with mock.patch.object(adapter, "_log_to_console") as mock_log:
                            adapter.export_record(record)
                            mock_send.assert_called_once_with(record)
                            mock_log.assert_called_once_with(record)


def test_export_record_without_segment_write_key():
    """Test export_record() when Segment write key is not set.

    When no write key is provided, the Segment client is disabled but
    send_metric is still called (the client handles the no-op internally).
    """
    with create_metrics_adapter() as metrics_adapter:
        with mock.patch.object(
            metrics_adapter.segment_client, "send_metric"
        ) as mock_send:
            with mock.patch.object(metrics_adapter, "_log_to_console") as mock_log:
                record = MetricRecord(
                    timestamp=datetime.now().timestamp(),
                    name="test_metric",
                    value=42.0,
                    type=MetricType.COUNTER,
                    labels={"test": "label"},
                    description="Test metric",
                    unit="count",
                )
                metrics_adapter.export_record(record)
                mock_send.assert_called_once_with(record)
                mock_log.assert_called_once_with(record)


def test_segment_client_disabled_without_write_key():
    """Test SegmentClient is disabled when write key is not provided.

    The presence of the write key determines whether the client is enabled.
    No separate boolean flag is needed.
    """
    with mock.patch.dict(
        "os.environ",
        {
            "ATLAN_SEGMENT_WRITE_KEY": "",
            "METRICS_BATCH_SIZE": "100",
            "METRICS_FLUSH_INTERVAL_SECONDS": "1",
            "METRICS_RETENTION_DAYS": "7",
            "METRICS_CLEANUP_ENABLED": "true",
            "METRICS_FILE_NAME": "metrics.parquet",
        },
    ):
        with mock.patch("opentelemetry.metrics.set_meter_provider"):
            with mock.patch("opentelemetry.sdk.metrics.MeterProvider"):
                with mock.patch(
                    "application_sdk.constants.SEGMENT_WRITE_KEY",
                    "",
                ):
                    with mock.patch(
                        "application_sdk.observability.segment_client.SEGMENT_WRITE_KEY",
                        "",
                    ):
                        adapter = AtlanMetricsAdapter()
                        assert adapter.segment_client is not None
                        assert adapter.segment_client.enabled is False


class TestPython314EventLoopCompat:
    """Tests for Python 3.14 compatibility where asyncio.get_event_loop()
    raises RuntimeError when no current event loop exists."""

    def test_flush_task_starts_via_thread_when_no_event_loop(self):
        """When no running event loop exists (Python 3.14 behavior),
        the adapter should fall back to starting the flush in a daemon thread."""
        AtlanMetricsAdapter._flush_task_started = False
        with mock.patch.dict(
            "os.environ",
            {
                "ENABLE_OTLP_METRICS": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            },
        ):
            with mock.patch("opentelemetry.metrics.set_meter_provider"):
                with mock.patch("opentelemetry.sdk.metrics.MeterProvider"):
                    with mock.patch(
                        "application_sdk.observability.metrics_adaptor.asyncio.get_running_loop",
                        side_effect=RuntimeError("no running event loop"),
                    ):
                        with mock.patch(
                            "application_sdk.observability.metrics_adaptor.threading.Thread"
                        ) as mock_thread:
                            mock_thread_instance = mock.MagicMock()
                            mock_thread.return_value = mock_thread_instance

                            _ = AtlanMetricsAdapter()

                            mock_thread.assert_called_once()
                            _, kwargs = mock_thread.call_args
                            assert kwargs.get("daemon") is True
                            mock_thread_instance.start.assert_called_once()

    def test_flush_task_uses_running_loop_when_available(self):
        """When a running event loop exists, the adapter should create
        a task on it instead of spawning a thread."""
        AtlanMetricsAdapter._flush_task_started = False
        mock_loop = mock.MagicMock()
        with mock.patch.dict(
            "os.environ",
            {
                "ENABLE_OTLP_METRICS": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            },
        ):
            with mock.patch("opentelemetry.metrics.set_meter_provider"):
                with mock.patch("opentelemetry.sdk.metrics.MeterProvider"):
                    with mock.patch(
                        "application_sdk.observability.metrics_adaptor.asyncio.get_running_loop",
                        return_value=mock_loop,
                    ):
                        with mock.patch(
                            "application_sdk.observability.metrics_adaptor.threading.Thread"
                        ) as mock_thread:
                            _ = AtlanMetricsAdapter()

                            mock_loop.create_task.assert_called_once()
                            mock_thread.assert_not_called()


class TestPrometheusMetrics:
    """Tests for Prometheus metrics integration."""

    def test_prometheus_reader_added_when_enabled(self):
        """When ENABLE_PROMETHEUS_METRICS is true, a PrometheusMetricReader
        should be added to the MeterProvider."""
        AtlanMetricsAdapter._flush_task_started = False
        mock_prom_reader = mock.MagicMock()
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.ENABLE_OTLP_METRICS",
            True,
        ):
            with mock.patch(
                "application_sdk.observability.metrics_adaptor.ENABLE_PROMETHEUS_METRICS",
                True,
            ):
                with mock.patch(
                    "application_sdk.observability.metrics_adaptor.metrics.set_meter_provider"
                ):
                    with mock.patch(
                        "application_sdk.observability.metrics_adaptor.MeterProvider"
                    ) as mock_provider:
                        mock_provider.return_value.get_meter.return_value = (
                            mock.MagicMock()
                        )
                        with mock.patch(
                            "application_sdk.observability.metrics_adaptor.OTLPMetricExporter"
                        ):
                            with mock.patch(
                                "application_sdk.observability.metrics_adaptor.PeriodicExportingMetricReader"
                            ):
                                with mock.patch.dict(
                                    "sys.modules",
                                    {
                                        "opentelemetry.exporter.prometheus": mock.MagicMock(
                                            PrometheusMetricReader=mock_prom_reader
                                        )
                                    },
                                ):
                                    AtlanMetricsAdapter()
                                    call_kwargs = mock_provider.call_args[1]
                                    assert len(call_kwargs["metric_readers"]) == 2

    def test_prometheus_reader_not_added_when_disabled(self):
        """When ENABLE_PROMETHEUS_METRICS is false, only the OTLP reader
        should be in the MeterProvider."""
        AtlanMetricsAdapter._flush_task_started = False
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.ENABLE_OTLP_METRICS",
            True,
        ):
            with mock.patch(
                "application_sdk.observability.metrics_adaptor.ENABLE_PROMETHEUS_METRICS",
                False,
            ):
                with mock.patch(
                    "application_sdk.observability.metrics_adaptor.metrics.set_meter_provider"
                ):
                    with mock.patch(
                        "application_sdk.observability.metrics_adaptor.MeterProvider"
                    ) as mock_provider:
                        mock_provider.return_value.get_meter.return_value = (
                            mock.MagicMock()
                        )
                        with mock.patch(
                            "application_sdk.observability.metrics_adaptor.OTLPMetricExporter"
                        ):
                            with mock.patch(
                                "application_sdk.observability.metrics_adaptor.PeriodicExportingMetricReader"
                            ):
                                AtlanMetricsAdapter()
                                call_kwargs = mock_provider.call_args[1]
                                assert len(call_kwargs["metric_readers"]) == 1
