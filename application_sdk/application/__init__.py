from typing import Any, List, Optional

from application_sdk.clients.utils import get_workflow_client
from application_sdk.server import ServerInterface
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
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
        server: Optional[ServerInterface] = None,
    ):
        """
        Initialize the application.

        Args:
            name (str): The name of the application.
            server (ServerInterface): The server class for the application.
        """
        self.application_name = name
        self.server = server

        # setup application server. serves the UI, and handles the various triggers
        self.application = None

        self.worker = None
        self.application = None  # For server, if needed

        self.workflow_client = get_workflow_client(application_name=name)

    async def setup_workflow(
        self,
        workflow_classes,
        activities_class,
        passthrough_modules: List[str] = [],
    ):
        """
        Set up the workflow client and start the worker for the application.

        Args:
            workflow_classes (list): The workflow classes for the application.
            activities_class (ActivitiesInterface): The activities class for the application.
            passthrough_modules (list): The modules to pass through to the worker.
        """
        await self.workflow_client.load()
        activities = activities_class()
        workflow_class = workflow_classes[0]
        self.worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_class.get_activities(activities),
            passthrough_modules=passthrough_modules,
        )

    async def start_workflow(self, workflow_args, workflow_class) -> Any:
        """
        Start a new workflow execution.

        Args:
            workflow_args (dict): The arguments for the workflow.
            workflow_class (WorkflowInterface): The workflow class for the application.

        Returns:
            Any: The result of the workflow execution.
        """
        if self.workflow_client is None:
            raise ValueError("Workflow client not initialized")
        return await self.workflow_client.start_workflow(workflow_args, workflow_class)

    async def start_worker(self, daemon: bool = True):
        """
        Start the worker for the application.

        Args:
            daemon (bool): Whether to run the worker in daemon mode.
        """
        if self.worker is None:
            raise ValueError("Worker not initialized")
        await self.worker.start(daemon=daemon)

    async def setup_server(self, workflow_class):
        """
        Optionally set up a server for the application. (No-op by default)
        """
        if self.workflow_client is None:
            await self.workflow_client.load()

        # setup application server. serves the UI, and handles the various triggers
        self.application = APIServer(
            workflow_client=self.workflow_client,
        )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.application.register_workflow(
            workflow_class=workflow_class,
            triggers=[HttpWorkflowTrigger()],
        )

    async def start_server(self):
        """
        Start the FastAPI server for the application.

        Raises:
            ValueError: If the application server is not initialized.
        """
        if self.application is None:
            raise ValueError("Application server not initialized")

        await self.application.start()
