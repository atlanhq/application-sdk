"""
This example demonstrates how to create a SQL workflow for extracting metadata from a PostgreSQL database with a custom transformer.
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
7. Transform the metadata into Atlas entities but using a custom transformer for Database entities
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
import time
from typing import Any, Dict
from urllib.parse import quote_plus

from pyatlan.model.assets import Database

from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.clients.sql import AsyncSQLClient
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers.sql import SQLHandler
from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import (
    SQLMetadataExtractionWorkflow,
)

APPLICATION_NAME = "postgres-custom-transformer"
DATABASE_DRIVER = "psycopg2"
DATABASE_DIALECT = "postgresql"

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class PostgreSQLClient(AsyncSQLClient):
    def get_sqlalchemy_connection_string(self) -> str:
        encoded_password: str = quote_plus(self.credentials["password"])
        return f"postgresql+psycopg://{self.credentials['user']}:{encoded_password}@{self.credentials['host']}:{self.credentials['port']}/{self.credentials['database']}"


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


class PostgresDatabase(Database):
    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> Database:
        database = Database.creator(
            name=obj["datname"],
            connection_qualified_name=obj["connection_qualified_name"],
        )
        return database


class CustomTransformer(AtlasTransformer):
    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        super().__init__(connector_name, tenant_id, **kwargs)

        self.entity_class_definitions["DATABASE"] = PostgresDatabase


class SampleSQLHandler(SQLHandler):
    tables_check_sql = """
    SELECT count(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_NAME !~ '{exclude_table}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
            AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
    """

    metadata_sql = """
    SELECT schema_name, catalog_name
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
    """


async def application_sql_with_custom_transformer(
    daemon: bool = True,
) -> Dict[str, Any]:
    print("Starting application_sql_with_custom_transformer")

    temporal_client = TemporalClient(
        application_name=APPLICATION_NAME,
    )
    await temporal_client.load()

    activities = SampleSQLActivities(
        sql_client_class=PostgreSQLClient,
        handler_class=SampleSQLHandler,
        transformer_class=CustomTransformer,
    )

    worker: Worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[SQLMetadataExtractionWorkflow],
        temporal_activities=SQLMetadataExtractionWorkflow.get_activities(activities),
    )

    # wait for the worker to start
    time.sleep(3)

    workflow_args = {
        "credentials": {
            "host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "user": os.getenv("POSTGRES_USER", "postgres"),
            "password": os.getenv("POSTGRES_PASSWORD", "password"),
            "database": os.getenv("POSTGRES_DATABASE", "postgres"),
        },
        "connection": {
            "connection_name": "test-connection",
            "connection_qualified_name": "default/postgres/1728518400",
        },
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
        "tenant_id": "123",
        # "workflow_id": "27498f69-13ae-44ec-a2dc-13ff81c517de",  # if you want to rerun an existing workflow, just keep this field.
        # "cron_schedule": "0/30 * * * *", # uncomment to run the workflow on a cron schedule, every 30 minutes
    }

    workflow_response = await temporal_client.start_workflow(
        workflow_args, SQLMetadataExtractionWorkflow
    )

    # Start the worker in a separate thread
    await worker.start(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_sql_with_custom_transformer(daemon=False))
    time.sleep(1000000)
