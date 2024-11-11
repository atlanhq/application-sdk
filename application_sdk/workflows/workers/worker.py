import logging
from typing import Any, Callable, List

from application_sdk.workflows.resources.temporal_resource import TemporalResource

logger = logging.getLogger(__name__)


class WorkflowWorker:
    """
    Base class for workflow workers

    This class provides a default implementation for the workflow, with hooks
    for subclasses to customize specific behaviors.
    """

    temporal_resource: TemporalResource | None = None
    temporal_activities: List[Callable] | None = None

    def __init__(
        self,
        temporal_resource: TemporalResource | None = None,
        temporal_activities: List[Callable] | None = None,
        workflow_class: Any | None = None,
    ):
        self.temporal_resource = temporal_resource
        self.temporal_worker = None
        self.temporal_activities = temporal_activities
        self.workflow_class = workflow_class

    async def start(self, *args: Any, **kwargs: Any) -> None:
        if not self.temporal_resource:
            raise ValueError("Temporal resource is not set")

        worker = self.temporal_resource.create_worker(
            activities=self.temporal_activities,
            workflow_class=self.workflow_class,
        )

        logger.info(
            f"Starting worker with task queue: {self.temporal_resource.worker_task_queue}"
        )
        await worker.run()
