"""
This examples demonstrates how to connect to a SQL database and extract metadata from it.

Usage Instructions:
- Set the usage credentials.

Actions performed by this workflow:
- Perform preflight checks
- Spawns a temporal workflow to execute the following activities
- Workflow Activities
    - Create an output directory
    - Fetch databases
    - Fetch schemas
    - Fetch tables
    - Fetch columns
    - Clean up output directory
    - Push results to object store
"""

import logging
import time
import threading
import uuid
from temporalio import workflow, activity
from temporalio.worker import Worker
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
import asyncio
from typing import Any, Dict, Self, TypeVar
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)
from urllib.parse import quote_plus
from application_sdk.dto.workflow import WorkflowConfig, WorkflowRequestPayload
from application_sdk.interfaces.platform import Platform
from application_sdk.logging import get_logger
from application_sdk.workflows.sql import SQLWorkflowBuilderInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import SQLWorkflowPreflightCheckInterface
from application_sdk.workflows.sql.workflow import SQLWorkflowWorkerInterface

APPLICATION_NAME = "sample-sql-workflow"


logger = get_logger(__name__)

class SampleSQLWorkflowMetadata(SQLWorkflowMetadataInterface):
    METADATA_SQL = """
    SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA;
    """


class SampleSQLWorkflowPreflight(SQLWorkflowPreflightCheckInterface):
    METADATA_SQL = """
    SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA;
    """
    TABLES_CHECK_SQL = """
    SELECT count(*)
    FROM INFORMATION_SCHEMA.TABLES;
    """


@workflow.defn
class SampleSQLWorkflowWorker(SQLWorkflowWorkerInterface):
    DATABASE_SQL = """
    SELECT * FROM pg_database WHERE datname = current_database();
    """
    TEMPORAL_ACTIVITIES = [
        SQLWorkflowWorkerInterface.setup_output_directory,
        SQLWorkflowWorkerInterface.fetch_databases,
        SQLWorkflowWorkerInterface.teardown_output_directory,
        SQLWorkflowWorkerInterface.push_results_to_object_store,
    ]
    PASSTHROUGH_MODULES = ["application_sdk"]

    @workflow.run
    async def run(self, config: WorkflowConfig) -> Dict[str, Any]:
        return await super().run(config)

    async def start_worker(self):
        self.temporal_client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace="default"
            # FIXME: causes issue with namespace other than default, To be reviewed.
        )

        self.temporal_worker = Worker(
            self.temporal_client,
            task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            workflows=[SampleSQLWorkflowWorker],
            activities=self.TEMPORAL_ACTIVITIES,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.PASSTHROUGH_MODULES
                )
            )
        )

        await self.temporal_worker.run()

    async def workflow_execution_handler(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        # FIXME: This has to contain the workflow execution logic 
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
                SampleSQLWorkflowWorker,
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


class SampleSQLWorkflowBuilder(SQLWorkflowBuilderInterface):
    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        encoded_password = quote_plus(credentials["password"])
        return f"postgresql+psycopg2://{credentials['user']}:{encoded_password}@{credentials['host']}:{credentials['port']}/{credentials['database']}"

    def get_sqlalchemy_connect_args(
        self, credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {}

    def __init__(self):
        # Preflight check
        preflight_check = SampleSQLWorkflowPreflight(
            self.get_sqlalchemy_connection_string,
            self.get_sqlalchemy_connect_args,
        )

        # Metadata interface
        metadata_interface = SampleSQLWorkflowMetadata(
            self.get_sqlalchemy_connection_string,
            self.get_sqlalchemy_connect_args,
        )

        # Worker interface
        worker_interface = SampleSQLWorkflowWorker(
            APPLICATION_NAME
        )

        super().__init__(
            metadata_interface=metadata_interface,
            preflight_check_interface=preflight_check,
            worker_interface=worker_interface,
        )

def run_worker(worker_interface):
    asyncio.run(worker_interface.start_worker())


if __name__ == "__main__":
    builder = SampleSQLWorkflowBuilder()
    # Start the temporal worker in a separate thread
    worker_thread = threading.Thread(
        target=run_worker,
        args=(builder.worker_interface,),
        daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)


    asyncio.run(builder.worker_interface.workflow_execution_handler(
        {
            "credentials": {
                "host": "host",
                "port": 5432,
                "user": "postgres",
                "password": "password",
                "database": "assets_100k"
            },
            "connection": {
                "connection": "dev"
            },
            "metadata": {
                "exclude-filter": "{}",
                "include-filter": "{}",
                "temp-table-regex": "",
                "advanced-config-strategy": "default",
                "use-source-schema-filtering": "false",
                "use-jdbc-internal-methods": "true",
                "authentication": "BASIC",
                "extraction-method": "direct"
            }
        }
    ))

    # wait for the workflow to finish
    time.sleep(100)
