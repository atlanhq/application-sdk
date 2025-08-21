from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple, Type

from application_sdk.activities.metadata_extraction.base import (
    BaseMetadataExtractionActivities,
)
from application_sdk.application import BaseApplication
from application_sdk.clients.base import BaseClient
from application_sdk.clients.utils import get_workflow_client
from application_sdk.constants import MAX_CONCURRENT_ACTIVITIES
from application_sdk.handlers.base import BaseHandler
from application_sdk.observability.decorators.observability_decorator import (
    observability,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import get_metrics
from application_sdk.observability.traces_adaptor import get_traces
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
from application_sdk.transformers import TransformerInterface
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)
metrics = get_metrics()
traces = get_traces()


class BaseMetadataExtractionApplication(BaseApplication):
    """
    Base application abstraction for non-SQL metadata extraction workflows.

    This class provides a standard way to set up and run non-SQL metadata extraction
    workflows using Temporal, including workflow client, worker, and FastAPI server setup.
    It is intended to be subclassed or used directly for most non-SQL-based metadata
    extraction applications.
    """

    def __init__(
        self,
        name: str,
        client_class: Optional[Type[BaseClient]] = None,
        handler_class: Optional[Type[BaseHandler]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
        server: Optional[APIServer] = None,
    ):
        """
        Initialize the base metadata extraction application.

        Args:
            name (str): Name of the application (used for workflow client and server identification).
            client_class (Type[BaseClient]): Base client class for source connectivity.
            handler_class (Optional[Type[BaseHandler]]): Handler class for preflight checks and metadata logic. Defaults to BaseHandler.
            transformer_class (Optional[Type[TransformerInterface]]): Transformer class for mapping to Atlas entities. Defaults to None.
            server (Optional[APIServer]): Server for the application. Defaults to None.
        """
        self.application_name = name
        self.transformer_class = transformer_class
        self.client_class = client_class or BaseClient
        self.handler_class = handler_class or BaseHandler

        # setup application server. serves the UI, and handles the various triggers
        self.server = server

        self.worker = None

        # setup workflow client for worker and application server
        self.workflow_client = get_workflow_client(
            application_name=self.application_name
        )

    @observability(logger=logger, metrics=metrics, traces=traces)
    async def setup_workflow(
        self,
        workflow_and_activities_classes: List[
            Tuple[Type[WorkflowInterface], Type[BaseMetadataExtractionActivities]]
        ],
        passthrough_modules: List[str] = [],
        activity_executor: Optional[ThreadPoolExecutor] = None,
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
    ):
        """
        Set up the workflow client and start the worker for base metadata extraction.

        Args:
            workflow_and_activities_classes (List[Tuple[Type[WorkflowInterface], Type[BaseMetadataExtractionActivities]]]): List of workflow and activities classes to register. Defaults to [(WorkflowInterface, BaseMetadataExtractionActivities)].
            passthrough_modules (List[str]): The modules to pass through to the worker. Defaults to [].
            activity_executor (ThreadPoolExecutor | None): Executor for running activities. Defaults to None.
            max_concurrent_activities (Optional[int]): Maximum number of concurrent activities. Defaults to MAX_CONCURRENT_ACTIVITIES.
        """
        # load the workflow client
        await self.workflow_client.load()

        workflow_classes = [
            workflow_class for workflow_class, _ in workflow_and_activities_classes
        ]

        # Collect all activities from all workflow classes
        workflow_activities = []
        for workflow_class, activities_class in workflow_and_activities_classes:
            workflow_activities.extend(
                workflow_class.get_activities(
                    activities_class(
                        client_class=self.client_class,
                        handler_class=self.handler_class,
                        transformer_class=self.transformer_class,
                    )
                )
            )

        self.worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_activities,
            passthrough_modules=passthrough_modules,
            activity_executor=activity_executor,
            max_concurrent_activities=max_concurrent_activities,
        )

    @observability(logger=logger, metrics=metrics, traces=traces)
    async def setup_server(
        self,
        workflow_class: Type[WorkflowInterface],
    ) -> None:
        """
        Set up the FastAPI server for the base metadata extraction application.

        Args:
            workflow_class (Type[WorkflowInterface]): Workflow class to register with the server. Users must provide their own workflow implementation.
        """
        if self.workflow_client is None:
            await self.workflow_client.load()

        # setup application server. serves the UI, and handles the various triggers
        self.server = APIServer(
            handler=self.handler_class(client=self.client_class()),
            workflow_client=self.workflow_client,
        )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.server.register_workflow(
            workflow_class=workflow_class,
            triggers=[HttpWorkflowTrigger()],
        )
