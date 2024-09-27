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
import os
import time
import threading
from temporalio import workflow
import asyncio
from typing import Any, Dict
from urllib.parse import quote_plus
from application_sdk.logging import get_logger
from application_sdk.workflows.models.workflow import WorkflowConfig
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

    def __init__(self, application_name=APPLICATION_NAME, *args, **kwargs):
        self.TEMPORAL_WORKFLOW_CLASS = SampleSQLWorkflowWorker
        super().__init__(application_name, *args, **kwargs)

    @workflow.run
    async def run(self, config: WorkflowConfig):
        await super().run(config)


class SampleSQLWorkflowBuilder(SQLWorkflowBuilderInterface):
    def get_sqlalchemy_connect_args(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        encoded_password = quote_plus(credentials["password"])
        return f"postgresql+psycopg2://{credentials['user']}:{encoded_password}@{credentials['host']}:{credentials['port']}/{credentials['database']}"

    def __init__(self, *args, **kwargs):
        self.metadata_interface = SampleSQLWorkflowMetadata(
            self.get_sql_engine
        )
        self.preflight_interface = SampleSQLWorkflowPreflight(
            self.get_sql_engine
        )
        self.worker_interface = SampleSQLWorkflowWorker(
            APPLICATION_NAME,
            get_sql_engine=self.get_sql_engine
        )
        super().__init__(
            metadata_interface=self.metadata_interface,
            preflight_check_interface=self.preflight_interface,
            worker_interface=self.worker_interface,
            *args, **kwargs
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

    asyncio.run(builder.worker_interface.run_workflow(
        {
            "credentials": {
                "host": os.getenv("POSTGRES_HOST", "localhost"),
                "port": os.getenv("POSTGRES_PORT", "5432"),
                "user": os.getenv("POSTGRES_USER", "postgres"),
                "password": os.getenv("POSTGRES_PASSWORD", "password"),
                "database": os.getenv("POSTGRES_DATABASE", "postgres")
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
