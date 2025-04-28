"""
Application = Workflow + API Server

Workflow = Workflow Client + Worker

API Server = Server Routes + Frontend Templates
"""

from typing import Any, Callable, Dict, List, Optional, Type
from application_sdk.clients.utils import get_workflow_client
from application_sdk.clients import ClientInterface
from application_sdk.transformers import TransformerInterface
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionActivities


class SQLMetadataExtractionApplication:
    def __init__(
        self,
        application_name: str,
        sql_client: SQLClient,
        handler: Optional[HandlerInterface] = None,
        transformer: TransformerInterface,
        workflow_classes: List[Type] = [],
    ):
        self.application_name = application_name
        self.workflow_classes = workflow_classes

        self.transformer = transformer or AtlasTransformer()
        self.sql_client = sql_client
        self.handler = handler or BaseSQLHandler(sql_client=sql_client)

        # setup application server. serves the UI, and handles the various triggers
        self.application = None

        self.workflow_client = get_workflow_client(application_name=self.application_name)
        await self.workflow_client.load()

    async def setup_workflow(
        self,
        worker_daemon_mode: bool = True,
    ):
        """
        Setup the workflow client and worker
        """

        activities = BaseSQLMetadataExtractionActivities(
            sql_client_class=self.sql_client,
            handler_class=BaseSQLHandler,
            transformer_class=self.transformer,
        )


        worker = Worker(
            workflow_client=self.workflow_client,
            workflow_classes=BaseSQLMetadataExtractionWorkflow,
            workflow_activities=BaseSQLMetadataExtractionWorkflow.get_activities(
                activities
            ),
        )

        await worker.start(daemon=worker_daemon_mode)

    async def start_workflow(self, workflow_args: Dict[str, Any], daemon: bool = True) -> Any:
        if self.workflow_client is None:
            raise ValueError("Workflow client not initialized")

        workflow_response = await self.workflow_client.start_workflow(
            self.workflow_args, workflow_class
        )
        return workflow_response

    def setup_application_server(self, daemon: bool = True) -> Any:
        """
        Setup the server
        """
        if self.workflow_client is None:
            raise ValueError("Workflow client not initialized")

        self.application = Application(
            handler=self.handler,
            workflow_client=self.workflow_client,
        )

        # register the workflow on the application server
        # the workflow is by default triggered by an HTTP POST request to the /start endpoint
        self.application.register_workflow(
            workflow_class=BaseSQLMetadataExtractionWorkflow,
            triggers=[HttpWorkflowTrigger()],
        )

    async def start_application_server(self, daemon: bool = True) -> Any:
        """
        Start the server
        """
        if self.application is None:
            raise ValueError("Application server not initialized")

        await self.application.start()

    async def stop_server(self):
        if self.application is None:
            raise ValueError("Application server not initialized")

        await self.application.stop()
