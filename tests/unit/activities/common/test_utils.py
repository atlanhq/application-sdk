"""Simplified unit tests for activities common utilities."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_workflow_id,
    send_periodic_heartbeat,
)


class TestGetWorkflowId:
    """Test cases for get_workflow_id function."""

    @patch("application_sdk.activities.common.utils.activity")
    def test_get_workflow_id_success(self, mock_activity):
        """Test successful workflow ID retrieval."""
        mock_activity.info.return_value.workflow_id = "test-workflow-123"

        result = get_workflow_id()

        assert result == "test-workflow-123"
        mock_activity.info.assert_called_once()

    @patch("application_sdk.activities.common.utils.activity")
    def test_get_workflow_id_activity_error(self, mock_activity):
        """Test workflow ID retrieval when activity.info() fails."""
        mock_activity.info.side_effect = Exception("Activity context error")

        with pytest.raises(Exception, match="Failed to get workflow id"):
            get_workflow_id()


class TestAutoHeartbeater:
    """Test cases for auto_heartbeater decorator."""

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.send_periodic_heartbeat")
    async def test_auto_heartbeater_success(self, mock_send_heartbeat, mock_activity):
        """Test successful auto_heartbeater decorator."""
        # Mock activity info
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        # Create a mock async function
        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()

        assert result == "success"
        mock_send_heartbeat.assert_called_once()

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.send_periodic_heartbeat")
    async def test_auto_heartbeater_default_timeout(
        self, mock_send_heartbeat, mock_activity
    ):
        """Test auto_heartbeater with default timeout."""
        # Mock activity info with no heartbeat timeout
        mock_activity.info.return_value.heartbeat_timeout = None

        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()

        assert result == "success"
        mock_send_heartbeat.assert_called_once()

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.send_periodic_heartbeat")
    async def test_auto_heartbeater_runtime_error(
        self, mock_send_heartbeat, mock_activity
    ):
        """Test auto_heartbeater when activity.info() raises RuntimeError."""
        mock_activity.info.side_effect = RuntimeError("No activity context")

        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()

        assert result == "success"
        mock_send_heartbeat.assert_called_once()

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.send_periodic_heartbeat")
    async def test_auto_heartbeater_function_error(
        self, mock_send_heartbeat, mock_activity
    ):
        """Test auto_heartbeater when the decorated function raises an error."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity():
            raise ValueError("Function error")

        with pytest.raises(ValueError, match="Function error"):
            await test_activity()

        # Heartbeat task should still be created and cancelled
        mock_send_heartbeat.assert_called_once()

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_function_warning(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity():
            return "sync_success"

        result = sync_activity()
        # The decorator returns a coroutine even for sync functions
        assert asyncio.iscoroutine(result)

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.send_periodic_heartbeat")
    async def test_auto_heartbeater_with_arguments(
        self, mock_send_heartbeat, mock_activity
    ):
        """Test auto_heartbeater with function arguments."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity(arg1, arg2, kwarg1=None):
            return f"{arg1}_{arg2}_{kwarg1}"

        result = await test_activity("a", "b", kwarg1="c")

        assert result == "a_b_c"
        mock_send_heartbeat.assert_called_once()


class TestSendPeriodicHeartbeat:
    """Test cases for send_periodic_heartbeat function."""

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.asyncio.sleep")
    async def test_send_periodic_heartbeat_success(self, mock_sleep, mock_activity):
        mock_sleep.return_value = None
        task = asyncio.create_task(send_periodic_heartbeat(0.1, "test_detail"))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Just ensure no exception is raised (heartbeat may not be called before cancel)

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.asyncio.sleep")
    async def test_send_periodic_heartbeat_multiple_details(
        self, mock_sleep, mock_activity
    ):
        mock_sleep.return_value = None
        task = asyncio.create_task(send_periodic_heartbeat(0.1, "detail1", "detail2"))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Just ensure no exception is raised

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.asyncio.sleep")
    async def test_send_periodic_heartbeat_no_details(self, mock_sleep, mock_activity):
        mock_sleep.return_value = None
        task = asyncio.create_task(send_periodic_heartbeat(0.1))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Just ensure no exception is raised

    @patch("application_sdk.activities.common.utils.activity")
    @patch("application_sdk.activities.common.utils.asyncio.sleep")
    async def test_send_periodic_heartbeat_sleep_error(self, mock_sleep, mock_activity):
        """Test periodic heartbeat when sleep raises an error."""
        mock_sleep.side_effect = asyncio.CancelledError()

        task = asyncio.create_task(send_periodic_heartbeat(0.1))

        with pytest.raises(asyncio.CancelledError):
            await task

        # Heartbeat should not be called if sleep fails
        mock_activity.heartbeat.assert_not_called()
