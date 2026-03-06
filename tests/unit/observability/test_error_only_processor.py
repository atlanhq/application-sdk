"""Unit tests for ErrorOnlySpanProcessor.

Tests the span filtering logic that ensures only ERROR spans are exported
while OK and UNSET spans are dropped.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest
from opentelemetry.trace import Status, StatusCode

from application_sdk.observability.error_only_processor import ErrorOnlySpanProcessor


@pytest.fixture
def mock_exporter():
    """Create a mock SpanExporter."""
    return MagicMock()


@pytest.fixture
def mock_batch_processor():
    """Create a mock BatchSpanProcessor."""
    return MagicMock()


def create_mock_span(status_code: StatusCode) -> Mock:
    """Create a mock span with the given status code.

    Args:
        status_code: The StatusCode for the span's status.

    Returns:
        A mock span with the specified status.
    """
    span = Mock()
    span.status = Status(status_code=status_code)
    return span


def create_mock_span_no_status() -> Mock:
    """Create a mock span with no status (None).

    Returns:
        A mock span with status set to None.
    """
    span = Mock()
    span.status = None
    return span


class TestErrorOnlySpanProcessor:
    """Test suite for ErrorOnlySpanProcessor."""

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_error_span_is_forwarded(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that spans with ERROR status are forwarded to the delegate."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        error_span = create_mock_span(StatusCode.ERROR)

        processor.on_end(error_span)

        mock_delegate.on_end.assert_called_once_with(error_span)

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_ok_span_is_dropped(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that spans with OK status are NOT forwarded to the delegate."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        ok_span = create_mock_span(StatusCode.OK)

        processor.on_end(ok_span)

        mock_delegate.on_end.assert_not_called()

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_unset_span_is_dropped(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that spans with UNSET status are NOT forwarded to the delegate."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        unset_span = create_mock_span(StatusCode.UNSET)

        processor.on_end(unset_span)

        mock_delegate.on_end.assert_not_called()

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_span_without_status_is_dropped(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that spans with status = None are NOT forwarded to the delegate."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        no_status_span = create_mock_span_no_status()

        processor.on_end(no_status_span)

        mock_delegate.on_end.assert_not_called()

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_on_start_always_forwards(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that on_start always forwards to the delegate for correct timing."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        span = Mock()
        parent_context = Mock()

        processor.on_start(span, parent_context)

        mock_delegate.on_start.assert_called_once_with(span, parent_context)

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_on_start_without_parent_context(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that on_start works without parent context."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)
        span = Mock()

        processor.on_start(span)

        mock_delegate.on_start.assert_called_once_with(span, None)

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_shutdown_delegates(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that shutdown forwards to the delegate."""
        mock_delegate = MagicMock()
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)

        processor.shutdown()

        mock_delegate.shutdown.assert_called_once()

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_force_flush_delegates(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that force_flush forwards to the delegate."""
        mock_delegate = MagicMock()
        mock_delegate.force_flush.return_value = True
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)

        result = processor.force_flush(timeout_millis=5000)

        mock_delegate.force_flush.assert_called_once_with(5000)
        assert result is True

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_force_flush_returns_delegate_result(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that force_flush returns the delegate's result."""
        mock_delegate = MagicMock()
        mock_delegate.force_flush.return_value = False
        mock_batch_cls.return_value = mock_delegate

        processor = ErrorOnlySpanProcessor(exporter=mock_exporter)

        result = processor.force_flush()

        assert result is False

    @patch("application_sdk.observability.error_only_processor.BatchSpanProcessor")
    def test_batch_processor_initialized_with_params(
        self, mock_batch_cls: MagicMock, mock_exporter: MagicMock
    ):
        """Test that BatchSpanProcessor is initialized with correct parameters."""
        ErrorOnlySpanProcessor(
            exporter=mock_exporter,
            max_queue_size=1024,
            schedule_delay_millis=3000,
            max_export_batch_size=256,
            export_timeout_millis=15000,
        )

        mock_batch_cls.assert_called_once_with(
            mock_exporter,
            max_queue_size=1024,
            schedule_delay_millis=3000,
            max_export_batch_size=256,
            export_timeout_millis=15000,
        )
