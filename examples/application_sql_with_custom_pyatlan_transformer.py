"""
This example demonstrates how to create a SQL workflow for extracting metadata from a PostgreSQL database with a custom transformer.
It uses the Temporal workflow engine to manage the extraction process.

Key components:
- SampleSQLWorkflowMetadata: Defines metadata extraction queries
- SampleSQLWorkflowPreflight: Performs preflight checks
- SampleSQLWorkflowWorker: Implements the main workflow logic (including extraction and transformation)
- SampleSQLWorkflow: Configures and builds the workflow

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
import os
import time
from typing import Any, Dict

from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)
from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.transformers.atlas.sql import Column, Procedure, Table
from application_sdk.workflows.metadata_extraction.sql import (
    BaseSQLMetadataExtractionWorkflow,
)

APPLICATION_NAME = "postgres-custom-transformer"
DATABASE_DIALECT = "postgresql"

logger = get_logger(__name__)


class SQLClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }


class SampleSQLActivities(BaseSQLMetadataExtractionActivities):
    fetch_database_sql = """
    SELECT d.*, d.datname as database_name FROM pg_database d WHERE datname = current_database();
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

    extract_temp_table_regex_table_sql = "AND t.table_name !~ '{exclude_table_regex}'"
    extract_temp_table_regex_column_sql = "AND c.table_name !~ '{exclude_table_regex}'"

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


class PostgresTable(Table):
    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """
        Postgres view and materialized view definitions are select queries,
        so we need to format the view definition to be a valid SQL query.

        src: https://github.com/atlanhq/marketplace-packages/blob/master/packages/atlan/postgres/transformers/view.jinja2
        """
        assert "table_name" in obj, "table_name cannot be None"
        assert "table_type" in obj, "table_type cannot be None"

        entity_data = super().get_attributes(obj)
        table_attributes = entity_data.get("attributes", {})
        table_custom_attributes = entity_data.get("custom_attributes", {})

        table_attributes["constraint"] = obj.get("partition_constraint", "")

        if (
            obj.get("table_kind", "") == "p"
            or obj.get("table_type", "") == "PARTITIONED TABLE"
        ):
            table_attributes["is_partitioned"] = True
            table_attributes["partition_strategy"] = obj.get("partition_strategy", "")
            table_attributes["partition_count"] = obj.get("partition_count", 0)
        else:
            table_attributes["is_partitioned"] = False

        table_custom_attributes["is_insertable_into"] = obj.get(
            "is_insertable_into", False
        )
        table_custom_attributes["is_typed"] = obj.get("is_typed", False)
        table_custom_attributes["self_referencing_col_name"] = obj.get(
            "self_referencing_col_name", ""
        )
        table_custom_attributes["ref_generation"] = obj.get("ref_generation", "")
        if obj.get("table_type") == "VIEW":
            view_definition = "CREATE OR REPLACE VIEW {view_name} AS {query}"
            table_attributes["definition"] = view_definition.format(
                view_name=obj.get("table_name", ""),
                query=obj.get("view_definition", ""),
            )
        elif obj.get("table_type") == "MATERIALIZED VIEW":
            view_definition = "CREATE MATERIALIZED VIEW {view_name} AS {query}"
            table_attributes["definition"] = view_definition.format(
                view_name=obj.get("table_name", ""),
                query=obj.get("view_definition", ""),
            )

        entity_class = None
        if entity_data["entity_class"] == Table:
            entity_class = PostgresTable
        else:
            entity_class = entity_data["entity_class"]

        return {
            **entity_data,
            "attributes": table_attributes,
            "custom_attributes": table_custom_attributes,
            "entity_class": entity_class,
        }


class PostgresColumn(Column):
    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        entity_data = super().get_attributes(obj)

        column_attributes = entity_data.get("attributes", {})
        column_custom_attributes = entity_data.get("custom_attributes", {})

        if obj.get("numeric_precision_radix", "") != "":
            column_custom_attributes["num_prec_radix"] = obj.get(
                "numeric_precision_radix", ""
            )
        if obj.get("is_identity", "") != "":
            column_custom_attributes["is_identity"] = obj.get("is_identity", "")
        if obj.get("identity_cycle", "") != "":
            column_custom_attributes["identity_cycle"] = obj.get("identity_cycle", "")

        if obj.get("constraint_type", "") == "PRIMARY KEY":
            column_attributes["is_primary"] = True

        elif obj.get("constraint_type", "") == "FOREIGN KEY":
            column_attributes["is_foreign"] = True

        return {
            **entity_data,
            "attributes": column_attributes,
            "custom_attributes": column_custom_attributes,
            "entity_class": PostgresColumn,
        }


class SQLAtlasTransformer(AtlasTransformer):
    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        super().__init__(connector_name, tenant_id, **kwargs)

        self.entity_class_definitions["TABLE"] = PostgresTable
        self.entity_class_definitions["COLUMN"] = PostgresColumn
        self.entity_class_definitions["EXTRAS-PROCEDURE"] = Procedure


class SampleSQLHandler(BaseSQLHandler):
    tables_check_sql = """
    SELECT count(*)
        FROM INFORMATION_SCHEMA.TABLES
        WHERE concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) !~ '{normalized_exclude_regex}'
            AND concat(TABLE_CATALOG, concat('.', TABLE_SCHEMA)) ~ '{normalized_include_regex}'
            AND TABLE_SCHEMA NOT IN ('performance_schema', 'information_schema', 'pg_catalog', 'pg_internal')
            {temp_table_regex_sql};
    """

    extract_temp_table_regex_table_sql = "AND t.table_name !~ '{exclude_table_regex}'"

    metadata_sql = """
    SELECT schema_name, catalog_name
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'
    """


async def application_sql_with_custom_pyatlan_transformer(
    daemon: bool = True,
) -> Dict[str, Any]:
    logger.info("Starting application_sql_with_custom_pyatlan_transformer")

    app = BaseSQLMetadataExtractionApplication(
        name=APPLICATION_NAME,
        client_class=SQLClient,
        handler_class=SampleSQLHandler,
        transformer_class=SQLAtlasTransformer,
    )

    await app.setup_workflow(
        workflow_and_activities_classes=[
            (BaseSQLMetadataExtractionWorkflow, SampleSQLActivities)
        ]
    )

    # wait for the worker to start
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
        # "workflow_id": "27498f69-13ae-44ec-a2dc-13ff81c517de",  # if you want to rerun an existing workflow, just keep this field.
        # "cron_schedule": "0/30 * * * *", # uncomment to run the workflow on a cron schedule, every 30 minutes
    }

    workflow_response = await app.start_workflow(workflow_args=workflow_args)

    await app.start_worker(daemon=daemon)

    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_sql_with_custom_pyatlan_transformer(daemon=False))
    time.sleep(1000000)
