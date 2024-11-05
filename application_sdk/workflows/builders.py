import asyncio
from abc import ABC

from application_sdk.logging import get_logger
from application_sdk.workflows.controllers import WorkflowWorkerControllerInterface
from application_sdk.workflows.resources import TemporalResource

logger = get_logger(__name__)


class WorkflowBuilderInterface(ABC):
    """
    Base class for workflow builder interfaces

    This class provides a default implementation for the workflow builder, with hooks
    for subclasses to customize specific behaviors.

    Attributes:
        worker_interface: The worker interface.
    """

    worker_controller: WorkflowWorkerControllerInterface

    def __init__(
        self,
        # Resources
        temporal_resource: TemporalResource,
        # Interfaces
        worker_controller: WorkflowWorkerControllerInterface,
    ):
        worker_controller = worker_controller or WorkflowWorkerControllerInterface(
            temporal_resource
        )
        self.with_worker_controller(worker_controller)

        self.temporal_resource = temporal_resource

    def with_worker_controller(self, worker_controller):
        self.worker_controller = worker_controller
        return self

    def start_worker(self):
        if not self.worker_controller:
            raise NotImplementedError("Worker controller not implemented")
        asyncio.run(self.worker_controller.start_worker())

    async def load_resources(self):
        await self.temporal_resource.load()
