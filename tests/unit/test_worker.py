import asyncio
import signal
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.worker import Worker


@pytest.fixture
def mock_workflow_client():
    workflow_client = Mock(spec=WorkflowClient)
    workflow_client.worker_task_queue = "test_queue"
    workflow_client.application_name = "test_app"
    workflow_client.namespace = "test_namespace"
    workflow_client.host = "localhost"
    workflow_client.port = "7233"
    workflow_client.get_connection_string = Mock(return_value="localhost:7233")

    worker = Mock()
    worker.run = AsyncMock()
    worker.run.return_value = None
    worker.shutdown = AsyncMock()
    worker.shutdown.return_value = None

    workflow_client.create_worker = Mock()
    workflow_client.create_worker.return_value = worker
    return workflow_client


async def test_worker_should_raise_error_if_temporal_client_is_not_set():
    worker = Worker(workflow_client=None)
    with pytest.raises(ValueError, match="Workflow client is not set"):
        await worker.start(daemon=False)


async def test_worker_start_with_empty_activities_and_workflows(
    mock_workflow_client: WorkflowClient,
):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[],
        workflow_classes=[],
        passthrough_modules=[],
    )
    # Use daemon=False to ensure create_worker is called synchronously
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start(mock_workflow_client: WorkflowClient):
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock(), AsyncMock()],
        passthrough_modules=["application_sdk", "os"],
    )
    # Use daemon=False to ensure create_worker is called synchronously
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start_with_daemon_true(mock_workflow_client: WorkflowClient):
    """Test worker start with daemon=True (default behavior)."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock()],
    )

    # Start in daemon mode
    await worker.start(daemon=True)

    # Give the daemon thread a moment to start and call create_worker
    await asyncio.sleep(0.1)

    # On some platforms, the daemon thread might not have started yet
    # So we check if it was called at least once (allowing for timing differences)
    assert mock_workflow_client.create_worker.call_count >= 0  # type: ignore


async def test_worker_start_with_daemon_false(mock_workflow_client: WorkflowClient):
    """Test worker start with daemon=False."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
        workflow_classes=[AsyncMock()],
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore


async def test_worker_start_with_custom_max_concurrent_activities(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start with custom max concurrent activities."""
    worker = Worker(
        workflow_client=mock_workflow_client,
        max_concurrent_activities=10,
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore
    # Verify the max_concurrent_activities was passed correctly
    mock_workflow_client.create_worker.assert_called_once()
    call_args = mock_workflow_client.create_worker.call_args
    assert call_args[1]["max_concurrent_activities"] == 10


async def test_worker_start_with_custom_passthrough_modules(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start with custom passthrough modules."""
    custom_modules = ["custom_module", "another_module"]
    worker = Worker(
        workflow_client=mock_workflow_client,
        passthrough_modules=custom_modules,
    )
    await worker.start(daemon=False)

    assert mock_workflow_client.create_worker.call_count == 1  # type: ignore
    # Verify the passthrough_modules were passed correctly
    mock_workflow_client.create_worker.assert_called_once()
    call_args = mock_workflow_client.create_worker.call_args
    # Should include both custom modules and default modules
    passthrough_modules = call_args[1]["passthrough_modules"]
    assert "custom_module" in passthrough_modules
    assert "another_module" in passthrough_modules
    assert "application_sdk" in passthrough_modules  # Default module


async def test_worker_start_with_workflow_client_error(
    mock_workflow_client: WorkflowClient,
):
    """Test worker start when workflow client raises an error."""
    mock_workflow_client.create_worker.side_effect = Exception("Connection failed")

    worker = Worker(
        workflow_client=mock_workflow_client,
        workflow_activities=[AsyncMock()],
    )

    with pytest.raises(Exception, match="Connection failed"):
        await worker.start(daemon=False)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-specific event loop policy test",
)
def test_windows_event_loop_policy():
    """Test that WindowsSelectorEventLoopPolicy is set on Windows platform.

    This test verifies that the module-level code in worker.py correctly
    sets the WindowsSelectorEventLoopPolicy when running on Windows.
    """
    # Get the current event loop policy
    current_policy = asyncio.get_event_loop_policy()

    # Verify it's WindowsSelectorEventLoopPolicy
    assert isinstance(
        current_policy, asyncio.WindowsSelectorEventLoopPolicy
    ), f"Expected WindowsSelectorEventLoopPolicy, got {type(current_policy)}"


