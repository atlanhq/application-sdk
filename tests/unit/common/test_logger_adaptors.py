from unittest import mock

import pytest
from loguru import logger

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter, get_logger


@pytest.fixture
def mock_logger():
    """Create a mock logger instance."""
    # Create a copy of the real logger for testing
    test_logger = logger.bind()
    test_logger.remove()

    # Add a mock handler for testing
    mock_handler = mock.MagicMock()
    test_logger.add(mock_handler, format="{message}")

    return test_logger


@pytest.fixture
def logger_adapter():
    """Create a logger adapter instance."""

    # Mock environment variables
    with mock.patch.dict(
        "os.environ",
        {
            "LOG_LEVEL": "INFO",
            "ENABLE_OTLP_LOGS": "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        },
    ):
        return AtlanLoggerAdapter("test_logger")


def test_process_without_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method without any context."""
    msg, kwargs = logger_adapter.process("Test message", {})
    assert "logger_name" in kwargs
    assert kwargs["logger_name"] == "test_logger"
    assert msg == "Test message"


def test_process_with_workflow_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when workflow information is present."""
    with mock.patch("temporalio.workflow.info") as mock_workflow_info:
        workflow_info = mock.Mock(
            workflow_id="test_workflow_id",
            run_id="test_run_id",
            workflow_type="test_workflow_type",
            namespace="test_namespace",
            task_queue="test_queue",
            attempt=1,
        )
        mock_workflow_info.return_value = workflow_info

        msg, kwargs = logger_adapter.process("Test message", {})

        assert kwargs["workflow_id"] == "test_workflow_id"
        assert kwargs["run_id"] == "test_run_id"
        assert kwargs["workflow_type"] == "test_workflow_type"
        assert kwargs["namespace"] == "test_namespace"
        assert kwargs["task_queue"] == "test_queue"
        assert kwargs["attempt"] == 1

        # Format the expected message using the workflow_info object
        workflow_context = f"Workflow Context: Workflow ID: <m>{workflow_info.workflow_id}</m> Run ID: <e>{workflow_info.run_id}</e> Type: <g>{workflow_info.workflow_type}</g>"
        expected_msg = f"Test message {workflow_context}"
        assert msg == expected_msg


def test_process_with_activity_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when activity information is present."""
    with mock.patch("temporalio.activity.info") as mock_activity_info:
        activity_info = mock.Mock(
            workflow_id="test_workflow_id",
            workflow_run_id="test_run_id",
            activity_id="test_activity_id",
            activity_type="test_activity_type",
            task_queue="test_queue",
            attempt=1,
            schedule_to_close_timeout="30s",
            start_to_close_timeout="25s",
        )
        mock_activity_info.return_value = activity_info

        msg, kwargs = logger_adapter.process("Test message", {})

        assert kwargs["workflow_id"] == "test_workflow_id"
        assert kwargs["run_id"] == "test_run_id"
        assert kwargs["activity_id"] == "test_activity_id"
        assert kwargs["activity_type"] == "test_activity_type"
        assert kwargs["task_queue"] == "test_queue"
        assert kwargs["attempt"] == 1
        assert kwargs["schedule_to_close_timeout"] == "30s"
        assert kwargs["start_to_close_timeout"] == "25s"

        # Format the expected message using the activity_info object
        activity_context = f"Activity Context: Activity ID: {activity_info.activity_id} Workflow ID: <m>{activity_info.workflow_id}</m> Run ID: <e>{activity_info.workflow_run_id}</e> Type: <g>{activity_info.activity_type}</g>"
        expected_msg = f"Test message {activity_context}"
        assert msg == expected_msg


def test_process_with_request_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method with request context."""
    with mock.patch(
        "application_sdk.common.logger_adaptors.request_context"
    ) as mock_context:
        mock_context.get.return_value = {"request_id": "test_request_id"}
        msg, kwargs = logger_adapter.process("Test message", {})
        assert "request_id" in kwargs
        assert kwargs["request_id"] == "test_request_id"


def test_get_logger():
    """Test get_logger function creates and caches logger instances."""
    logger1 = get_logger("test_logger")
    logger2 = get_logger("test_logger")
    assert logger1 is logger2
    assert isinstance(logger1, AtlanLoggerAdapter)
