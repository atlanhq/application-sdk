from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.handlers import HandlerInterface
from application_sdk.server import ServerInterface


class Application(ABC):
    """
    Abstract base class for all application implementations.

    This class defines the core interface for all application types,
    providing a standardized way to set up and manage workflows and servers.
    """

    def __init__(
        self,
        name: str,
        handler: Optional[HandlerInterface] = None,
        server: Optional[ServerInterface] = None,
    ):
        pass

    @abstractmethod
    async def setup_workflow(self, workflow_classes, activities_class):
        pass

    @abstractmethod
    async def start_workflow(self, workflow_args, workflow_class):
        pass

    @abstractmethod
    async def start_worker(self, daemon: bool = True):
        pass

    @abstractmethod
    async def setup_server(self, workflow_class):
        pass

    @abstractmethod
    async def start_server(self):
        pass
