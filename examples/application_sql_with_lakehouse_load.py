"""
This example demonstrates how to create a SQL metadata extraction workflow
that loads extracted data into an Iceberg lakehouse via the MDLH service.

After extraction and transformation, the SDK automatically calls the MDLH
REST API to load:
  1. Raw parquet files into a "raw" Iceberg table (after all fetches complete)
  2. Transformed jsonl files into per-entity-type Iceberg tables in
     entity_metadata (e.g. database, schema, table, column)

**No code changes are required** — lakehouse loading is enabled entirely
through environment variables. This example shows the env var configuration
alongside a standard SQL extraction app.

Required environment variables for lakehouse load:
  ENABLE_LAKEHOUSE_LOAD=true
  MDLH_BASE_URL=http://mdlh:4541          (default)

  # Raw table (loaded after extraction)
  LH_LOAD_RAW_NAMESPACE=entity_raw
  LH_LOAD_RAW_TABLE_NAME=postgres_raw
  LH_LOAD_RAW_MODE=APPEND                 (default; or UPSERT)

  # Transformed tables (loaded per entity type after transformation)
  # Table name is derived from typename (e.g. "database" -> entity_metadata.database)
  LH_LOAD_TRANSFORMED_NAMESPACE=entity_metadata  (default)
  LH_LOAD_TRANSFORMED_MODE=APPEND                (default; or UPSERT)

  # Optional polling tuning
  LH_LOAD_POLL_INTERVAL_SECONDS=10        (default)
  LH_LOAD_MAX_POLL_ATTEMPTS=360           (default, = 1 hour at 10s intervals)

Workflow execution order:
  preflight_check
  get_workflow_args
  asyncio.gather:
    fetch_databases  -> transform_data
    fetch_schemas    -> transform_data
    fetch_tables     -> transform_data
    fetch_columns    -> transform_data
    fetch_procedures -> transform_data
  load_to_lakehouse  (raw/*.parquet -> raw Iceberg table)       [NEW]
  upload_to_atlan    (if ENABLE_ATLAN_UPLOAD=true)
  load_to_lakehouse  (transformed/*.jsonl -> transformed table) [NEW]

Usage:
  1. Set PostgreSQL connection credentials as environment variables
  2. Set the lakehouse load environment variables listed above
  3. Run: python examples/application_sql_with_lakehouse_load.py
"""

import asyncio
import os
import time
from typing import Any, Dict

from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)
from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionWorkflow,
)

APPLICATION_NAME = "postgres"

logger = get_logger(__name__)


class SQLClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )


class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    """Standard SQL extraction activities — no changes needed for lakehouse load."""

    fetch_database_sql = """
    SELECT datname as database_name FROM pg_database WHERE datname = current_database();
    """

    fetch_schema_sql = """
    SELECT s.*
    FROM information_schema.schemata s
    WHERE s.schema_name NOT LIKE 'pg_%'
        AND s.schema_name != 'information_schema'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) !~ '{normalized_exclude_regex}'
        AND concat(s.CATALOG_NAME, concat('.', s.SCHEMA_NAME)) ~ '{normalized_include_regex}';
    """

    fetch_table_sql = """
    SELECT t.*
    FROM information_schema.tables t
    WHERE concat(current_database(), concat('.', t.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', t.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """

    extract_temp_table_regex_table_sql = "AND t.table_name !~ '{exclude_table_regex}'"
    extract_temp_table_regex_column_sql = "AND c.table_name !~ '{exclude_table_regex}'"

    fetch_column_sql = """
    SELECT c.*
    FROM information_schema.columns c
    WHERE concat(current_database(), concat('.', c.table_schema)) !~ '{normalized_exclude_regex}'
        AND concat(current_database(), concat('.', c.table_schema)) ~ '{normalized_include_regex}'
        {temp_table_regex_sql};
    """


class SampleSQLHandler(BaseSQLHandler):
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


async def application_sql_with_lakehouse_load(daemon: bool = True) -> Dict[str, Any]:
    logger.info("Starting application_sql_with_lakehouse_load")

    # The application setup is identical to a standard SQL extraction app.
    # Lakehouse loading is configured purely through environment variables —
    # the SDK's workflow automatically calls load_to_lakehouse when
    # ENABLE_LAKEHOUSE_LOAD=true and namespace/table_name are set.
    app = BaseSQLMetadataExtractionApplication(
        name=APPLICATION_NAME,
        client_class=SQLClient,
        handler_class=SampleSQLHandler,
    )

    await app.setup_workflow(
        workflow_and_activities_classes=[
            (BaseSQLMetadataExtractionWorkflow, SampleSQLActivities)
        ]
    )

    time.sleep(3)

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
    }

    workflow_response = await app.start_workflow(workflow_args=workflow_args)

    await app.start_worker(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    # To enable lakehouse load, set these env vars before running:
    #   export ENABLE_LAKEHOUSE_LOAD=true
    #   export LH_LOAD_RAW_NAMESPACE=entity_raw
    #   export LH_LOAD_RAW_TABLE_NAME=postgres_raw
    #   export LH_LOAD_TRANSFORMED_NAMESPACE=entity_metadata
    asyncio.run(application_sql_with_lakehouse_load(daemon=False))
