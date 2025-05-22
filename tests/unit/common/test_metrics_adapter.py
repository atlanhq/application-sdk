from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from application_sdk.observability.metrics_adaptor import (
    AtlanMetricsAdapter,
    MetricRecord,
    MetricType,
    get_metrics,
)


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
            "OBSERVABILITY_DIR": "/tmp/observability",
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
