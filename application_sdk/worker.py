"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution.
"""

import asyncio
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, List, Optional, Sequence

from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker as TemporalWorker

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import (
    DEPLOYMENT_NAME,
    GRACEFUL_SHUTDOWN_TIMEOUT,
    MAX_CONCURRENT_ACTIVITIES,
)
from application_sdk.interceptors.models import (
    ApplicationEventNames,
    Event,
    EventTypes,
    WorkerStartEventData,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.eventstore import EventStore

logger = get_logger(__name__)


if sys.platform not in ("win32", "cygwin"):
    try:
        import uvloop

        # Use uvloop for performance optimization on supported platforms (not Windows)
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except (ImportError, ModuleNotFoundError):
        # uvloop is not available, use default asyncio
        logger.warning("uvloop is not available, using default asyncio")
        pass
elif sys.platform == "win32":
    # Use WindowsSelectorEventLoopPolicy for Windows platform
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class _ShutdownEvent:
    """Thread-safe shutdown event that can be awaited from any event loop.

    This class provides a unified mechanism for signaling shutdown across threads.
    It uses a threading.Event for cross-thread signaling and provides an async
    wait method that can be used from any event loop.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        """Signal shutdown. Thread-safe, can be called from any thread."""
        self._event.set()

    def is_set(self) -> bool:
        """Check if shutdown has been signaled. Thread-safe."""
        return self._event.is_set()

    async def wait(self) -> None:
        """Wait for shutdown signal. Can be awaited from any event loop."""
        while not self._event.is_set():
            await asyncio.sleep(0.1)


