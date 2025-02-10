import logging
from unittest import mock

import pytest

from application_sdk.common.logger_adaptors import (  # Assume this is the file where the class resides
    AtlanLoggerAdapter,
)


@pytest.fixture
def mock_logger():
    """Fixture to provide a mock logger for testing."""
    return mock.Mock(spec=logging.Logger)


@pytest.fixture
def logger_adapter(mock_logger: logging.Logger):
    """Fixture to provide the AtlanLoggerAdapter instance."""
    return AtlanLoggerAdapter(mock_logger)


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


def test_process_without_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when neither workflow nor activity information is available."""
    # Mock workflow.info() and activity.info() to return None (no context)
    with mock.patch("temporalio.workflow.info", return_value=None):
        with mock.patch("temporalio.activity.info", return_value=None):
            msg, kwargs = logger_adapter.process("Test message", {})

            # Ensure process id and thread id are added
            assert "extra" in kwargs
            assert "process_id" in kwargs["extra"]
            assert "thread_id" in kwargs["extra"]

            del kwargs["extra"]["process_id"]
            del kwargs["extra"]["thread_id"]

            # Ensure no extra information is added
            assert kwargs["extra"] == {}
            assert msg == "Test message"


def test_is_enabled_for(logger_adapter: AtlanLoggerAdapter):
    """Test that isEnabledFor returns the correct value."""
    with mock.patch.object(logger_adapter, "logger") as mock_base_logger:
        mock_base_logger.isEnabledFor.return_value = True
        assert logger_adapter.isEnabledFor(logging.INFO) is True

        mock_base_logger.isEnabledFor.return_value = False
        assert logger_adapter.isEnabledFor(logging.INFO) is False


def test_base_logger_property(
    logger_adapter: AtlanLoggerAdapter, mock_logger: logging.Logger
):
    """Test that base_logger property returns the original logger."""
    assert logger_adapter.base_logger is mock_logger
