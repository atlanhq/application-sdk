"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution.
"""

import asyncio
import threading
import multiprocessing
from typing import Any, List, Optional, Sequence

import uvloop
from temporalio.types import CallableType

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.common.logger_adaptors import get_logger


import threading
from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import ThreadPoolExecutor
from typing import Any, List, Sequence

import uvloop
from temporalio.types import CallableType
from temporalio.worker import SharedStateManager

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import get_safe_num_threads, get_actual_cpu_count

logger = get_logger(__name__)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class Worker:
    """Worker class for managing Temporal workflow workers.

    This class handles the initialization and execution of Temporal workers,
    including their activities, workflows, and module configurations.

    Attributes:
        workflow_client (WorkflowClient | None): Client for interacting with Temporal.
        workflow_worker: The Temporal worker instance.
        workflow_activities (Sequence[CallableType]): List of activity functions.
        workflow_classes (List[Any]): List of workflow classes.
        passthrough_modules (List[str]): List of module names to pass through.
        max_concurrent_activities (int | None): Maximum number of concurrent activities.
    """

    def __init__(
        self,
        workflow_client: WorkflowClient | None = None,
        workflow_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = ["application_sdk", "pandas", "os", "app"],
        workflow_classes: List[Any] = [],
        max_concurrent_activities: Optional[int] = None,
        is_sync_activities: bool = False,
    ):
        """Initialize the Worker.

        Args:
            workflow_client (WorkflowClient | None, optional): Client for interacting
                with Temporal. Defaults to None.
            workflow_activities (Sequence[CallableType], optional): List of activity
                functions. Defaults to empty list.
            passthrough_modules (List[str], optional): List of module names to pass
                through. Defaults to ["application_sdk", "os"].
            workflow_classes (List[Any], optional): List of workflow classes.
                Defaults to empty list.
        """
        self.workflow_client = workflow_client
        self.workflow_worker = None
        self.workflow_activities = workflow_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = passthrough_modules
        self.max_concurrent_activities = max_concurrent_activities


        if is_sync_activities:
            self.max_concurrent_activities = get_actual_cpu_count()

        activity_executor = ThreadPoolExecutor(get_safe_num_threads())
        kwargs = {}
        if is_sync_activities:
            # https://github.com/temporalio/samples-python/blob/main/hello/hello_activity_multiprocess.py
            activity_executor = ProcessPoolExecutor(get_actual_cpu_count())
            kwargs = {
                "shared_state_manager": SharedStateManager.create_from_multiprocessing(multiprocessing.Manager())
            }

        self.worker = self.workflow_client.create_worker(
            activities=self.workflow_activities,
            workflow_classes=self.workflow_classes,
            passthrough_modules=self.passthrough_modules,
            max_concurrent_activities=self.max_concurrent_activities,
            activity_executor=activity_executor,
            **kwargs,
        )

    async def start(self, daemon: bool = True, *args: Any, **kwargs: Any) -> None:
        """Start the Temporal worker.

        This method starts the worker either in the current thread or as a daemon
        thread based on the daemon parameter.

        Args:
            daemon (bool, optional): Whether to run the worker in a daemon thread.
                Defaults to False.
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.

        Raises:
            ValueError: If workflow_client is not set.

        Note:
            When running as a daemon, the worker runs in a separate thread and
            does not block the main thread.
        """
        if daemon:
            worker_thread = threading.Thread(
                target=lambda: asyncio.run(self.worker.run()), daemon=True
            )
            worker_thread.start()
            logger.info(f"Worker started in daemon thread on task queue: {self.workflow_client.worker_task_queue}")
            return

        if not self.workflow_client:
            raise ValueError("Workflow client is not set")

        try:
            logger.info(
                f"Starting worker with task queue: {self.workflow_client.worker_task_queue}"
            )
            await self.worker.run()
        except Exception as e:
            logger.error(f"Error running worker: {e}")
            raise e
