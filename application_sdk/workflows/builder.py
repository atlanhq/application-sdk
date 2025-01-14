import logging
from abc import ABC, abstractmethod

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.clients.temporal_resource import TemporalResource
from application_sdk.workflows.workflow import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


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

    temporal_resource: TemporalResource

    async def load_resources(self):
        await self.temporal_resource.load()

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

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "WorkflowBuilderInterface":
        self.temporal_resource = temporal_resource
        return self


class MinerBuilderInterface(ABC):
    temporal_resource: TemporalResource

    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    async def load_resources(self):
        await self.temporal_resource.load()

    @abstractmethod
    def build(self) -> WorkflowInterface:
        raise NotImplementedError("build method must be implemented")

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "MinerBuilderInterface":
        self.temporal_resource = temporal_resource
        return self

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "MinerBuilderInterface":
        self.preflight_check_controller = preflight_check_controller
        return self
