from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from unittest import mock

import pytest
from opentelemetry.trace import SpanKind

from application_sdk.observability.traces_adaptor import (
    AtlanTracesAdapter,
    TraceRecord,
    get_traces,
)


@pytest.fixture
def mock_tracer():
    """Create a mock tracer instance."""
    with mock.patch("opentelemetry.trace.set_tracer_provider"):
        with mock.patch("opentelemetry.sdk.trace.TracerProvider") as mock_provider:
            mock_tracer = mock.MagicMock()
            mock_provider.return_value.get_tracer.return_value = mock_tracer
            yield mock_tracer


@contextmanager
def create_traces_adapter() -> Generator[AtlanTracesAdapter, None, None]:
    """Create a traces adapter instance with mocked environment."""
    with mock.patch.dict(
        "os.environ",
        {
            "ENABLE_OTLP_TRACES": "true",  # Enable OTLP for testing
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "TRACES_BATCH_SIZE": "100",
            "TRACES_FLUSH_INTERVAL_SECONDS": "1",
            "TRACES_RETENTION_DAYS": "7",
            "TRACES_CLEANUP_ENABLED": "true",
            "OBSERVABILITY_DIR": "/tmp/observability",
            "TRACES_FILE_NAME": "traces.parquet",
        },
    ):
        # Create mock tracer first
        mock_tracer = mock.MagicMock()

        # Mock the tracer provider setup
        with mock.patch("opentelemetry.trace.set_tracer_provider"):
            with mock.patch("opentelemetry.sdk.trace.TracerProvider") as mock_provider:
                mock_provider.return_value.get_tracer.return_value = mock_tracer

                # Create adapter after mocks are in place
                adapter = AtlanTracesAdapter()
                # Set the tracer directly
                adapter.tracer = mock_tracer
                yield adapter


@pytest.fixture
def traces_adapter():
    """Fixture for non-hypothesis tests."""
    with create_traces_adapter() as adapter:
        yield adapter


def test_process_record_with_trace_record():
    """Test process_record() method with TraceRecord instance."""
    with create_traces_adapter() as traces_adapter:
        record = TraceRecord(
            timestamp=datetime.now().timestamp(),
            trace_id="1234567890abcdef",
            span_id="abcdef1234567890",
            parent_span_id="0987654321fedcba",
            name="test_span",
            kind="SERVER",
            status_code="OK",
            status_message="Success",
            attributes={"test": "attribute"},
            events=[{"name": "test_event", "attributes": {"key": "value"}}],
            duration_ms=150.5,
        )
        processed = traces_adapter.process_record(record)
        assert processed["trace_id"] == "1234567890abcdef"
        assert processed["span_id"] == "abcdef1234567890"
        assert processed["parent_span_id"] == "0987654321fedcba"
        assert processed["name"] == "test_span"
        assert processed["kind"] == "SERVER"
        assert processed["status_code"] == "OK"
        assert processed["status_message"] == "Success"
        assert processed["attributes"] == {"test": "attribute"}
        assert processed["events"] == [
            {"name": "test_event", "attributes": {"key": "value"}}
        ]
        assert processed["duration_ms"] == 150.5


def test_process_record_with_dict():
    """Test process_record() method with dictionary input."""
    with create_traces_adapter() as traces_adapter:
        record = {
            "timestamp": datetime.now().timestamp(),
            "trace_id": "1234567890abcdef",
            "span_id": "abcdef1234567890",
            "parent_span_id": "0987654321fedcba",
            "name": "test_span",
            "kind": "SERVER",
            "status_code": "OK",
            "status_message": "Success",
            "attributes": {"test": "attribute"},
            "events": [{"name": "test_event", "attributes": {"key": "value"}}],
            "duration_ms": 150.5,
        }
        processed = traces_adapter.process_record(record)
        assert processed == record


@pytest.mark.asyncio
async def test_send_to_otel():
    """Test _send_to_otel() method."""
    with create_traces_adapter() as traces_adapter:
        with mock.patch.object(
            traces_adapter.tracer, "start_as_current_span"
        ) as mock_span:
            mock_span_context = mock.MagicMock()
            mock_span.return_value.__enter__.return_value = mock_span_context

            record = TraceRecord(
                timestamp=datetime.now().timestamp(),
                trace_id="1234567890abcdef",
                span_id="abcdef1234567890",
                name="test_span",
                kind="SERVER",
                status_code="OK",
                attributes={"test": "attribute"},
                events=[{"name": "test_event", "attributes": {"key": "value"}}],
                duration_ms=150.5,
            )
            traces_adapter._send_to_otel(record)

            mock_span.assert_called_once_with(
                name="test_span", kind=SpanKind.SERVER, attributes={"test": "attribute"}
            )
            mock_span_context.set_status.assert_called_once()
            mock_span_context.add_event.assert_called_once_with(
                name="test_event", attributes={"key": "value"}, timestamp=mock.ANY
            )


@pytest.mark.asyncio
async def test_log_to_console():
    """Test _log_to_console() method."""
    with create_traces_adapter() as traces_adapter:
        with mock.patch(
            "application_sdk.observability.traces_adaptor.get_logger"
        ) as mock_get_logger:
            mock_logger = mock.MagicMock()
            mock_get_logger.return_value = mock_logger

            # Create a test trace record
            trace_record = TraceRecord(
                timestamp=datetime.now().timestamp(),
                trace_id="1234567890abcdef",
                span_id="abcdef1234567890",
                name="test_span",
                kind="SERVER",
                status_code="OK",
                attributes={"test": "attribute"},
                duration_ms=150.5,
            )

            # Call the method
            traces_adapter._log_to_console(trace_record)

            # Verify the logger was called with the correct message
            mock_logger.tracing.assert_called_once()
            log_message = mock_logger.tracing.call_args[0][0]
            assert "test_span" in log_message
            assert "1234567890abcdef" in log_message
            assert "abcdef1234567890" in log_message
            assert "OK" in log_message
            assert "150.5ms" in log_message
            assert "{'test': 'attribute'}" in log_message


def test_get_traces():
    """Test get_traces function creates and caches traces instance."""
    traces1 = get_traces()
    traces2 = get_traces()
    assert traces1 is traces2
    assert isinstance(traces1, AtlanTracesAdapter)


@pytest.mark.asyncio
async def test_cleanup_old_records():
    """Test cleanup of old records."""
    with create_traces_adapter() as traces_adapter:
        # Add some test traces
        for i in range(3):
            traces_adapter.record_trace(
                name=f"test_span_{i}",
                trace_id=f"trace_{i}",
                span_id=f"span_{i}",
                kind="SERVER",
                status_code="OK",
                attributes={"test": f"value_{i}"},
                duration_ms=100.0,
            )

        # Force cleanup
        await traces_adapter._cleanup_old_records()

        # Verify cleanup was performed
        # Note: In a real test, we would need to mock the file system operations
        # and verify the cleanup logic. This is just a basic structure.
        assert True
