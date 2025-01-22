"""Worker module for managing Temporal workers.

This module provides the Worker class for managing Temporal workflow workers,
including their initialization, configuration, and execution.
"""

import asyncio
import logging
import threading
from typing import Any, List, Sequence

from temporalio.types import CallableType

from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Worker:
    """Worker class for managing Temporal workflow workers.

    This class handles the initialization and execution of Temporal workers,
    including their activities, workflows, and module configurations.

    Attributes:
        temporal_client (TemporalClient | None): Client for interacting with Temporal.
        temporal_worker: The Temporal worker instance.
        temporal_activities (Sequence[CallableType]): List of activity functions.
        workflow_classes (List[Any]): List of workflow classes.
        passthrough_modules (List[str]): List of module names to pass through.
    """

    def __init__(
        self,
        temporal_client: TemporalClient | None = None,
        temporal_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = ["application_sdk", "os"],
        workflow_classes: List[Any] = [],
    ):
        """Initialize the Worker.

        Args:
            temporal_client (TemporalClient | None, optional): Client for interacting
                with Temporal. Defaults to None.
            temporal_activities (Sequence[CallableType], optional): List of activity
                functions. Defaults to empty list.
            passthrough_modules (List[str], optional): List of module names to pass
                through. Defaults to ["application_sdk", "os"].
            workflow_classes (List[Any], optional): List of workflow classes.
                Defaults to empty list.
        """
        self.temporal_client = temporal_client
        self.temporal_worker = None
        self.temporal_activities = temporal_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = passthrough_modules

    async def start(self, daemon: bool = False, *args: Any, **kwargs: Any) -> None:
        """Start the Temporal worker.

        This method starts the worker either in the current thread or as a daemon
        thread based on the daemon parameter.

        Args:
            daemon (bool, optional): Whether to run the worker in a daemon thread.
                Defaults to False.
            *args: Additional positional arguments.
            **kwargs: Additional keyword arguments.

        Raises:
            ValueError: If temporal_client is not set.

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

        if not self.temporal_client:
            raise ValueError("Temporal client is not set")

        try:
            worker = self.temporal_client.create_worker(
                activities=self.temporal_activities,
                workflow_classes=self.workflow_classes,
                passthrough_modules=self.passthrough_modules,
            )

            logger.info(
                f"Starting worker with task queue: {self.temporal_client.worker_task_queue}"
            )
            await worker.run()
        except Exception as e:
            logger.error(f"Error starting worker: {e}")
            raise e