class Worker:
    """Worker class for managing Temporal workflow workers.

    This class handles the initialization and execution of Temporal workers,
    including their activities, workflows, and module configurations.

    Attributes:
        workflow_client: Client for interacting with Temporal.
        workflow_worker: The Temporal worker instance.
        workflow_activities: List of activity functions.
        workflow_classes: List of workflow classes.
        passthrough_modules: List of module names to pass through.
        max_concurrent_activities: Maximum number of concurrent activities.
        graceful_shutdown_timeout: Time to wait for in-flight activities during shutdown.
        is_draining: Whether the worker is currently shutting down.

    Note:
        This class is designed to be thread-safe when running workers in daemon mode.
        However, care should be taken when modifying worker attributes after initialization.

        Graceful Shutdown Behavior:
        Signal handlers for SIGTERM and SIGINT are automatically registered when start()
        is called from the main thread. Upon receiving these signals:
        1. The signal handler sets a shutdown flag (non-blocking)
        2. The worker detects the flag and stops polling for new tasks
        3. In-flight activities are allowed to complete (up to graceful_shutdown_timeout)
        4. The worker exits as soon as all activities complete or timeout is reached

    Example:
        >>> from application_sdk.worker import Worker
        >>> from my_workflow import MyWorkflow, my_activity
        >>>
        >>> worker = Worker(
        ...     workflow_client=client,
        ...     workflow_activities=[my_activity],
        ...     workflow_classes=[MyWorkflow],
        ...     graceful_shutdown_timeout=timedelta(seconds=7200) # 2 hours
        ... )
        >>> await worker.start(daemon=True)
        >>> worker.register_signal_handlers()  # Always call from main thread
    """

    default_passthrough_modules = ["application_sdk", "pandas", "os", "app"]

    def __init__(
        self,
        workflow_client: Optional[WorkflowClient] = None,
        workflow_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = [],
        workflow_classes: Sequence[ClassType] = [],
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
        activity_executor: Optional[ThreadPoolExecutor] = None,
        graceful_shutdown_timeout: Optional[timedelta] = None,
    ):
        """Initialize the Worker.

        Args:
            workflow_client: Client for interacting with Temporal.
                Defaults to None.
            workflow_activities: List of activity functions.
                Defaults to empty list.
            passthrough_modules: List of module names to pass through.
                Defaults to ["application_sdk", "pandas", "os", "app"].
            workflow_classes: List of workflow classes.
                Defaults to empty list.
            max_concurrent_activities: Maximum number of activities that can run
                concurrently. Defaults to None (no limit).
            activity_executor: Executor for running activities.
                Defaults to None (uses a default thread pool executor).
            graceful_shutdown_timeout: Time to wait for in-flight activities to
                complete during graceful shutdown. Defaults to GRACEFUL_SHUTDOWN_TIMEOUT
                constant (2 hours). Ensure Kubernetes terminationGracePeriodSeconds
                is set to match or exceed this value.

        Returns:
            None

        Raises:
            TypeError: If workflow_activities contains non-callable items.
            ValueError: If passthrough_modules contains invalid module names.
        """
        self.workflow_client = workflow_client
        self.workflow_worker: Optional[TemporalWorker] = None
        self.workflow_activities = workflow_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = list(
            set(passthrough_modules + self.default_passthrough_modules)
        )
        self.max_concurrent_activities = max_concurrent_activities
        self.graceful_shutdown_timeout = (
            graceful_shutdown_timeout
            if graceful_shutdown_timeout is not None
            else GRACEFUL_SHUTDOWN_TIMEOUT
        )

        # Shutdown event for cross-thread signaling
        self._shutdown_event = _ShutdownEvent()

        self.activity_executor = activity_executor or ThreadPoolExecutor(
            max_workers=max_concurrent_activities or 5,
            thread_name_prefix="activity-pool-",
        )

        # Store event data for later publishing
        self._worker_creation_event_data = None
        if self.workflow_client:
            self._worker_creation_event_data = WorkerStartEventData(
                application_name=self.workflow_client.application_name,
                deployment_name=DEPLOYMENT_NAME,
                task_queue=self.workflow_client.worker_task_queue,
                namespace=self.workflow_client.namespace,
                host=self.workflow_client.host,
                port=self.workflow_client.port,
                connection_string=self.workflow_client.get_connection_string(),
                max_concurrent_activities=max_concurrent_activities,
                workflow_count=len(workflow_classes),
                activity_count=len(workflow_activities),
            )

    @property
    def is_draining(self) -> bool:
        """Check if the worker is currently draining (shutting down).

        This property can be used by health probes to return 503 when the worker
        is in the process of shutting down, preventing new traffic from being
        routed to this instance.

        Returns:
            bool: True if the worker is draining, False otherwise.
        """
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        """Request graceful shutdown of the worker.

        This method signals the worker to begin graceful shutdown. It is thread-safe
        and can be called from any thread (including signal handlers).

        The actual shutdown is performed by the worker's event loop, which monitors
        the shutdown event. This design allows for clean cross-thread communication
        without the complexity of managing multiple event loops.

        Example:
            >>> # From a signal handler or any thread
            >>> worker.request_shutdown()
        """
        if self._shutdown_event.is_set():
            logger.info("Shutdown already requested, ignoring duplicate call")
            return

        self._shutdown_event.set()
        logger.info(
            "Shutdown requested. "
            f"Worker will wait up to {self.graceful_shutdown_timeout.total_seconds()}s "
            "for in-flight activities to complete."
        )

    def register_signal_handlers(self) -> None:
        """Register signal handlers for graceful shutdown.

        This method registers SIGTERM and SIGINT handlers that will trigger
        graceful shutdown of the worker. It must be called from the main thread.

        The signal handlers simply call request_shutdown(), which sets a flag
        that the worker monitors. This keeps the signal handler simple and
        avoids cross-thread async complexity.

        Example:
            >>> worker = Worker(workflow_client=client, ...)
            >>> await worker.start(daemon=True)
            >>> worker.register_signal_handlers()  # Call from main thread
            >>>
            >>> # Or in non-daemon mode:
            >>> worker.register_signal_handlers()
            >>> await worker.start(daemon=False)

        Note:
            - Must be called from the main thread
            - On Windows, this method will log a warning and return without
              registering handlers (Windows has limited signal support)
            - The signal handler is non-blocking and simply sets a shutdown flag
        """
        if sys.platform in ("win32", "cygwin"):
            logger.warning(
                "Signal handlers for graceful shutdown are not supported on Windows"
            )
            return

        if threading.current_thread() is not threading.main_thread():
            logger.warning(
                "register_signal_handlers() must be called from the main thread"
            )
            return

        def signal_handler(signum: int, frame: Any) -> None:
            """Handle termination signals by requesting shutdown."""
            sig_name = signal.Signals(signum).name
            logger.info(f"Received {sig_name}, requesting graceful shutdown")
            self.request_shutdown()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal_handler)
            logger.debug(f"Registered signal handler for {sig.name}")

    async def _perform_shutdown(self) -> None:
        """Perform graceful shutdown of the Temporal worker.

        This method initiates the shutdown process which:
        1. Stops polling for new workflow/activity tasks
        2. Waits for in-flight activities to complete (up to graceful_shutdown_timeout)
        3. Cancels any remaining activities after timeout
        4. Shuts down the activity executor

        This is called internally when the shutdown event is set.
        """
        if not self.workflow_worker:
            logger.warning("No workflow worker to shutdown")
            return

        try:
            logger.info("Calling worker.shutdown() to stop polling and drain tasks")
            await self.workflow_worker.shutdown()
            logger.info("Worker shutdown completed successfully")
        except Exception as e:
            logger.error(f"Error during worker shutdown: {e}")
        finally:
            # Shutdown the activity executor to release thread resources
            if self.activity_executor:
                logger.debug("Shutting down activity executor")
                self.activity_executor.shutdown(wait=False)

            # Close the workflow client to stop token refresh and cleanup
            if self.workflow_client:
                try:
                    await self.workflow_client.close()
                    logger.debug("Workflow client closed")
                except Exception as e:
                    logger.warning(f"Error closing workflow client: {e}")

    async def _monitor_shutdown(self) -> None:
        """Monitor for shutdown signal and trigger graceful shutdown.

        This coroutine runs alongside the worker and waits for the shutdown
        event to be set. When set, it triggers graceful shutdown of the worker.

        This design allows signal handlers to simply set a flag, while the
        actual async shutdown work happens in the worker's event loop.
        """
        await self._shutdown_event.wait()
        logger.info("Shutdown event detected, initiating graceful shutdown")
        await self._perform_shutdown()

    async def start(self, daemon: bool = True, *args: Any, **kwargs: Any) -> None:
        """Start the Temporal worker.

        This method starts the worker either in the current thread or as a daemon
        thread based on the daemon parameter.

        Args:
            daemon: Whether to run the worker in a daemon thread.
                Defaults to True.
            *args: Additional positional arguments passed to the worker.
            **kwargs: Additional keyword arguments passed to the worker.

        Returns:
            None

        Raises:
            ValueError: If workflow_client is not set.
            RuntimeError: If worker creation fails.
            ConnectionError: If connection to Temporal server fails.

        Note:
            When daemon=True, the worker runs in a daemon background thread and
            does not block the main thread. The main thread continues running
            (e.g., serving FastAPI requests, sleeping, etc.).

            Graceful Shutdown:
            Signal handlers for SIGTERM/SIGINT are automatically registered when
            start() is called from the main thread. Upon receiving these signals:
            - Signal handler sets a shutdown flag (non-blocking, returns immediately)
            - Main thread continues running (doesn't exit)
            - Worker thread detects the shutdown flag via _monitor_shutdown()
            - Worker stops polling for new tasks
            - In-flight activities are allowed to complete (up to graceful_shutdown_timeout)
            - Worker exits as soon as all activities complete or timeout is reached
            
            The key insight: Signal handlers don't exit the main thread, they just set
            a flag. This allows graceful shutdown to work even with daemon threads.
        """
        # Auto-register signal handlers if called from main thread
        # This ensures graceful shutdown works out of the box
        if threading.current_thread() is threading.main_thread():
            self.register_signal_handlers()

        if daemon:
            worker_thread = threading.Thread(
                target=lambda: asyncio.run(self.start(daemon=False)), daemon=True
            )
            worker_thread.start()
            return

        if not self.workflow_client:
            raise ValueError("Workflow client is not set")

        if self._worker_creation_event_data:
            worker_creation_event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.WORKER_START.value,
                data=self._worker_creation_event_data.model_dump(),
            )

            await EventStore.publish_event(worker_creation_event)

        try:
            worker = self.workflow_client.create_worker(
                activities=self.workflow_activities,
                workflow_classes=self.workflow_classes,
                passthrough_modules=self.passthrough_modules,
                max_concurrent_activities=self.max_concurrent_activities,
                activity_executor=self.activity_executor,
                graceful_shutdown_timeout=self.graceful_shutdown_timeout,
            )

            # Store reference for potential external access
            self.workflow_worker = worker

            logger.info(
                f"Starting worker with task queue: {self.workflow_client.worker_task_queue}"
            )

            # Start shutdown monitor as a background task
            # It will trigger shutdown when the shutdown event is set
            monitor_task = asyncio.create_task(self._monitor_shutdown())

            try:
                # Run the worker - this blocks until shutdown is complete
                await worker.run()
            finally:
                # Cancel the monitor task if it's still running
                # (e.g., if worker.run() returned without shutdown being requested)
                if not monitor_task.done():
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass

            logger.info("Worker run() completed, shutdown is complete")
        except Exception as e:
            logger.error(f"Error starting worker: {e}")
            raise e
