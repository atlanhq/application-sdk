"""Simplified unit tests for activities common utilities."""

import asyncio
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
    send_periodic_heartbeat,
    send_periodic_heartbeat_sync,
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


class TestGetObjectStorePrefix:
    """Test cases for get_object_store_prefix function - Real World Scenarios."""

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.relpath")
    @patch("os.path.commonpath")
    def test_temporary_path_to_object_store_conversion(
        self, mock_commonpath, mock_relpath, mock_abspath
    ):
        """Test conversion from SDK temporary paths to object store paths."""
        # Mock absolute path resolution
        mock_abspath.side_effect = lambda p: {
            "./local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456": "/abs/local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)

        # Mock commonpath to return temp path (indicating path is under temp directory)
        mock_commonpath.return_value = "/abs/local/tmp"

        # Mock relative path calculation
        mock_relpath.return_value = "artifacts/apps/myapp/workflows/wf-123/run-456"

        path = "./local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456"
        result = get_object_store_prefix(path)

        assert result == "artifacts/apps/myapp/workflows/wf-123/run-456"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    def test_user_relative_object_store_paths(self, mock_abspath):
        """Test common user-provided relative object store paths."""
        # Mock absolute path resolution - paths are NOT under TEMPORARY_PATH
        mock_abspath.side_effect = lambda p: {
            "datasets/sales/2024/": "/some/other/datasets/sales/2024/",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)

        test_cases = [
            (
                "datasets/sales/2024/",
                "datasets/sales/2024",
            ),  # os.path.abspath normalizes trailing slash
            ("data/input.parquet", "data/input.parquet"),
            (
                "./datasets/sales/",
                "./datasets/sales",
            ),  # os.path.abspath normalizes trailing slash
            ("models/trained/v1.pkl", "models/trained/v1.pkl"),
        ]

        for input_path, expected in test_cases:
            result = get_object_store_prefix(input_path)
            assert (
                result == expected
            ), f"Failed for {input_path}: got {result}, expected {expected}"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    @patch("os.path.normpath")
    def test_user_absolute_paths_converted_to_relative(
        self, mock_normpath, mock_commonpath, mock_abspath
    ):
        """Test user absolute paths converted for object store usage."""
        # Mock absolute path resolution - paths are NOT under TEMPORARY_PATH
        mock_abspath.side_effect = lambda p: {
            "/data/test.parquet": "/data/test.parquet",
            "/datasets/sales/2024/": "/datasets/sales/2024/",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)

        # Mock commonpath to indicate paths are NOT under temp directory
        # Return a path that's definitely NOT the temp path to trigger user-path logic
        mock_commonpath.return_value = "/different/root"

        # Mock normpath to normalize paths properly
        mock_normpath.side_effect = (
            lambda p: p.lstrip("./") if p.startswith("./") else p
        )

        test_cases = [
            ("/data/test.parquet", "data/test.parquet"),
            (
                "/datasets/sales/2024/",
                "datasets/sales/2024",
            ),  # Remove trailing slash - simplified
            ("/models/trained/v1.pkl", "models/trained/v1.pkl"),
        ]

        for input_path, expected in test_cases:
            result = get_object_store_prefix(input_path)
            assert (
                result == expected
            ), f"Failed for {input_path}: got {result}, expected {expected}"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    @patch("os.path.normpath")
    def test_user_provided_object_store_keys(
        self, mock_normpath, mock_commonpath, mock_abspath
    ):
        """Test user-provided object store keys are returned as-is."""
        # Mock absolute path resolution - paths are NOT under TEMPORARY_PATH
        mock_abspath.side_effect = lambda p: p

        # Mock commonpath to indicate paths are NOT under temp directory
        mock_commonpath.return_value = "/different/root"

        # Mock normpath to return paths as-is
        mock_normpath.side_effect = lambda p: p

        # Test object store keys (always use forward slashes)
        test_cases = [
            "data/file1.parquet",
            "datasets/sales/2024_q1.json",
            "models/model.pkl",
            "/data/file1.parquet",  # Leading slash should be stripped
            "//datasets//sales//file.json",  # Multiple slashes should be normalized
        ]

        expected_results = [
            "data/file1.parquet",
            "datasets/sales/2024_q1.json",
            "models/model.pkl",
            "data/file1.parquet",  # Leading slash stripped
            "datasets//sales//file.json",  # Leading/trailing slashes stripped
        ]

        for object_store_key, expected in zip(test_cases, expected_results):
            result = get_object_store_prefix(object_store_key)
            assert (
                result == expected
            ), f"Failed for object store key '{object_store_key}': got '{result}', expected '{expected}'"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    def test_security_path_normalization(self, mock_commonpath, mock_abspath):
        """Test that path normalization handles security edge cases correctly."""
        # Mock absolute path resolution - paths are NOT under TEMPORARY_PATH
        mock_abspath.side_effect = lambda p: {
            "../../../etc/passwd": "/some/other/../../../etc/passwd",
            "../../sensitive/data": "/some/other/../../sensitive/data",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)

        # Mock commonpath to indicate paths are not under temp
        mock_commonpath.return_value = "/"

        # These should be normalized but not blocked (object store handles security)
        test_cases = [
            ("../../../etc/passwd", "../../../etc/passwd"),
            ("../../sensitive/data", "../../sensitive/data"),
        ]

        for input_path, expected in test_cases:
            result = get_object_store_prefix(input_path)
            assert (
                result == expected
            ), f"Failed for {input_path}: got {result}, expected {expected}"


class TestAutoHeartbeater:
    """Test cases for auto_heartbeater decorator."""

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_success(self, mock_activity):
        """Test successful async auto_heartbeater returns the activity result."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()
        assert result == "success"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_default_timeout(self, mock_activity):
        """Test async auto_heartbeater with default timeout (no heartbeat_timeout set)."""
        mock_activity.info.return_value.heartbeat_timeout = None

        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()
        assert result == "success"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_runtime_error(self, mock_activity):
        """Test async auto_heartbeater when activity.info() raises RuntimeError."""
        mock_activity.info.side_effect = RuntimeError("No activity context")

        @auto_heartbeater
        async def test_activity():
            return "success"

        result = await test_activity()
        assert result == "success"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_function_error(self, mock_activity):
        """Test async auto_heartbeater stops heartbeat thread when activity raises."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity():
            raise ValueError("Function error")

        with pytest.raises(ValueError, match="Function error"):
            await test_activity()

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_heartbeats_during_blocking_call(
        self, mock_activity
    ):
        """Test that heartbeats fire even when async activity blocks the event loop.

        This is the core scenario DBBI-310 addresses: an async activity that calls
        blocking synchronous code (time.sleep) should still get heartbeats because
        the heartbeat thread runs independently of the event loop.
        """
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        async def blocking_async_activity():
            # Block the event loop with a synchronous sleep
            time.sleep(0.3)
            return "done"

        result = await blocking_async_activity()

        assert result == "done"
        # Heartbeat interval = 0.3s / 3 = 0.1s; blocking for 0.3s should yield
        # at least 1 heartbeat call from the background thread
        assert mock_activity.heartbeat.call_count >= 1

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_heartbeat_thread_stops_on_failure(
        self, mock_activity
    ):
        """Test that the heartbeat thread stops when an async activity fails.

        Heartbeats must not continue after the activity has errored out.
        """
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        async def failing_activity():
            time.sleep(0.15)
            raise RuntimeError("activity failed")

        with pytest.raises(RuntimeError, match="activity failed"):
            await failing_activity()

        # Record the heartbeat count right after the activity fails
        count_at_failure = mock_activity.heartbeat.call_count
        # Wait a bit to ensure no more heartbeats arrive
        await asyncio.sleep(0.3)
        assert mock_activity.heartbeat.call_count == count_at_failure

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_function_returns_value(self, mock_activity):
        """Test that sync functions return plain values (not coroutines)."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity():
            return "sync_success"

        result = sync_activity()
        assert result == "sync_success"
        assert not asyncio.iscoroutine(result)

    def test_auto_heartbeater_preserves_sync_nature(self):
        """Test that sync wrapper is not a coroutine function."""

        @auto_heartbeater
        def sync_activity():
            return "test"

        assert not asyncio.iscoroutinefunction(sync_activity)

    def test_auto_heartbeater_preserves_async_nature(self):
        """Test that async wrapper is still a coroutine function."""

        @auto_heartbeater
        async def async_activity():
            return "test"

        assert asyncio.iscoroutinefunction(async_activity)

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_function_error(self, mock_activity):
        """Test that sync function errors propagate correctly."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity():
            raise ValueError("Sync error")

        with pytest.raises(ValueError, match="Sync error"):
            sync_activity()

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_with_arguments(self, mock_activity):
        """Test auto_heartbeater with sync function arguments."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity(arg1, arg2, kwarg1=None):
            return f"{arg1}_{arg2}_{kwarg1}"

        result = sync_activity("a", "b", kwarg1="c")
        assert result == "a_b_c"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_auto_heartbeater_async_with_arguments(self, mock_activity):
        """Test async auto_heartbeater with function arguments."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity(arg1, arg2, kwarg1=None):
            return f"{arg1}_{arg2}_{kwarg1}"

        result = await test_activity("a", "b", kwarg1="c")
        assert result == "a_b_c"

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_heartbeats_during_blocking_call(self, mock_activity):
        """Test that sync activities also get heartbeats during blocking work."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        def blocking_sync_activity():
            time.sleep(0.3)
            return "done"

        result = blocking_sync_activity()

        assert result == "done"
        assert mock_activity.heartbeat.call_count >= 1

    @patch("application_sdk.activities.common.utils.activity")
    def test_auto_heartbeater_sync_heartbeat_thread_stops_on_failure(
        self, mock_activity
    ):
        """Test that the heartbeat thread stops when a sync activity fails."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        def failing_sync_activity():
            time.sleep(0.15)
            raise RuntimeError("sync failed")

        with pytest.raises(RuntimeError, match="sync failed"):
            failing_sync_activity()

        count_at_failure = mock_activity.heartbeat.call_count
        time.sleep(0.3)
        assert mock_activity.heartbeat.call_count == count_at_failure


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


class TestSendPeriodicHeartbeatSync:
    """Test cases for send_periodic_heartbeat_sync function."""

    @patch("application_sdk.activities.common.utils.activity")
    def test_heartbeat_called_and_stops(self, mock_activity):
        """Test that heartbeats are sent and the thread stops on event."""
        stop_event = threading.Event()
        thread = threading.Thread(
            target=send_periodic_heartbeat_sync,
            args=(0.05, stop_event),
            daemon=True,
        )
        thread.start()
        # Poll for at least 2 heartbeats with a generous deadline
        deadline = time.monotonic() + 5
        while mock_activity.heartbeat.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop_event.set()
        thread.join(timeout=2)
        assert not thread.is_alive(), "Heartbeat thread failed to terminate"
        assert mock_activity.heartbeat.call_count >= 2

    @patch("application_sdk.activities.common.utils.activity")
    def test_stops_immediately_on_pre_set_event(self, mock_activity):
        """Test that a pre-set event prevents any heartbeats."""
        stop_event = threading.Event()
        stop_event.set()
        send_periodic_heartbeat_sync(1.0, stop_event)
        mock_activity.heartbeat.assert_not_called()
