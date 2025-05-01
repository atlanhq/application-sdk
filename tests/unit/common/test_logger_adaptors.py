import logging
from unittest import mock

import pytest

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter, get_logger


@pytest.fixture
def mock_logger():
    """Create a mock logger instance."""
    logger = mock.MagicMock(spec=logging.Logger)
    # Set handlers as a list attribute, not a mock
    logger.handlers = []
    logger.name = "test_logger"

    # Configure the mock to properly handle handler operations
    def remove_handler(handler):
        if handler in logger.handlers:
            logger.handlers.remove(handler)

    def add_handler(handler):
        logger.handlers.append(handler)

    logger.removeHandler.side_effect = remove_handler
    logger.addHandler.side_effect = add_handler

    return logger


@pytest.fixture
def logger_adapter(mock_logger):
    """Create a logger adapter instance with mocked underlying logger."""
    # Mock environment variables
    with mock.patch.dict(
        "os.environ",
        {
            "LOG_LEVEL": "INFO",
            "ENABLE_OTLP_LOGS": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        },
    ):
        # Mock the StreamHandler and Formatter
        with mock.patch("logging.StreamHandler") as mock_handler, mock.patch(
            "logging.Formatter"
        ) as mock_formatter:
            mock_handler.return_value = mock.MagicMock()
            mock_formatter.return_value = mock.MagicMock()
            mock_handler.return_value.setFormatter = mock.MagicMock()
            return AtlanLoggerAdapter(mock_logger)


def test_process_without_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method without any context."""
    msg, kwargs = logger_adapter.process("Test message", {})
    assert "extra" in kwargs
    assert msg == "Test message"


def test_is_enabled_for(logger_adapter: AtlanLoggerAdapter):
    """Test isEnabledFor method."""
    logger_adapter.logger.isEnabledFor.return_value = True
    assert logger_adapter.isEnabledFor(logging.INFO)
    logger_adapter.logger.isEnabledFor.assert_called_once_with(logging.INFO)


def test_base_logger_property(logger_adapter: AtlanLoggerAdapter):
    """Test base_logger property."""
    assert logger_adapter.base_logger == logger_adapter.logger


def test_process_with_workflow_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when workflow information is present."""
    # Mock workflow.info() to return a fake workflow context
    with mock.patch("temporalio.workflow.info") as mock_workflow_info:
        mock_workflow_info.return_value = mock.Mock(
            workflow_id="test_workflow_id",
            run_id="test_run_id",
            workflow_type="test_workflow_type",
            namespace="test_namespace",
            task_queue="test_queue",
            attempt=1,
        )

        msg, kwargs = logger_adapter.process("Test message", {})

        assert "extra" in kwargs
        assert kwargs["extra"]["workflow_id"] == "test_workflow_id"
        assert kwargs["extra"]["run_id"] == "test_run_id"
        assert kwargs["extra"]["workflow_type"] == "test_workflow_type"
        assert kwargs["extra"]["namespace"] == "test_namespace"
        assert kwargs["extra"]["task_queue"] == "test_queue"
        assert kwargs["extra"]["attempt"] == 1
        expected_msg = "Test message \n Workflow Info: \n ['workflow_id'=test_workflow_id] ['run_id'=test_run_id] ['workflow_type'=test_workflow_type] \n"
        assert msg == expected_msg


def test_process_with_activity_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when activity information is present."""
    # Mock activity.info() to return a fake activity context
    with mock.patch("temporalio.activity.info") as mock_activity_info:
        mock_activity_info.return_value = mock.Mock(
            workflow_id="test_workflow_id",
            workflow_run_id="test_run_id",
            activity_id="test_activity_id",
            activity_type="test_activity_type",
            task_queue="test_queue",
            attempt=1,
            schedule_to_close_timeout="30s",
            start_to_close_timeout="25s",
        )

        msg, kwargs = logger_adapter.process("Test message", {})

        assert "extra" in kwargs
        assert kwargs["extra"]["workflow_id"] == "test_workflow_id"
        assert kwargs["extra"]["run_id"] == "test_run_id"
        assert kwargs["extra"]["activity_id"] == "test_activity_id"
        assert kwargs["extra"]["activity_type"] == "test_activity_type"
        assert kwargs["extra"]["task_queue"] == "test_queue"
        assert kwargs["extra"]["attempt"] == 1
        assert kwargs["extra"]["schedule_to_close_timeout"] == "30s"
        assert kwargs["extra"]["start_to_close_timeout"] == "25s"
        expected_msg = "Test message \n Activity Info: \n ['workflow_id'=test_workflow_id] ['run_id'=test_run_id] ['activity_type'=test_activity_type] \n"
        assert msg == expected_msg


def test_process_with_request_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method with request context."""
    with mock.patch(
        "application_sdk.common.logger_adaptors.request_context"
    ) as mock_context:
        mock_context.get.return_value = {"request_id": "test_request_id"}
        msg, kwargs = logger_adapter.process("Test message", {})
        assert "extra" in kwargs
        assert kwargs["extra"]["request_id"] == "test_request_id"


def test_get_logger():
    """Test get_logger function creates and caches logger instances."""
    logger1 = get_logger("test_logger")
    logger2 = get_logger("test_logger")
    assert logger1 is logger2
    assert isinstance(logger1, AtlanLoggerAdapter)
