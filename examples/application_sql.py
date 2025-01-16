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
import logging
import os
import threading
import time
from urllib.parse import quote_plus

from application_sdk.clients.async_sql_client import AsyncSQLClient
from application_sdk.clients.sql_client import SQLClient, SQLClientConfig
from application_sdk.clients.temporal_client import TemporalClient, TemporalConfig
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.sql.builders.builder import SQLWorkflowBuilder
from application_sdk.workflows.sql.controllers.preflight_check import (
    SQLWorkflowPreflightCheckController,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers.atlas import AtlasTransformer
from application_sdk.workflows.workers.worker import WorkflowWorker

APPLICATION_NAME = "postgres"

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class PostgreSQLClient(AsyncSQLClient):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.config.credentials["password"])
        return f"postgresql+psycopg://{self.config.credentials['user']}:{encoded_password}@{self.config.credentials['host']}:{self.config.credentials['port']}/{self.config.credentials['database']}"


class SampleSQLWorkflow(SQLWorkflow):
    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """

    fetch_schema_sql = """
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

    fetch_table_sql = """
    SELECT
        t.*
    FROM
        information_schema.tables t
    WHERE
        concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        AND t.table_name !~ '{exclude_table}';
    """

    fetch_column_sql = """
    SELECT
        c.*
    FROM
        information_schema.columns c
    WHERE
        concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        AND c.table_name !~ '{exclude_table}';
    """

    sql_client: SQLClient | None = PostgreSQLClient(SQLClientConfig())


class SampleSQLWorkflowBuilder(SQLWorkflowBuilder):
    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        return super().build(workflow=workflow or SampleSQLWorkflow())


class SampleSQLWorkflowPreflightCheckController(SQLWorkflowPreflightCheckController):
    TABLES_CHECK_SQL = """
    SELECT count(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME !~ '{exclude_table}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
            AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
    """

    METADATA_SQL = """
    SELECT schema_name, catalog_name
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
    """


async def application_sql():
    print("Starting application_sql")

    temporal_client = TemporalClient(
        TemporalConfig(
            application_name=APPLICATION_NAME,
        )
    )
    await temporal_client.load()

    tenant_id = os.getenv("TENANT_ID", "development")

    transformer = AtlasTransformer(
        connector_name=APPLICATION_NAME,
        connector_type="sql",
        tenant_id=tenant_id,
    )

    sql_client = PostgreSQLClient(SQLClientConfig())

    workflow: SQLWorkflow = (
        SampleSQLWorkflowBuilder()
        .set_transformer(transformer)
        .set_temporal_client(temporal_client)
        .set_sql_client(sql_client)
        .set_preflight_check_controller(
            SampleSQLWorkflowPreflightCheckController(sql_client)
        )
        .build()
    )

    worker: WorkflowWorker = WorkflowWorker(
        temporal_client=temporal_client,
        temporal_activities=workflow.get_activities(),
        workflow_classes=[SQLWorkflow],
        passthrough_modules=["application_sdk", "os", "pandas"],
    )

    # Start the worker in a separate thread
    worker_thread = threading.Thread(
        target=lambda: asyncio.run(worker.start()), daemon=True
    )
    worker_thread.start()

    # wait for the worker to start
    time.sleep(3)

    workflow_response = await workflow.start(
        {
            "credentials": {
                "host": os.getenv("POSTGRES_HOST", "localhost"),
                "port": os.getenv("POSTGRES_PORT", "5432"),
                "user": os.getenv("POSTGRES_USER", "postgres"),
                "password": os.getenv("POSTGRES_PASSWORD", "password"),
                "database": os.getenv("POSTGRES_DATABASE", "postgres"),
            },
            "connection": {"connection": "dev"},
            "metadata": {
                "exclude_filter": "{}",
                "include_filter": "{}",
                "temp_table_regex": "",
                "advanced_config_strategy": "default",
                "use_source_schema_filtering": "false",
                "use_jdbc_internal_methods": "true",
                "authentication": "BASIC",
                "extraction-method": "direct",
                "exclude_views": "true",
                "exclude_empty_tables": "false",
            },
            # "workflow_id": "27498f69-13ae-44ec-a2dc-13ff81c517de",  # if you want to rerun an existing workflow, just keep this field.
            # "cron_schedule": "0/30 * * * *", # uncomment to run the workflow on a cron schedule, every 30 minutes
        }
    )

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_sql())
    time.sleep(1000000)
