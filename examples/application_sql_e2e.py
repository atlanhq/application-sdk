"""
This example demonstrates a complete workflow for extracting metadata from a PostgreSQL database
and publishing it to Atlas. It uses the Temporal workflow engine to manage the extraction and
publishing process.

Key components:
- PostgreSQLClient: Handles PostgreSQL-specific connection details
- SampleSQLActivities: Defines metadata extraction queries for PostgreSQL
- SampleSQLWorkflowHandler: Implements preflight checks and schema validation
- SQLMetadataExtractionWorkflow: Manages the main workflow logic
- AtlasPublishAtlanWorker: Handles publishing extracted metadata to Atlas

Workflow steps:
1. Perform preflight checks on the database
2. Extract database metadata:
   - Database information
   - Schema information
   - Table information
   - Column information
3. Transform the metadata into Atlas entities
4. Publish the results to Atlas

Usage:
1. Set the PostgreSQL connection credentials as environment variables:
   - POSTGRES_HOST
   - POSTGRES_PORT
   - POSTGRES_USER
   - POSTGRES_PASSWORD
   - POSTGRES_DATABASE
2. Run the script to start both the extraction and publishing workers
3. The workflow will automatically extract metadata and publish it to Atlas

Note: This example is specific to PostgreSQL but can be adapted for other SQL databases.
The workflow can be configured to run on a schedule using the cron_schedule parameter.
"""

import asyncio
import os
from typing import Any, Dict
from urllib.parse import quote_plus

from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.clients.sql import SQLClient
from application_sdk.clients.utils import get_workflow_client
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.handlers.sql import SQLHandler
from application_sdk.publish_app.worker import AtlasPublishAtlanWorker
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import (
    SQLMetadataExtractionWorkflow,
)

APPLICATION_NAME = "postgres"

logger = get_logger(__name__)


class PostgreSQLClient(SQLClient):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.credentials["password"])
        return f"postgresql+psycopg://{self.credentials['username']}:{encoded_password}@{self.credentials['host']}:{self.credentials['port']}/{self.credentials['database']}"


class SampleSQLActivities(SQLMetadataExtractionActivities):
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
    WHERE concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """

    tables_extraction_temp_table_regex_sql = (
        "AND t.table_name !~ '{exclude_table_regex}'"
    )
    column_extraction_temp_table_regex_sql = (
        "AND c.table_name !~ '{exclude_table_regex}'"
    )

    fetch_column_sql = """
    SELECT
        c.*
    FROM
        information_schema.columns c
    WHERE
        concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """


class SampleSQLWorkflowHandler(SQLHandler):
    tables_check_sql = """
    SELECT count(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
            AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
            {temp_table_regex_sql};
    """

    temp_table_regex_sql = "AND t.table_name !~ '{exclude_table_regex}'"

    metadata_sql = """
    SELECT schema_name, catalog_name
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
    """


async def application_sql(daemon: bool = True) -> Dict[str, Any]:
    print("Starting application_sql")

    workflow_client = get_workflow_client(
        application_name=APPLICATION_NAME,
    )
    await workflow_client.load()

    activities = SampleSQLActivities(
        sql_client_class=PostgreSQLClient, handler_class=SampleSQLWorkflowHandler
    )

    worker: Worker = Worker(
        workflow_client=workflow_client,
        workflow_classes=[SQLMetadataExtractionWorkflow],
        workflow_activities=SQLMetadataExtractionWorkflow.get_activities(activities),
    )

    workflow_args = {
        "credentials": {
            "authType": "basic",
            "host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "username": os.getenv("POSTGRES_USER", "postgres"),
            "password": os.getenv("POSTGRES_PASSWORD", "password"),
            "database": os.getenv("POSTGRES_DATABASE", "postgres"),
        },
        "connection": {
            "connection_name": "test-connection",
            "connection_qualified_name": "default/postgres/1728518400",
        },
        "metadata": {
            "exclude-filter": "{}",
            "include-filter": "{}",
            "temp-table-regex": "",
            "extraction-method": "direct",
            "exclude_views": "true",
            "exclude_empty_tables": "false",
        },
        "tenant_id": "123",
        SQLMetadataExtractionWorkflow.E2E_WORKFLOW_ARGS_KEY: "true",
        # "workflow_id": "27498f69-13ae-44ec-a2dc-13ff81c517de",  # if you want to rerun an existing workflow, just keep this field.
        # "cron_schedule": "0/30 * * * *", # uncomment to run the workflow on a cron schedule, every 30 minutes
    }

    publish_client = get_workflow_client(
        application_name=AtlasPublishAtlanWorker.TASK_QUEUE
    )
    await publish_client.load()
    publish_worker = AtlasPublishAtlanWorker(publish_client)
    await publish_worker.start(daemon)

    workflow_response = await workflow_client.start_workflow(
        workflow_args, SQLMetadataExtractionWorkflow
    )
    await worker.start(daemon=False)
    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_sql(daemon=True))
