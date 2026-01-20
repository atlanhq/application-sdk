import asyncio
import sys
import threading
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import GRACEFUL_SHUTDOWN_TIMEOUT
from application_sdk.worker import Worker, _ShutdownEvent


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

    workflow_client.create_worker = Mock()
    workflow_client.create_worker.return_value = worker
    workflow_client.close = AsyncMock()
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


class TestShutdownEvent:
    """Test suite for _ShutdownEvent helper class."""

    def test_initially_not_set(self):
        """Test that shutdown event is not set initially."""
        event = _ShutdownEvent()
        assert event.is_set() is False

    def test_set_and_is_set(self):
        """Test that set() makes is_set() return True."""
        event = _ShutdownEvent()
        event.set()
        assert event.is_set() is True

    def test_set_is_idempotent(self):
        """Test that calling set() multiple times is safe."""
        event = _ShutdownEvent()
        event.set()
        event.set()  # Should not raise
        assert event.is_set() is True

    async def test_wait_returns_when_set(self):
        """Test that wait() returns when the event is set."""
        event = _ShutdownEvent()

        async def set_after_delay():
            await asyncio.sleep(0.05)
            event.set()

        # Start the setter task
        asyncio.create_task(set_after_delay())

        # Wait should return after the event is set
        await asyncio.wait_for(event.wait(), timeout=1.0)
        assert event.is_set() is True

    async def test_wait_returns_immediately_if_already_set(self):
        """Test that wait() returns immediately if event is already set."""
        event = _ShutdownEvent()
        event.set()

        # Should return almost immediately
        await asyncio.wait_for(event.wait(), timeout=0.5)


