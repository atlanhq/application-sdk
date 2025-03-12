"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution.
"""

import asyncio
import multiprocessing
import threading
from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import ThreadPoolExecutor
from typing import Any, List, Sequence

import uvloop
from temporalio.types import CallableType
from temporalio.worker import SharedStateManager

from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.utils import get_safe_num_threads, get_actual_cpu_count

logger = get_logger(__name__)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class Worker:
    """Worker class for managing Temporal workflow workers.

    This class handles the initialization and execution of Temporal workers,
    including their activities, workflows, and module configurations.

    Attributes:
        temporal_client (TemporalClient | None): Client for interacting with Temporal.
        worker: The Temporal worker instance.
        temporal_activities (Sequence[CallableType]): List of activity functions.
        workflow_classes (List[Any]): List of workflow classes.
        passthrough_modules (List[str]): List of module names to pass through.
        max_concurrent_activities (int | None): Maximum number of concurrent activities.
    """

    def __init__(
        self,
        temporal_client: TemporalClient,
        temporal_activities: Sequence[CallableType] | None  = None,
        passthrough_modules: List[str] | None = None,
        workflow_classes: List[Any] | None = None,
        max_concurrent_activities: int = 2,
        is_sync_activities: bool = False,
    ):
        """Initialize the Worker.

        Args:
            temporal_client (TemporalClient): Client for interacting
                with Temporal.
            temporal_activities (Sequence[CallableType]): List of activity
                functions. Defaults to empty list.
            passthrough_modules (List[str]): List of module names to pass
                through. Defaults to ["application_sdk", "os"].
            workflow_classes (List[Any]): List of workflow classes.
                Defaults to empty list.
            max_concurrent_activities (int): Maximum number of concurrent
                activities. Defaults to 2 or the number of CPUs for synchronous activities
            is_sync_activities (bool): Whether activities run by the worker are synchronous.
        """
        if workflow_classes is None:
            workflow_classes = []
        if temporal_activities is None:
            temporal_activities = []
        if passthrough_modules is None:
            passthrough_modules = ["application_sdk", "os"]

        self.temporal_client = temporal_client
        self.temporal_activities = temporal_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = passthrough_modules
        self.is_sync_activities = is_sync_activities

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

        self.worker = self.temporal_client.create_worker(
            activities=self.temporal_activities,
            workflow_classes=self.workflow_classes,
            passthrough_modules=self.passthrough_modules,
            max_concurrent_activities=self.max_concurrent_activities,
            activity_executor=activity_executor,
            **kwargs,
        )


    async def start(self, daemon: bool = False) -> None:
        """Start the Temporal worker.

        This method starts the worker either in the current thread or as a daemon
        thread based on the daemon parameter

        Args:
            daemon (bool, optional): Whether to run the worker in a daemon thread.
                Defaults to False.

        Note:
            When running as a daemon, the worker runs in a separate thread and
            does not block the main thread.
        """
        if daemon:
            worker_thread = threading.Thread(
                target=lambda: asyncio.run(self.worker.run()), daemon=True
            )
            worker_thread.start()
            return

        try:
            logger.info(f"Starting Temporal worker on queue {self.worker.task_queue}")
            await self.worker.run()
        except Exception as e:
            logger.error(f"Error running worker: {e}")
            raise e
