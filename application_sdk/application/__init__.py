from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Type

from application_sdk.activities import ActivitiesInterface
from application_sdk.clients.utils import get_workflow_client
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.eventstore import EventRegistration
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
        application_manifest: Optional[dict] = None,
    ):
        """
        Initialize the application.

        Args:
            name (str): The name of the application.
            server (ServerInterface): The server class for the application.
        """
        self.application_name = name

        # setup application server. serves the UI, and handles the various triggers
        self.server = server

        self.worker = None

        self.workflow_client = get_workflow_client(application_name=name)

        self.application_manifest: Dict[str, Any] = application_manifest
        self.bootstrap_event_registration()

    def bootstrap_event_registration(self):
        self.event_subscriptions = {}
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
            workflow_activities.extend(
                workflow_class.get_activities(activities_class())
            )

        self.worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_activities,
            passthrough_modules=passthrough_modules,
            activity_executor=activity_executor,
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

    async def setup_server(
        self,
        workflow_class,
        ui_enabled: bool = True,
    ):
        """
        Optionally set up a server for the application. (No-op by default)
        """
        if self.workflow_client is None:
            await self.workflow_client.load()

        # Overrides the application server. serves the UI, and handles the various triggers
        self.server = APIServer(
            workflow_client=self.workflow_client,
            ui_enabled=ui_enabled,
        )

        if self.event_subscriptions:
            for event_trigger in self.event_subscriptions.values():
                if event_trigger.workflow_class is None:
                    raise ValueError(
                        f"Workflow class not set for event trigger {event_trigger.event_id}"
                    )

                self.server.register_workflow(
                    workflow_class=event_trigger.workflow_class,
                    triggers=[event_trigger],
                )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.server.register_workflow(
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
