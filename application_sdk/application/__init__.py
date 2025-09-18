from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Type

from application_sdk.activities import ActivitiesInterface
from application_sdk.clients.base import BaseClient
from application_sdk.clients.utils import get_workflow_client
from application_sdk.constants import ENABLE_MCP
from application_sdk.events.models import EventRegistration
from application_sdk.handlers.base import BaseHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server import ServerInterface
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
from application_sdk.server.fastapi.models import EventWorkflowTrigger
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


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
        application_manifest: Optional[Dict[str, Any]] = None,
        client_class: Optional[Type[BaseClient]] = None,
        handler_class: Optional[Type[BaseHandler]] = None,
    ):
        """
        Initialize the application.

        Args:
            name (str): The name of the application.
            server (ServerInterface): The server class for the application.
            application_manifest (Optional[Dict[str, Any]]): Application manifest configuration.
            client_class (Optional[Type[BaseClient]]): Client class for the application.
            handler_class (Optional[Type[BaseHandler]]): Handler class for the application.
        """
        self.application_name = name

        # setup application server. serves the UI, and handles the various triggers
        self.server = server

        self.worker = None

        self.workflow_client = get_workflow_client(application_name=name)

        self.application_manifest: Optional[Dict[str, Any]] = application_manifest
        self.bootstrap_event_registration()

        self.client_class = client_class or BaseClient
        self.handler_class = handler_class or BaseHandler

        # MCP configuration
        self.mcp_server: Optional["MCPServer"] = None
        if ENABLE_MCP:
            from application_sdk.server.mcp import MCPServer

            self.mcp_server = MCPServer(application_name=name)

    def bootstrap_event_registration(self):
        self.event_subscriptions: Dict[str, EventWorkflowTrigger] = {}
        if self.application_manifest is None:
            logger.warning("No application manifest found, skipping event registration")
            return

        event_registration = EventRegistration(
            **self.application_manifest.get("eventRegistration", {})
        )
        if not event_registration.consumes or len(event_registration.consumes) == 0:
            logger.warning(
                "No event registration found in the application manifest, skipping event registration"
            )
            return

        for consume in event_registration.consumes:
            logger.info(f"Setting up event registration for {consume}")
            event_trigger: EventWorkflowTrigger = EventWorkflowTrigger(
                event_type=consume.event_type,
                event_name=consume.event_name,
                event_filters=consume.filters,
                event_id=consume.event_id,
            )

            if event_trigger.event_id in self.event_subscriptions:
                raise ValueError(
                    f"Event {event_trigger.event_id} duplicate in the application manifest"
                )

            self.event_subscriptions[consume.event_id] = event_trigger

    def register_event_subscription(
        self, event_id: str, workflow_class: Type[WorkflowInterface]
    ):
        if self.event_subscriptions is None:
            raise ValueError("Event subscriptions not initialized")

        if event_id not in self.event_subscriptions:
            raise ValueError(
                f"Event {event_id} not initialized in the application manifest"
            )

        self.event_subscriptions[event_id].workflow_class = workflow_class

    async def setup_workflow(
        self,
        workflow_and_activities_classes: List[
            Tuple[Type[WorkflowInterface], Type[ActivitiesInterface]]
        ],
        passthrough_modules: List[str] = [],
        activity_executor: Optional[ThreadPoolExecutor] = None,
    ):
        """
        Set up the workflow client and start the worker for the application.

        Args:
            workflow_and_activities_classes (list): The workflow and activities classes for the application.
            passthrough_modules (list): The modules to pass through to the worker.
            activity_executor (ThreadPoolExecutor | None): Executor for running activities.
        """
        await self.workflow_client.load()

        workflow_classes = [
            workflow_class for workflow_class, _ in workflow_and_activities_classes
        ]
        workflow_activities = []
        for workflow_class, activities_class in workflow_and_activities_classes:
            workflow_activities.extend(  # type: ignore
                workflow_class.get_activities(activities_class())  # type: ignore
            )

        self.worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_activities,
            passthrough_modules=passthrough_modules,
            activity_executor=activity_executor,
        )

        # Register MCP tools if ENABLED_MCP is True and an MCP server is initialized
        if self.mcp_server:
            logger.info("Registering MCP tools from workflow and activities classes")
            await self.mcp_server.register_tools(  # type: ignore
                workflow_and_activities_classes=workflow_and_activities_classes
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
        return await self.workflow_client.start_workflow(workflow_args, workflow_class)  # type: ignore

    async def start_worker(self, daemon: bool = True):
        """
        Start the worker for the application.

        Args:
            daemon (bool): Whether to run the worker in daemon mode.
        """
        if self.worker is None:
            raise ValueError("Worker not initialized")
        await self.worker.start(daemon=daemon)

    async def setup_server(
        self,
        workflow_class: Type[WorkflowInterface],
        ui_enabled: bool = True,
        has_configmap: bool = False,
    ):
        """
        Set up FastAPI server and automatically mount MCP if enabled.

        Args:
            workflow_class (WorkflowInterface): The workflow class for the application.
            ui_enabled (bool): Whether to enable the UI.
            has_configmap (bool): Whether to enable the configmap.
        """
        if self.workflow_client is None:
            await self.workflow_client.load()

        mcp_http_app: Optional[Any] = None
        lifespan: Optional[Any] = None

        if self.mcp_server:
            try:
                mcp_http_app = await self.mcp_server.get_http_app()
                lifespan = mcp_http_app.lifespan
            except Exception as e:
                logger.warning(f"Failed to get MCP HTTP app: {e}")

        self.server = APIServer(
            lifespan=lifespan,
            workflow_client=self.workflow_client,
            ui_enabled=ui_enabled,
            handler=self.handler_class(client=self.client_class()),
            has_configmap=has_configmap,
        )

        # Mount MCP at root
        if mcp_http_app:
            try:
                self.server.app.mount("", mcp_http_app)  # Mount at root
            except Exception as e:
                logger.warning(f"Failed to mount MCP HTTP app: {e}")

        # Register event-based workflows if any
        if self.event_subscriptions:
            for event_trigger in self.event_subscriptions.values():
                if event_trigger.workflow_class is None:  # type: ignore
                    raise ValueError(
                        f"Workflow class not set for event trigger {event_trigger.event_id}"
                    )

                self.server.register_workflow(  # type: ignore
                    workflow_class=event_trigger.workflow_class,  # type: ignore
                    triggers=[event_trigger],
                )

        # Register the main workflow (HTTP POST /start endpoint)
        self.server.register_workflow(  # type: ignore
            workflow_class=workflow_class,
            triggers=[HttpWorkflowTrigger()],
        )

    async def start_server(self):
        """
        Start the FastAPI server for the application.

        Raises:
            ValueError: If the application server is not initialized.
        """
        if self.server is None:
            raise ValueError("Application server not initialized")

        await self.server.start()
