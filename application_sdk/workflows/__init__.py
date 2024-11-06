import asyncio
from abc import ABC
from typing import Optional

from application_sdk.logging import get_logger
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
    WorkflowWorkerControllerInterface,
)
from application_sdk.workflows.resources import TemporalResource

logger = get_logger(__name__)


class WorkflowBuilderInterface(ABC):
    """
    Base class for workflow builder interfaces

    This class provides a default implementation for the workflow builder, with hooks
    for subclasses to customize specific behaviors.

    Attributes:
        auth_controller: The auth interface.
        metadata_controller: The metadata interface.
        preflight_check_controller: The preflight check interface.
        worker_controller: The worker interface.
    """

    def __init__(
        self,
        # Resources
        temporal_resource: TemporalResource,
        # Controllers
        auth_controller: Optional[WorkflowAuthControllerInterface] = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface] = None,
        preflight_check_controller: Optional[
            WorkflowPreflightCheckControllerInterface
        ] = None,
        worker_controller: Optional[WorkflowWorkerControllerInterface] = None,
    ):
        self.temporal_resource = temporal_resource

        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller
        self.worker_controller = worker_controller

    def start_worker(self):
        if not self.worker_controller:
            raise NotImplementedError("Worker interface not implemented")
        asyncio.run(self.worker_controller.start_worker())

    async def load_resources(self):
        await self.temporal_resource.load()
