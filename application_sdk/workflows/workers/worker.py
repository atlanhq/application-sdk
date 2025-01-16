import logging
from typing import Any, List

from temporalio.types import CallableType

from application_sdk.clients.temporal_client import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class WorkflowWorker:
    """
    Base class for workflow workers

    This class provides a default implementation for the workflow, with hooks
    for subclasses to customize specific behaviors.
    """

    def __init__(
        self,
        temporal_client: TemporalClient | None = None,
        temporal_activities: List[CallableType] | None = [],
        passthrough_modules: List[str] | None = ["application_sdk", "os"],
        workflow_classes: List[Any] | None = [],
    ):
        self.temporal_client = temporal_client
        self.temporal_worker = None
        self.temporal_activities = temporal_activities
        self.workflow_classes = workflow_classes
        self.passthrough_modules = passthrough_modules

    async def start(self, *args: Any, **kwargs: Any) -> None:
        if not self.temporal_client:
            raise ValueError("Temporal client is not set")

        worker = self.temporal_client.create_worker(
            activities=self.temporal_activities,
            workflow_classes=self.workflow_classes,
            passthrough_modules=self.passthrough_modules,
        )

        logger.info(
            f"Starting worker with task queue: {self.temporal_client.worker_task_queue}"
        )
        await worker.run()
