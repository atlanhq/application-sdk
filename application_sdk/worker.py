import asyncio
import logging
import threading
from typing import Any, List, Sequence

from temporalio.types import CallableType

from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class Worker:
    def __init__(
        self,
        temporal_client: TemporalClient | None = None,
        temporal_activities: Sequence[CallableType] = [],
        passthrough_modules: List[str] = ["application_sdk", "os"],
        workflow_classes: List[Any] = [],
    ):
        self.temporal_client = temporal_client
        self.temporal_worker = None
        self.temporal_activities = temporal_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = passthrough_modules

    async def start(self, daemon: bool = False, *args: Any, **kwargs: Any) -> None:
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
