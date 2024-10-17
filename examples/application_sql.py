"""
This example demonstrates how to create a SQL workflow for extracting metadata from a PostgreSQL database.
It uses the Temporal workflow engine to manage the extraction process.

Key components:
- SampleSQLWorkflowMetadata: Defines metadata extraction queries
- SampleSQLWorkflowPreflight: Performs preflight checks
- SampleSQLWorkflowWorker: Implements the main workflow logic (including extraction and transformation)
- SampleSQLWorkflowBuilder: Configures and builds the workflow

Workflow steps:
1. Perform preflight checks
2. Create an output directory
3. Fetch database information
4. Fetch schema information
5. Fetch table information
6. Fetch column information
7. Transform the metadata into Atlas entities
8. Clean up the output directory
9. Push results to object store

Usage:
1. Set the PostgreSQL connection credentials as environment variables
2. Run the script to start the Temporal worker and execute the workflow

Note: This example is specific to PostgreSQL but can be adapted for other SQL databases.
"""

import asyncio
import os
import threading
import time
from typing import Any, Dict
from urllib.parse import quote_plus

from temporalio import workflow

from application_sdk.logging import get_logger
from application_sdk.workflows.sql import SQLWorkflowBuilderInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import (
    SQLWorkflowPreflightCheckInterface,
)
from application_sdk.workflows.sql.workflow import SQLWorkflowWorkerInterface
from application_sdk.workflows.transformers.atlas import AtlasTransformer

APPLICATION_NAME = "postgres"


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

    SCHEMA_SQL = """
    SELECT
        s.*
    FROM
        information_schema.schemata s
    WHERE
        s.schema_name NOT LIKE 'pg_%'
        AND s.schema_name != 'information_schema'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
    """

    TABLE_SQL = """
    SELECT
        t.*
    FROM
        information_schema.tables t
    WHERE
        concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        AND t.table_name !~ '{exclude_table}';
    """

    COLUMN_SQL = """
    SELECT
        c.*
    FROM
        information_schema.columns c
    WHERE
        concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        AND c.table_name !~ '{exclude_table}';
    """

    # PASSTHROUGH_MODULES: Sequence[str] = ["application_sdk", "time"]

    def __init__(
        self, application_name: str = APPLICATION_NAME, *args: Any, **kwargs: Any
    ):
        self.TEMPORAL_WORKFLOW_CLASS = SampleSQLWorkflowWorker
        # we use the default TEMPORAL_ACTIVITIES from the parent class (SQLWorkflowWorkerInterface)
        transformer = AtlasTransformer(
            connector_name=application_name, connector_type="sql"
        )
        super().__init__(
            transformer=transformer,
            application_name=application_name,
            *args,
            **kwargs,
        )

    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]):
        await super().run(workflow_args)


class SampleSQLWorkflowBuilder(SQLWorkflowBuilderInterface):
    def get_sqlalchemy_connect_args(
        self, credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        return {}

    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        encoded_password = quote_plus(credentials["password"])
        return f"postgresql+psycopg2://{credentials['user']}:{encoded_password}@{credentials['host']}:{credentials['port']}/{credentials['database']}"

    def __init__(self, *args: Any, **kwargs: Any):
        self.metadata_interface = SampleSQLWorkflowMetadata(self.get_sql_engine)
        self.preflight_interface = SampleSQLWorkflowPreflight(self.get_sql_engine)
        self.worker_interface = SampleSQLWorkflowWorker(
            APPLICATION_NAME, get_sql_engine=self.get_sql_engine
        )
        super().__init__(
            metadata_interface=self.metadata_interface,
            preflight_check_interface=self.preflight_interface,
            worker_interface=self.worker_interface,
            *args,
            **kwargs,
        )


if __name__ == "__main__":
    builder = SampleSQLWorkflowBuilder()
    # Start the temporal worker in a separate thread
    worker_thread = threading.Thread(target=builder.start_worker, args=(), daemon=True)
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)

    asyncio.run(
        builder.worker_interface.start_workflow(
            {
                "credentials": {
                    "host": os.getenv("POSTGRES_HOST", "localhost"),
                    "port": os.getenv("POSTGRES_PORT", "5432"),
                    "user": os.getenv("POSTGRES_USER", "postgres"),
                    "password": os.getenv("POSTGRES_PASSWORD", "password"),
                    "database": os.getenv("POSTGRES_DATABASE", "assets_100k"),
                },
                "connection": {"connection": "dev"},
                "metadata": {
                    "exclude-filter": "{}",
                    "include-filter": "{}",
                    "temp-table-regex": "",
                    "advanced-config-strategy": "default",
                    "use-source-schema-filtering": "false",
                    "use-jdbc-internal-methods": "true",
                    "authentication": "BASIC",
                    "extraction-method": "direct",
                },
            }
        )
    )

    # wait for the workflow to finish
    time.sleep(300)
