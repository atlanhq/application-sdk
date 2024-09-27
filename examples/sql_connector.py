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

# from datetime import time
import time
import threading
from temporalio import activity, workflow
import asyncio
from typing import Any, Dict
from urllib.parse import quote_plus
from application_sdk.dto.workflow import WorkflowConfig
from application_sdk.workflows.sql import SQLWorkflowBuilderInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import SQLWorkflowPreflightCheckInterface
from application_sdk.workflows.sql.workflow import SQLWorkflowWorkerInterface

APPLICATION_NAME = "sample-sql-workflow"


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


class SampleSQLWorkflowWorker(SQLWorkflowWorkerInterface):
    # FIXME: this is not picked up currently
    DATABASE_SQL = """
    SELECT * FROM pg_database WHERE datname = current_database();
    """
    TEMPORAL_WORKFLOW_NAME = SQLWorkflowWorkerInterface
    TEMPORAL_ACTIVITIES = [
        SQLWorkflowWorkerInterface.setup_output_directory,
        SQLWorkflowWorkerInterface.fetch_databases,
        SQLWorkflowWorkerInterface.teardown_output_directory,
        SQLWorkflowWorkerInterface.push_results_to_object_store,
    ]
    PASSTHROUGH_MODULES = ["application_sdk"]



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

    print("Start workflow")

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
