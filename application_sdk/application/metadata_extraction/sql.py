from typing import Any, Dict, List, Optional, Type

from application_sdk.clients.utils import get_workflow_client
from application_sdk.clients import ClientInterface
from application_sdk.handlers import HandlerInterface
from application_sdk.transformers import TransformerInterface
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.server.fastapi import HttpWorkflowTrigger
from application_sdk.server.fastapi import Application

class BaseSQLMetadataExtractionApplication:
    """
    Base application abstraction for SQL metadata extraction workflows.

    This class provides a standard way to set up and run SQL metadata extraction workflows using Temporal,
    including workflow client, worker, and FastAPI server setup. It is intended to be subclassed or used
    directly for most SQL-based metadata extraction applications.
    """

    def __init__(
        self,
        name: str,
        sql_client_class: Type[BaseSQLClient],
        handler_class: Optional[Type[HandlerInterface]] = None,
        transformer_class: Optional[Type[TransformerInterface]] = None,
    ):
        """
        Initialize the SQL metadata extraction application.

        Args:
            name (str): Name of the application (used for workflow client and server identification).
            sql_client_class (Type[BaseSQLClient]): SQL client class for source connectivity.
            handler_class (Optional[Type[HandlerInterface]]): Handler class for preflight checks and metadata logic. Defaults to BaseSQLHandler.
            transformer_class (Optional[Type[TransformerInterface]]): Transformer class for mapping to Atlas entities. Defaults to AtlasTransformer.
        """
        self.application_name = name
        self.transformer_class = transformer_class or AtlasTransformer
        self.sql_client_class = sql_client_class
        self.handler_class = handler_class or BaseSQLHandler

        # setup application server. serves the UI, and handles the various triggers
        self.application = None

        # setup workflow client for worker and application server
        self.workflow_client = get_workflow_client(application_name=self.application_name)

    async def setup_workflow(
        self,
        workflow_classes: List[Type] = [BaseSQLMetadataExtractionWorkflow],
        activities_class: Type = BaseSQLMetadataExtractionActivities,
        worker_daemon_mode: bool = True,
    ):
        """
        Set up the workflow client and start the worker for SQL metadata extraction.

        Args:
            workflow_classes (List[Type]): List of workflow classes to register. Defaults to [BaseSQLMetadataExtractionWorkflow].
            activities_class (Type): Activities class to use for workflow activities. Defaults to BaseSQLMetadataExtractionActivities.
            worker_daemon_mode (bool): Whether to run the worker in daemon mode. Defaults to True.
        """

        # load the workflow client
        await self.workflow_client.load()

        workflow_class: BaseSQLMetadataExtractionWorkflow = workflow_classes[0]

        # setup sql metadata extraction activities
        # requires a sql client for source connectivity, handler for preflight checks, transformer for atlas mapping
        activities = activities_class(
            sql_client_class=self.sql_client_class,
            handler_class=BaseSQLHandler,
            transformer_class=self.transformer_class,
        )

        # setup and start worker for workflow and activities execution
        worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=workflow_classes,
            workflow_activities=workflow_class.get_activities(activities),
        )
        await worker.start(daemon=worker_daemon_mode)

    async def start_workflow(self, workflow_args: Dict[str, Any], workflow_class: Type = BaseSQLMetadataExtractionWorkflow) -> Any:
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

    async def setup_server(self, workflow_class: Type = BaseSQLMetadataExtractionWorkflow) -> Any:
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
        self.application = Application(
            handler=self.handler_class(sql_client=self.sql_client_class()),
            workflow_client=self.workflow_client,
        )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.application.register_workflow(
            workflow_class=workflow_class,
            triggers=[HttpWorkflowTrigger()],
        )

    async def start_server(self, daemon: bool = True) -> Any:
        """
        Start the FastAPI server for the application.

        Args:
            daemon (bool): Whether to run the server in daemon mode. Defaults to True.

        Returns:
            Any: None

        Raises:
            ValueError: If the application server is not initialized.
        """
        if self.application is None:
            raise ValueError("Application server not initialized")

        await self.application.start()
