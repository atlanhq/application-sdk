from typing import Any, Optional

from application_sdk.clients.utils import get_workflow_client
from application_sdk.handlers import HandlerInterface
from application_sdk.server import ServerInterface
from application_sdk.worker import Worker


class BaseApplication:
    """
    Generic application abstraction for orchestrating workflows, workers, and (optionally) servers.

    This class provides a standard way to set up and run workflows using Temporal, including workflow client,
    worker, and (optionally) FastAPI server setup. It is intended to be used directly for most simple applications,
    and can be subclassed for more specialized use cases.
    """

    def __init__(
        self,
        name: str,
        handler: Optional[HandlerInterface] = None,
        server: Optional[ServerInterface] = None,
    ):
        self.application_name = name
        self.handler = handler
        self.server = server
        self.workflow_client = get_workflow_client(application_name=name)
        self.worker = None
        self.application = None  # For server, if needed

    async def setup_workflow(self, workflow_classes, activities_class):
        """
        Set up the workflow client and start the worker for the application.
        """
        await self.workflow_client.load()
        activities = activities_class()
        workflow_class = workflow_classes[0]
        self.worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_class.get_activities(activities),
        )

    async def start_workflow(self, workflow_args, workflow_class) -> Any:
        """
        Start a new workflow execution.
        """
        if self.workflow_client is None:
            raise ValueError("Workflow client not initialized")
        return await self.workflow_client.start_workflow(workflow_args, workflow_class)

    async def start_worker(self, daemon: bool = True):
        """
        Start the worker for the application.
        """
        if self.worker is None:
            raise ValueError("Worker not initialized")
        await self.worker.start(daemon=daemon)

    async def setup_server(self, workflow_class):
        """
        Optionally set up a server for the application. (No-op by default)
        """
        pass

    async def start_server(self):
        """
        Optionally start the server for the application. (No-op by default)
        """
        pass
