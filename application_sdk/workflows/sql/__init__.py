import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError

from application_sdk.interfaces.platform import Platform
from application_sdk.workflows import (
    WorkflowAuthInterface,
    WorkflowBuilderInterface,
    WorkflowMetadataInterface,
    WorkflowPreflightCheckInterface,
    WorkflowWorkerInterface,
)
from application_sdk.workflows.sql.auth import SQLWorkflowAuthInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import SQLWorkflowPreflightCheckInterface
from application_sdk.workflows.sql.worker import SQLWorkflowWorkerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater

logger = logging.getLogger(__name__)


class SQLWorkflowBuilderInterface(WorkflowBuilderInterface, ABC):
    @abstractmethod
    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        pass

    @abstractmethod
    def get_sqlalchemy_connect_args(
        self, credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        pass

    def __init__(
        self,
        auth_interface: Optional[WorkflowAuthInterface] = None,
        metadata_interface: Optional[WorkflowMetadataInterface] = None,
        preflight_check_interface: Optional[WorkflowPreflightCheckInterface] = None,
        worker_interface: Optional[WorkflowWorkerInterface] = None,
    ):
        if not auth_interface:
            auth_interface = SQLWorkflowAuthInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not metadata_interface:
            metadata_interface = SQLWorkflowMetadataInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not preflight_check_interface:
            preflight_check_interface = SQLWorkflowPreflightCheckInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not worker_interface:
            worker_interface = SQLWorkflowWorkerInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        super().__init__(
            auth_interface,
            metadata_interface,
            preflight_check_interface,
            worker_interface,
        )


@workflow.defn
class SQLWorkflowWorker(WorkflowWorkerInterface):
    DATABASE_SQL = ""
    SCHEMA_SQL = ""
    TABLE_SQL = ""
    COLUMN_SQL = ""

    @staticmethod
    async def run_query_in_batch():
        pass

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_databases(self):
        pass

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(self):
        pass

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_tables(self):
        pass

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_columns(self):
        pass

    @staticmethod
    @activity.defn
    @auto_heartbeater
    async def fetch_sql(self, sql: str):
        pass

    def __init__(self, application_name: str):
        self.TEMPORAL_ACTIVITIES = [
            self.fetch_databases,
            self.fetch_schemas,
            self.fetch_tables,
            self.fetch_columns,
        ]
        super().__init__(application_name)

    @workflow.run
    async def run(self, *args, **kwargs) -> Dict[str, Any]:
        workflow_id = str(uuid.uuid4())
        credential_config = kwargs.get('credentials').get_credential_config()
        credential_guid = Platform.store_credentials(credential_config)

        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)

        try:
            handle = await self.temporal_client.start_workflow(
                self.TEMPORAL_WORKFLOW_NAME,
                *args,
                **kwargs,
                id=workflow_id,
                task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            )
            logger.info(f"Workflow started: {handle.id} {handle.result_run_id}")
            return {
                "message": "Workflow started",
                "workflow_id": handle.id,
                "run_id": handle.result_run_id,
            }

        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e