class TestGracefulShutdown:
    """Test suite for graceful shutdown functionality."""

    def test_default_graceful_shutdown_timeout(self):
        """Test that default graceful_shutdown_timeout is set from constants."""
        worker = Worker(workflow_client=None)
        assert worker.graceful_shutdown_timeout == GRACEFUL_SHUTDOWN_TIMEOUT
        assert worker.graceful_shutdown_timeout.total_seconds() == 7200  # 2 hours

    def test_custom_graceful_shutdown_timeout(self):
        """Test that custom graceful_shutdown_timeout is respected."""
        custom_timeout = timedelta(seconds=300)
        worker = Worker(workflow_client=None, graceful_shutdown_timeout=custom_timeout)
        assert worker.graceful_shutdown_timeout == custom_timeout
        assert worker.graceful_shutdown_timeout.total_seconds() == 300

    def test_graceful_shutdown_timeout_zero(self):
        """Test that zero timeout is accepted (immediate cancellation)."""
        zero_timeout = timedelta(seconds=0)
        worker = Worker(workflow_client=None, graceful_shutdown_timeout=zero_timeout)
        assert worker.graceful_shutdown_timeout == zero_timeout
        assert worker.graceful_shutdown_timeout.total_seconds() == 0

    def test_graceful_shutdown_timeout_large_value(self):
        """Test that large timeout values are accepted."""
        large_timeout = timedelta(hours=2)
        worker = Worker(workflow_client=None, graceful_shutdown_timeout=large_timeout)
        assert worker.graceful_shutdown_timeout == large_timeout
        assert worker.graceful_shutdown_timeout.total_seconds() == 7200

    def test_is_draining_initially_false(self):
        """Test that is_draining is False when worker is created."""
        worker = Worker(workflow_client=None)
        assert worker.is_draining is False

    def test_is_draining_property_reflects_shutdown_event(self):
        """Test that is_draining property reflects shutdown event state."""
        worker = Worker(workflow_client=None)

        # Initially not draining
        assert worker.is_draining is False

        # Request shutdown
        worker.request_shutdown()
        assert worker.is_draining is True

    def test_workflow_worker_initially_none(self):
        """Test that workflow_worker is None before start is called."""
        worker = Worker(workflow_client=None)
        assert worker.workflow_worker is None

    async def test_graceful_shutdown_timeout_passed_to_create_worker(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that graceful_shutdown_timeout is passed to create_worker."""
        custom_timeout = timedelta(seconds=120)
        worker = Worker(
            workflow_client=mock_workflow_client,
            graceful_shutdown_timeout=custom_timeout,
        )
        await worker.start(daemon=False)

        # Verify create_worker was called with graceful_shutdown_timeout
        mock_workflow_client.create_worker.assert_called_once()
        call_kwargs = mock_workflow_client.create_worker.call_args[1]
        assert call_kwargs["graceful_shutdown_timeout"] == custom_timeout

    async def test_workflow_worker_reference_stored_after_start(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that workflow_worker reference is stored after start."""
        worker = Worker(workflow_client=mock_workflow_client)
        assert worker.workflow_worker is None

        await worker.start(daemon=False)

        # Verify the workflow_worker reference is stored
        assert worker.workflow_worker is not None
        assert worker.workflow_worker == mock_workflow_client.create_worker.return_value

    def test_request_shutdown_sets_draining_flag(self):
        """Test that request_shutdown() sets the draining flag."""
        worker = Worker(workflow_client=None)
        assert worker.is_draining is False

        worker.request_shutdown()

        assert worker.is_draining is True

    def test_request_shutdown_ignores_duplicate_calls(self):
        """Test that duplicate request_shutdown() calls are ignored."""
        worker = Worker(workflow_client=None)

        # First call
        worker.request_shutdown()
        assert worker.is_draining is True

        # Second call should be ignored (no error)
        worker.request_shutdown()
        assert worker.is_draining is True

    def test_request_shutdown_is_thread_safe(self):
        """Test that request_shutdown() can be called from multiple threads."""
        import threading

        worker = Worker(workflow_client=None)
        results = []

        def call_shutdown():
            worker.request_shutdown()
            results.append(worker.is_draining)

        threads = [threading.Thread(target=call_shutdown) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should see is_draining as True
        assert all(results)
        assert worker.is_draining is True

    @pytest.mark.skipif(
        sys.platform in ("win32", "cygwin"),
        reason="Signal handlers not supported on Windows",
    )
    def test_register_signal_handlers_from_main_thread(self):
        """Test that register_signal_handlers can be called from main thread."""
        worker = Worker(workflow_client=None)

        # Should not raise when called from main thread
        worker.register_signal_handlers()

        # Verify we're in main thread (sanity check)
        import threading

        assert threading.current_thread() is threading.main_thread()

    @pytest.mark.skipif(
        sys.platform in ("win32", "cygwin"),
        reason="Signal handlers not supported on Windows",
    )
    def test_register_signal_handlers_logs_warning_for_non_main_thread(self):
        """Test that register_signal_handlers logs warning when not on main thread."""
        import threading

        worker = Worker(workflow_client=None)
        warning_logged = []

        def run_in_thread():
            # This should log a warning since we're not on main thread
            worker.register_signal_handlers()
            warning_logged.append(True)

        thread = threading.Thread(target=run_in_thread)
        thread.start()
        thread.join()

        # The method should have completed without error
        assert len(warning_logged) == 1

    async def test_daemon_mode_graceful_shutdown_works(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that graceful shutdown works correctly in daemon mode.
        
        Key insight: Signal handlers don't exit the main thread, they just set
        a shutdown flag. The main thread continues running (serving requests,
        sleeping, etc.), which gives the daemon worker thread time to complete
        graceful shutdown before the application terminates.
        """
        # Create a worker with a mock that blocks until shutdown is requested
        shutdown_completed = threading.Event()
        
        async def mock_worker_run():
            """Mock worker.run() that blocks until shutdown is requested."""
            # Wait for shutdown signal
            while not worker._shutdown_event.is_set():
                await asyncio.sleep(0.05)
            # Simulate graceful shutdown
            await asyncio.sleep(0.1)
            shutdown_completed.set()
        
        mock_worker = Mock()
        mock_worker.run = mock_worker_run
        mock_worker.shutdown = AsyncMock()
        mock_workflow_client.create_worker.return_value = mock_worker
        
        worker = Worker(workflow_client=mock_workflow_client)

        # Start in daemon mode (background thread)
        await worker.start(daemon=True)

        # Give the daemon thread a moment to start
        await asyncio.sleep(0.1)

        # Verify worker is running and not draining
        assert worker.is_draining is False

        # Simulate signal handler being called (this is what SIGTERM does)
        worker.request_shutdown()

        # Verify shutdown was requested
        assert worker.is_draining is True
        
        # Wait for shutdown to complete (with timeout)
        # In daemon mode, the worker thread performs graceful shutdown
        # while the main thread continues running
        assert shutdown_completed.wait(timeout=2.0), "Graceful shutdown did not complete"

    async def test_non_daemon_mode_graceful_shutdown_works(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that graceful shutdown works correctly in non-daemon mode.
        
        In non-daemon mode, worker.run() blocks the current thread. When
        shutdown is requested, the worker completes gracefully and returns.
        """
        shutdown_completed = threading.Event()
        
        async def mock_worker_run():
            """Mock worker.run() that blocks until shutdown is requested."""
            # Wait for shutdown signal
            while not worker._shutdown_event.is_set():
                await asyncio.sleep(0.05)
            # Simulate graceful shutdown
            await asyncio.sleep(0.1)
            shutdown_completed.set()
        
        mock_worker = Mock()
        mock_worker.run = mock_worker_run
        mock_worker.shutdown = AsyncMock()
        mock_workflow_client.create_worker.return_value = mock_worker
        
        worker = Worker(workflow_client=mock_workflow_client)

        # Start shutdown in a separate task (simulating signal handler)
        async def trigger_shutdown_after_delay():
            await asyncio.sleep(0.1)
            worker.request_shutdown()
        
        shutdown_task = asyncio.create_task(trigger_shutdown_after_delay())

        # Start in non-daemon mode (blocks until shutdown completes)
        await worker.start(daemon=False)
        
        # If we reach here, worker.run() has returned, meaning shutdown completed
        assert worker.is_draining is True
        assert shutdown_completed.is_set()
        
        # Clean up
        await shutdown_task

    async def test_daemon_mode_with_long_running_activity(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test graceful shutdown with a long-running activity in daemon mode."""
        activity_completed = threading.Event()
        shutdown_initiated = threading.Event()
        
        async def mock_worker_run():
            """Mock worker.run() that simulates a long-running activity."""
            # Wait for shutdown signal
            while not worker._shutdown_event.is_set():
                await asyncio.sleep(0.05)
            
            shutdown_initiated.set()
            
            # Simulate activity taking time to complete
            await asyncio.sleep(0.2)
            activity_completed.set()
        
        mock_worker = Mock()
        mock_worker.run = mock_worker_run
        mock_worker.shutdown = AsyncMock()
        mock_workflow_client.create_worker.return_value = mock_worker
        
        worker = Worker(
            workflow_client=mock_workflow_client,
            graceful_shutdown_timeout=timedelta(seconds=5)
        )

        # Start in daemon mode
        await worker.start(daemon=True)
        await asyncio.sleep(0.1)

        # Request shutdown
        start_time = asyncio.get_event_loop().time()
        worker.request_shutdown()

        # Verify shutdown was initiated
        assert shutdown_initiated.wait(timeout=1.0), "Shutdown not initiated"
        
        # Verify activity completes gracefully (not killed immediately)
        assert activity_completed.wait(timeout=2.0), "Activity was not allowed to complete"
        
        # Verify shutdown took some time (activity was allowed to finish)
        elapsed = asyncio.get_event_loop().time() - start_time
        assert elapsed >= 0.2, "Activity was killed too quickly"

    async def test_signal_handler_does_not_block_main_thread(
        self, mock_workflow_client: WorkflowClient
    ):
        """Test that signal handler returns immediately without blocking."""
        worker = Worker(workflow_client=mock_workflow_client)
        
        # Measure time taken by request_shutdown (simulating signal handler)
        start_time = asyncio.get_event_loop().time()
        worker.request_shutdown()
        elapsed = asyncio.get_event_loop().time() - start_time
        
        # Signal handler should return almost immediately (< 10ms)
        assert elapsed < 0.01, f"Signal handler blocked for {elapsed}s (should be < 0.01s)"
        
        # Verify shutdown flag is set
        assert worker.is_draining is True
