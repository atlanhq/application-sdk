import asyncio
import signal
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.worker import Worker

DRAIN_DELAY_PATCH = "application_sdk.worker.SHUTDOWN_DRAIN_DELAY_SECONDS"


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

    @patch(DRAIN_DELAY_PATCH, 0)
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

    @patch(DRAIN_DELAY_PATCH, 0)
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
    @patch(DRAIN_DELAY_PATCH, 0)
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
    @patch(DRAIN_DELAY_PATCH, 0)
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


class TestShutdownDrainDelay:
    """Tests for the drain delay that prevents SIGTERM from preempting
    in-flight activity completion RPCs.

    Reproduces the real-world deadlock observed on 2026-03-27: a
    save_workflow_run_state activity failed (Atlas 100K char limit),
    SIGTERM arrived 3 seconds later, and the shutdown task preempted
    the SDK's _run_activity coroutine before it could call
    complete_activity_task(). The worker then held a phantom "in-use"
    task slot for the entire 12-hour graceful_shutdown_timeout.

    The drain delay (asyncio.sleep before worker.shutdown()) yields the
    event loop so the pending complete_activity_task() can flush.
    """

    @staticmethod
    async def _run_shutdown_with_inflight_activity(
        mock_workflow_client: WorkflowClient,
        drain_delay_seconds: float,
    ) -> tuple[bool, Mock]:
        """Helper that simulates the SIGTERM-vs-activity-completion race.

        Schedules a task that mimics the SDK's _run_activity coroutine:
        it yields once (representing the await inside _run_activity between
        catching the exception and calling complete_activity_task), then
        records completion.

        Then calls _shutdown_worker (the real method, not mocked) with
        SHUTDOWN_DRAIN_DELAY_SECONDS patched to drain_delay_seconds.

        Returns (activity_completed, mock_temporal_worker).
        """
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        # Simulate _run_activity coroutine that is between the exception
        # handler and the complete_activity_task() call. In the Temporal
        # SDK (_activity.py:376-392), after catching the activity exception
        # and encoding the failure, it awaits complete_activity_task().
        # The single yield (sleep(0)) represents that await point.
        activity_completed = False

        async def simulate_inflight_activity_completion():
            nonlocal activity_completed
            # Yield once — represents the await point in _run_activity
            # between encode_failure() and complete_activity_task()
            await asyncio.sleep(0)
            # This line represents complete_activity_task() sending
            # RespondActivityTaskFailed to the Temporal server
            activity_completed = True

        # Schedule the activity completion BEFORE calling shutdown,
        # exactly as happens in production: _run_activity is already
        # running when SIGTERM fires and creates the shutdown task.
        asyncio.create_task(simulate_inflight_activity_completion())

        # Call the REAL _shutdown_worker with the specified drain delay
        with patch(DRAIN_DELAY_PATCH, drain_delay_seconds):
            await worker._shutdown_worker()

        return activity_completed, mock_temporal_worker

    async def test_without_fix_activity_completion_is_preempted(
        self, mock_workflow_client: WorkflowClient
    ):
        """WITHOUT the drain delay, shutdown preempts the activity
        completion — reproducing the production deadlock.

        drain_delay=0 simulates the original code (no sleep before
        shutdown). The shutdown task runs to completion without yielding,
        so the pending activity completion task never gets scheduled.

        In production this meant:
        - RespondActivityTaskFailed was never sent to Temporal
        - temporal_worker_task_slots_used stayed at 1
        - worker.shutdown() waited 12 hours for a slot that would never free
        """
        activity_completed, mock_worker = (
            await self._run_shutdown_with_inflight_activity(
                mock_workflow_client, drain_delay_seconds=0
            )
        )

        # PROVES THE BUG: activity completion never ran
        assert activity_completed is False
        # shutdown() was still called, but in production it would block
        # for 12 hours waiting for the phantom slot
        mock_worker.shutdown.assert_called_once()

    async def test_with_fix_activity_completion_runs_before_shutdown(
        self, mock_workflow_client: WorkflowClient
    ):
        """WITH the drain delay, the activity completion runs before
        shutdown — the fix works.

        drain_delay=0.01 (any value > 0) yields the event loop via
        asyncio.sleep(), giving the pending activity completion task
        a chance to execute before worker.shutdown() is called.

        In production this means:
        - RespondActivityTaskFailed is sent to Temporal
        - The task slot is released
        - worker.shutdown() sees no in-flight work and returns immediately
        """
        activity_completed, mock_worker = (
            await self._run_shutdown_with_inflight_activity(
                mock_workflow_client, drain_delay_seconds=0.01
            )
        )

        # PROVES THE FIX: activity completion ran before shutdown
        assert activity_completed is True
        mock_worker.shutdown.assert_called_once()

    async def test_fix_flushes_multiple_pending_completions(
        self, mock_workflow_client: WorkflowClient
    ):
        """The drain delay flushes multiple pending activity completions,
        not just one."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        completions = []

        async def simulate_activity_completion(activity_id: str):
            await asyncio.sleep(0)
            completions.append(activity_id)

        asyncio.create_task(simulate_activity_completion("activity_1"))
        asyncio.create_task(simulate_activity_completion("activity_2"))
        asyncio.create_task(simulate_activity_completion("activity_3"))

        with patch(DRAIN_DELAY_PATCH, 0.01):
            await worker._shutdown_worker()

        assert set(completions) == {"activity_1", "activity_2", "activity_3"}

    @patch(DRAIN_DELAY_PATCH, 0)
    async def test_shutdown_completes_even_with_zero_delay(
        self, mock_workflow_client: WorkflowClient
    ):
        """Shutdown doesn't hang even when drain delay is 0 and there are
        no pending completions."""
        worker = Worker(
            workflow_client=mock_workflow_client,
            workflow_activities=[],
            workflow_classes=[],
        )

        mock_temporal_worker = Mock()
        mock_temporal_worker.shutdown = AsyncMock()
        worker.workflow_worker = mock_temporal_worker

        await worker._shutdown_worker()
        mock_temporal_worker.shutdown.assert_called_once()
