import logging
from abc import ABC, abstractmethod

from application_sdk.clients.temporal_client import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers import WorkflowHandlerInterface
from application_sdk.workflows.workflow import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class WorkflowBuilderInterface(ABC):
    """
    Base class for workflow builder interfaces

    This class provides a default implementation for the workflow builder, with hooks
    for subclasses to customize specific behaviors.

    Attributes:
        handler: The handler interface.
        worker_controller: The worker interface.
    """

    handler: WorkflowHandlerInterface
    temporal_client: TemporalClient

    async def load_clients(self):
        await self.temporal_client.load()

    @abstractmethod
    def build(self) -> WorkflowInterface:
        raise NotImplementedError("build method must be implemented")

    def set_handler(
        self, handler: WorkflowHandlerInterface
    ) -> "WorkflowBuilderInterface":
        self.handler = handler
        return self

    def set_temporal_client(
        self, temporal_client: TemporalClient
    ) -> "WorkflowBuilderInterface":
        self.temporal_client = temporal_client
        return self


class MinerBuilderInterface(ABC):
    """
    Base class for miner builder interfaces
    """

    temporal_client: TemporalClient
    handler: WorkflowHandlerInterface

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

    def set_handler(self, handler: WorkflowHandlerInterface) -> "MinerBuilderInterface":
        self.handler = handler
        return self
