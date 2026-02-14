"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution with graceful shutdown support.
"""

import asyncio
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker as TemporalWorker

from application_sdk.clients.workflow import WorkflowClient

if TYPE_CHECKING:
    from application_sdk.credentials import Credential
from application_sdk.constants import (
    DEPLOYMENT_NAME,
    GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
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


class Worker:
    """Worker class for managing Temporal workflow workers.

    This class handles the initialization and execution of Temporal workers,
    including their activities, workflows, module configurations, and graceful shutdown.

    Attributes:
        workflow_client: Client for interacting with Temporal.
        workflow_worker: The Temporal worker instance.
        workflow_activities: List of activity functions.
        workflow_classes: List of workflow classes.
        passthrough_modules: List of module names to pass through.
        max_concurrent_activities: Maximum number of concurrent activities.

    Graceful Shutdown:
        When SIGTERM or SIGINT is received:
        1. Signal handlers trigger worker.shutdown()
        2. Worker stops polling for new tasks
        3. In-flight activities are allowed to complete within graceful_shutdown_timeout
        4. Worker exits early if all activities complete, or at timeout

    Note:
        This class is designed to be thread-safe when running workers in daemon mode.
        However, care should be taken when modifying worker attributes after initialization.

    Example:
        >>> from application_sdk.worker import Worker
        >>> from my_workflow import MyWorkflow, my_activity
        >>>
        >>> worker = Worker(
        ...     workflow_client=client,
        ...     workflow_activities=[my_activity],
        ...     workflow_classes=[MyWorkflow]
        ... )
        >>> await worker.start(daemon=True)
    """

    default_passthrough_modules = ["application_sdk", "pandas", "os", "app"]

    def _setup_signal_handlers(self) -> None:
        """Set up SIGTERM and SIGINT handlers for graceful shutdown.

        Signal handlers can only be registered from the main thread.

        Platform Notes:
            - Unix/Linux/macOS: Full signal handling support via asyncio event loop.
            - Windows: Signal handling is not supported. Workers on Windows will not
              respond to SIGTERM/SIGINT for graceful shutdown. On Windows, the worker
              will continue running until the process is forcefully terminated.
        """
        # Signal handlers only work on Unix-like systems
        if sys.platform in ("win32", "cygwin"):
            logger.warning(
                "Signal handlers not supported on Windows. "
                "Graceful shutdown via SIGTERM/SIGINT is not available. "
                "For production deployments, use Unix-based systems."
            )
            return

        # Signal handlers can only be registered from the main thread
        if threading.current_thread() is not threading.main_thread():
            logger.debug(
                "Skipping signal handler registration - not running in main thread"
            )
            return

        loop = asyncio.get_running_loop()

        def handle_signal(sig_name: str) -> None:
            """Handle shutdown signal by triggering worker.shutdown().

            Uses a flag to prevent multiple shutdown tasks from being created
            if multiple signals are received in quick succession.
            """
            if self._shutdown_initiated:
                logger.debug(f"Received {sig_name}, but shutdown already in progress")
                return

            self._shutdown_initiated = True
            logger.info(
                f"Received {sig_name}, initiating graceful shutdown "
                f"(timeout: {GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS}s)"
            )
            if self.workflow_worker:
                asyncio.create_task(self._shutdown_worker())

        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: handle_signal("SIGTERM"))
            loop.add_signal_handler(signal.SIGINT, lambda: handle_signal("SIGINT"))
            logger.debug("Registered SIGTERM and SIGINT handlers")
        except (ValueError, RuntimeError) as e:
            logger.warning(f"Could not set up signal handlers: {e}")

    async def _shutdown_worker(self) -> None:
        """Shutdown the worker gracefully.

        Calls worker.shutdown() which:
        1. Stops polling for new tasks
        2. Waits for in-flight activities (up to graceful_shutdown_timeout)
        3. Returns when done or timeout reached
        """
        if not self.workflow_worker:
            return

        try:
            logger.info("Stopping polling, waiting for in-flight activities...")
            await self.workflow_worker.shutdown()
            logger.info("Worker shutdown complete")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    def __init__(
        self,
        workflow_client: Optional[WorkflowClient] = None,
        workflow_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = [],
        workflow_classes: Sequence[ClassType] = [],
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
        activity_executor: Optional[ThreadPoolExecutor] = None,
        credential_declarations: Optional[List["Credential"]] = None,
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
            credential_declarations: List of Credential declarations for
                automatic credential bootstrap. If provided, credentials will
                be automatically injected at workflow start.

        Returns:
            None

        Raises:
            TypeError: If workflow_activities contains non-callable items.
            ValueError: If passthrough_modules contains invalid module names.
        """
        self.workflow_client = workflow_client
        self.workflow_worker: Optional[TemporalWorker] = None
        self._shutdown_initiated = False
        self.workflow_activities = workflow_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = list(
            set(passthrough_modules + self.default_passthrough_modules)
        )
        self.max_concurrent_activities = max_concurrent_activities
        self.credential_declarations = credential_declarations

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

        Graceful Shutdown:
            When SIGTERM/SIGINT is received:
            1. Worker stops polling for new tasks
            2. In-flight activities complete within graceful_shutdown_timeout
            3. Worker exits early if activities finish, or at timeout
        """
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
                credential_declarations=self.credential_declarations,
            )
            self.workflow_worker = worker

            logger.info(
                f"Starting worker with task queue: {self.workflow_client.worker_task_queue}"
            )

            # Set up signal handlers and run worker
            self._setup_signal_handlers()

            await worker.run()

            logger.info("Worker stopped")

        except Exception as e:
            logger.error(f"Error starting worker: {e}")
            raise e
