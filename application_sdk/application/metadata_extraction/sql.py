from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Type

from application_sdk.application import BaseApplication
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.utils import get_workflow_client
from application_sdk.constants import MAX_CONCURRENT_ACTIVITIES
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.observability.decorators.observability_decorator import (
    observability,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import get_metrics
from application_sdk.observability.traces_adaptor import get_traces
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
from application_sdk.transformers.query import QueryBasedTransformer
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
    BaseSQLMetadataExtractionWorkflow,
)

logger = get_logger(__name__)
metrics = get_metrics()
traces = get_traces()


class BaseSQLMetadataExtractionApplication(BaseApplication):
    """
    Base application abstraction for SQL metadata extraction workflows.

    This class provides a standard way to set up and run SQL metadata extraction workflows using Temporal,
    including workflow client, worker, and FastAPI server setup. It is intended to be subclassed or used
    directly for most SQL-based metadata extraction applications.
    """

    def __init__(
        self,
        name: str,
        client_class: Optional[Type[BaseSQLClient]] = None,
        handler_class: Optional[Type[BaseSQLHandler]] = None,
        transformer_class: Optional[Type[QueryBasedTransformer]] = None,
        server: Optional[APIServer] = None,
    ):
        """
        Initialize the SQL metadata extraction application.

        Args:
            name (str): Name of the application (used for workflow client and server identification).
            client_class (Type[BaseSQLClient]): SQL client class for source connectivity.
            handler_class (Optional[Type[HandlerInterface]]): Handler class for preflight checks and metadata logic. Defaults to BaseSQLHandler.
            transformer_class (Optional[Type[TransformerInterface]]): Transformer class for mapping to Atlas entities. Defaults to QueryBasedTransformer.
            server (Optional[APIServer]): Server for the application. Defaults to None.
        """
        self.application_name = name
        self.transformer_class = transformer_class or QueryBasedTransformer
        self.client_class = client_class or BaseSQLClient
        self.handler_class = handler_class or BaseSQLHandler

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
            Tuple[
                Type[BaseSQLMetadataExtractionWorkflow],
                Type[BaseSQLMetadataExtractionActivities],
            ]
        ] = [(BaseSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)],
        passthrough_modules: List[str] = [],
        activity_executor: Optional[ThreadPoolExecutor] = None,
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
    ):
        """
        Set up the workflow client and start the worker for SQL metadata extraction.

        Args:
            workflow_and_activities_classes (List[Tuple[Type[BaseSQLMetadataExtractionWorkflow], Type[BaseSQLMetadataExtractionActivities]]]): List of workflow and activities classes to register. Defaults to [(BaseSQLMetadataExtractionWorkflow, BaseSQLMetadataExtractionActivities)].
            worker_daemon_mode (bool): Whether to run the worker in daemon mode. Defaults to True.
            passthrough_modules (List[str]): The modules to pass through to the worker. Defaults to None.
            activity_executor (ThreadPoolExecutor | None): Executor for running activities.
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
                        sql_client_class=self.client_class,
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
    async def start_workflow(
        self,
        workflow_args: Dict[str, Any],
        workflow_class: Type = BaseSQLMetadataExtractionWorkflow,
    ) -> Any:
        """
        Start a new workflow execution for SQL metadata extraction.

        Args:
            workflow_args (Dict[str, Any]): Arguments to pass to the workflow (credentials, connection, metadata, etc.).
            workflow_class (Type): Workflow class to use. Defaults to BaseSQLMetadataExtractionWorkflow.

        Returns:
            Any: The workflow response from the workflow client.

        Raises:
            ValueError: If the workflow client is not initialized.
        """
        if self.workflow_client is None:
            raise ValueError("Workflow client not initialized")

        workflow_response = await self.workflow_client.start_workflow(
            workflow_args, workflow_class
        )
        return workflow_response

    @observability(logger=logger, metrics=metrics, traces=traces)
    async def start_worker(self, daemon: bool = True):
        """
        Start the worker for the SQL metadata extraction application.
        """
        if self.worker is None:
            raise ValueError("Worker not initialized")
        await self.worker.start(daemon=daemon)

    @observability(logger=logger, metrics=metrics, traces=traces)
    async def setup_server(
        self,
        workflow_class: Type[
            BaseSQLMetadataExtractionWorkflow
        ] = BaseSQLMetadataExtractionWorkflow,
    ) -> Any:
        """
        Set up the FastAPI server for the SQL metadata extraction application.

        Args:
            workflow_class (Type): Workflow class to register with the server. Defaults to BaseSQLMetadataExtractionWorkflow.

        Returns:
            Any: None
        """
        if self.workflow_client is None:
            await self.workflow_client.load()

        # setup application server. serves the UI, and handles the various triggers
        self.server = APIServer(
            handler=self.handler_class(sql_client=self.client_class()),
            workflow_client=self.workflow_client,
        )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.server.register_workflow(
            workflow_class=workflow_class,
            triggers=[HttpWorkflowTrigger()],
        )
