from abc import ABC, abstractmethod

from application_sdk.logging import get_logger
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface

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

    preflight_check_controller: WorkflowPreflightCheckControllerInterface
    metadata_controller: WorkflowMetadataControllerInterface
    auth_controller: WorkflowAuthControllerInterface

    async def load_resources(self):
        pass

    @abstractmethod
    def build(self) -> WorkflowInterface:
        raise NotImplementedError("build method must be implemented")

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "WorkflowBuilderInterface":
        self.preflight_check_controller = preflight_check_controller
        return self

    def set_metadata_controller(
        self, metadata_controller: WorkflowMetadataControllerInterface
    ) -> "WorkflowBuilderInterface":
        self.metadata_controller = metadata_controller
        return self

    def set_auth_controller(
        self, auth_controller: WorkflowAuthControllerInterface
    ) -> "WorkflowBuilderInterface":
        self.auth_controller = auth_controller
        return self