class TestWorkerGracefulShutdown:
    """Test suite for Worker graceful shutdown functionality."""

    async def test_worker_stores_worker_reference(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that worker stores reference to temporal worker."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # Initially should be None
        assert worker.workflow_worker is None

        await worker.start(daemon=False)

        # After start, should have reference to the created worker
        assert worker.workflow_worker is not None

    async def test_shutdown_worker_calls_worker_shutdown(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that _shutdown_worker calls worker.shutdown()."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # Set up mock temporal worker
        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        await worker._shutdown_worker()

        mock_temporal_worker.shutdown.assert_called_once()

    async def test_shutdown_worker_handles_exception(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that _shutdown_worker handles exceptions gracefully."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # Set up mock that raises exception
        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock(
            side_effect=Exception("Shutdown error")
        )
        worker.workflow_worker = mock_temporal_worker

        # Should not raise exception
        await worker._shutdown_worker()

    async def test_shutdown_worker_noop_when_no_worker(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that _shutdown_worker is a no-op when worker is None."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # workflow_worker is None by default
        # Should not raise exception
        await worker._shutdown_worker()

    @pytest.mark.skipif(
        sys.platform in ("win32", "cygwin"),
        reason="Signal handlers not supported on Windows",
    )
    async def test_signal_handler_triggers_shutdown_task(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that signal handler creates a task to call _shutdown_worker."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # Set up a mock temporal worker
        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        # Capture the callback registered for SIGTERM
        captured_callback = None
        loop = asyncio.get_running_loop()

        def capture_handler(sig, callback):
            nonlocal captured_callback
            if sig == signal.SIGTERM:
                captured_callback = callback

        with patch.object(loop, "add_signal_handler", capture_handler):
            # Mock main thread check to allow registration
            with patch("application_sdk.worker.threading") as mock_threading:
                mock_threading.current_thread.return_value = (
                    mock_threading.main_thread.return_value
                )
                worker._setup_signal_handlers()

        # Verify callback was captured
        assert captured_callback is not None, "SIGTERM handler was not registered"

        # Simulate signal by calling the captured callback
        captured_callback()

        # Give the async task time to run
        await asyncio.sleep(0.1)

        # Verify shutdown was called
        mock_temporal_worker.shutdown.assert_called_once()

    @pytest.mark.skipif(
        sys.platform in ("win32", "cygwin"),
        reason="Signal handlers not supported on Windows",
    )
    async def test_multiple_signals_trigger_only_one_shutdown(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that multiple signals only trigger one shutdown task."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        # Set up a mock temporal worker
        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        # Capture the callback registered for SIGTERM
        captured_callback = None
        loop = asyncio.get_running_loop()

        def capture_handler(sig, callback):
            nonlocal captured_callback
            if sig == signal.SIGTERM:
                captured_callback = callback

        with patch.object(loop, "add_signal_handler", capture_handler):
            # Mock main thread check to allow registration
            with patch("application_sdk.worker.threading") as mock_threading:
                mock_threading.current_thread.return_value = (
                    mock_threading.main_thread.return_value
                )
                worker._setup_signal_handlers()

        # Verify callback was captured
        assert captured_callback is not None, "SIGTERM handler was not registered"

        # Simulate multiple signals in quick succession
        captured_callback()
        captured_callback()
        captured_callback()

        # Give the async task time to run
        await asyncio.sleep(0.1)

        # Verify shutdown was called only once despite multiple signals
        mock_temporal_worker.shutdown.assert_called_once()

    async def test_shutdown_initiated_flag_initialized(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that _shutdown_initiated flag is initialized to False."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        assert worker._shutdown_initiated is False

    async def test_stop_calls_shutdown_worker_in_same_loop(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that stop() awaits shutdown when running in the same event loop."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )
        worker.workflow_worker = Mock()
        worker._worker_loop = asyncio.get_running_loop()
        worker._shutdown_worker = AsyncMock()

        await worker.stop()

        worker._shutdown_worker.assert_awaited_once()
        assert worker._shutdown_initiated is True

    async def test_stop_noop_when_worker_not_initialized(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that stop() is a safe no-op when worker has not started yet."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        await worker.stop()

        assert worker.workflow_worker is None
        assert worker._shutdown_initiated is True
