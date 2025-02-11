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
            run_id="test_run_id", workflow_id="test_workflow_id"
        )

        msg, kwargs = logger_adapter.process("Test message", {})

        assert kwargs["run_id"] == "test_run_id"
        assert kwargs["workflow_id"] == "test_workflow_id"
        assert msg == "Test message"


def test_process_with_activity_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when activity information is present."""
    # Mock activity.info() to return a fake activity context
    with mock.patch("temporalio.activity.info") as mock_activity_info:
        mock_activity_info.return_value = mock.Mock(
            workflow_run_id="test_run_id",
            workflow_id="test_workflow_id",
            activity_id="test_activity_id",
        )

        msg, kwargs = logger_adapter.process("Test message", {})

        assert kwargs["run_id"] == "test_run_id"
        assert kwargs["workflow_id"] == "test_workflow_id"
        assert kwargs["activity_id"] == "test_activity_id"
        assert msg == "Test message"


def test_process_without_context(logger_adapter: AtlanLoggerAdapter):
    """Test process() method when neither workflow nor activity information is available."""
    # Mock workflow.info() and activity.info() to return None (no context)
    with mock.patch("temporalio.workflow.info", return_value=None):
        with mock.patch("temporalio.activity.info", return_value=None):
            msg, kwargs = logger_adapter.process("Test message", {})

            # Ensure process id and thread id are added
            assert "process_id" in kwargs
            assert "thread_id" in kwargs

            del kwargs["process_id"]
            del kwargs["thread_id"]

            # Ensure no extra information is added
            assert kwargs == {}
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
