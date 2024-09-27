import logging
import uuid
from typing import Any, Dict

from temporalio.client import Client, WorkflowFailureError, WorkflowHandle

from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError

from application_sdk.common.converter import transform_metadata
from application_sdk.dto.workflow import WorkflowConfig, WorkflowRequestPayload
from application_sdk.interfaces.platform import Platform

from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows import WorkflowWorkerInterface

logger = logging.getLogger(__name__)

class SQLWorkflowWorkerInterface(WorkflowWorkerInterface):
    DATABASE_SQL = ""
    SCHEMA_SQL = ""
    TABLE_SQL = ""
    COLUMN_SQL = ""

    def __init__(self,
        application_name: str = "sql-connector"
    ):
        super().__init__(application_name)

    @activity.defn
    @auto_heartbeater
    async def fetch_databases(config: WorkflowConfig):
        pass

    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(self, credentialGuid: str, config: WorkflowConfig):
        pass

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(self):
        pass

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(self):
        pass

    @activity.defn
    @auto_heartbeater
    async def fetch_sql(self, sql: str):
        pass


    async def workflow_execution_handler(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        workflow_payload = WorkflowRequestPayload(**workflow_args)

        client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace="default"
            # FIXME: causes issue with different namespace, TBR.
        )

        workflow_id = str(uuid.uuid4())
        credential_guid = Platform.store_credentials(workflow_payload.credentials)

        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)

        config = WorkflowConfig(
            workflowId=workflow_id,
            credentialsGUID=credential_guid,
            includeFilterStr=workflow_payload.metadata.include_filter,
            excludeFilterStr=workflow_payload.metadata.exclude_filter,
            tempTableRegexStr=workflow_payload.metadata.temp_table_regex,
            outputType="JSON",
            outputPrefix="/tmp/output",
            verbose=True,
        )

        try:
            handle: WorkflowHandle[Any, Any] = await client.start_workflow(  # pyright: ignore[reportUnknownMemberType]
                self.TEMPORAL_WORKFLOW_NAME,
                config,
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
