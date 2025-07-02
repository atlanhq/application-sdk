"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution.
"""

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Sequence

from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker as TemporalWorker

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import MAX_CONCURRENT_ACTIVITIES
from application_sdk.observability.logger_adaptor import get_logger

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

    def __init__(
        self,
        workflow_client: Optional[WorkflowClient] = None,
        workflow_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = [],
        workflow_classes: Sequence[ClassType] = [],
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
        activity_executor: Optional[ThreadPoolExecutor] = None,
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

        self.activity_executor = activity_executor or ThreadPoolExecutor(
            max_workers=max_concurrent_activities or 5,
            thread_name_prefix="activity-pool-",
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

        Note:
            When running as a daemon, the worker runs in a separate thread and
            does not block the main thread.
        """
        if daemon:
            worker_thread = threading.Thread(
                target=lambda: asyncio.run(self.start(daemon=False)), daemon=True
            )
            worker_thread.start()
            return

        if not self.workflow_client:
            raise ValueError("Workflow client is not set")

        try:
            worker = self.workflow_client.create_worker(
                activities=self.workflow_activities,
                workflow_classes=self.workflow_classes,
                passthrough_modules=self.passthrough_modules,
                max_concurrent_activities=self.max_concurrent_activities,
                activity_executor=self.activity_executor,
            )

            logger.info(
                f"Starting worker with task queue: {self.workflow_client.worker_task_queue}"
            )
            await worker.run()
        except Exception as e:
            logger.error(f"Error starting worker: {e}")
            raise e
