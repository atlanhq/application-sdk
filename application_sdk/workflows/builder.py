import logging
from abc import ABC, abstractmethod

from application_sdk.clients.temporal_client import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
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

    temporal_client: TemporalClient

    async def load_clients(self):
        await self.temporal_client.load()

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

    def set_temporal_client(
        self, temporal_client: TemporalClient
    ) -> "WorkflowBuilderInterface":
        self.temporal_client = temporal_client
        return self


class MinerBuilderInterface(ABC):
    temporal_client: TemporalClient

    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    async def load_clients(self):
        await self.temporal_client.load()

    @abstractmethod
    def build(self) -> WorkflowInterface:
        raise NotImplementedError("build method must be implemented")

    def set_temporal_client(
        self, temporal_client: TemporalClient
    ) -> "MinerBuilderInterface":
        self.temporal_client = temporal_client
        return self

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "MinerBuilderInterface":
        self.preflight_check_controller = preflight_check_controller
        return self
