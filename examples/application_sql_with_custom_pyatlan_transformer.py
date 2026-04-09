"""SQL metadata extraction with custom pyatlan-based transformer (v3 pattern).

Demonstrates how to customize metadata transformation using typed pyatlan
entity classes with SqlMetadataExtractor. In v3, custom entity classes
are registered on the extractor and used during the transform task.

Key differences from the base SQL example:
- Custom entity classes (PostgresTable, PostgresColumn) extend pyatlan types
- Entity class definitions are registered on the transformer
- Typed contracts replace Dict[str, Any] throughout

Usage:
    python examples/application_sql_with_custom_pyatlan_transformer.py
"""

import asyncio
from typing import Any, Dict

from application_sdk.app import task
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.main import run_dev_combined
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsInput,
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesInput,
    FetchTablesOutput,
    TransformInput,
    TransformOutput,
)
from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.transformers.atlas.sql import Column, Procedure, Table

logger = get_logger(__name__)


class SQLClient(BaseSQLClient):
    """PostgreSQL connection configuration."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )


class PostgresTable(Table):
    """Custom table entity with Postgres-specific attributes.

    Handles view definitions and partition metadata that are specific
    to PostgreSQL's information_schema output.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
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

        entity_class = (
            PostgresTable
            if entity_data["entity_class"] == Table
            else entity_data["entity_class"]
        )

        return {
            **entity_data,
            "attributes": table_attributes,
            "custom_attributes": table_custom_attributes,
            "entity_class": entity_class,
        }


class PostgresColumn(Column):
    """Custom column entity with Postgres-specific attributes.

    Adds precision radix, identity, and constraint type metadata
    specific to PostgreSQL columns.
    """

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
    """Custom transformer that registers Postgres-specific entity classes."""

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        super().__init__(connector_name, tenant_id, **kwargs)
        self.entity_class_definitions["TABLE"] = PostgresTable
        self.entity_class_definitions["COLUMN"] = PostgresColumn
        self.entity_class_definitions["EXTRAS-PROCEDURE"] = Procedure


class PostgresExtractorWithPyatlan(SqlMetadataExtractor):
    """PostgreSQL extractor with custom pyatlan-based transformation.

    Uses PostgresTable and PostgresColumn entity classes for
    Postgres-specific attribute mapping during the transform step.
    """

    fetch_database_sql = """
    SELECT d.*, d.datname as database_name FROM pg_database d WHERE datname = current_database();
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

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        return await super().fetch_databases(input)

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        return await super().fetch_schemas(input)

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        return await super().fetch_tables(input)

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        return await super().fetch_columns(input)

    @task(timeout_seconds=1800)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        """Transform using custom pyatlan entity classes."""
        return await super().transform_data(input)


class SampleSQLHandler(Handler):
    """Handler for the PostgreSQL connector with pyatlan transformer."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(
                    name="databaseSchemaCheck",
                    passed=True,
                    message="Schemas and Databases check successful",
                ),
                PreflightCheck(
                    name="tablesCheck",
                    passed=True,
                    message="Tables check successful",
                ),
            ],
        )

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[])


if __name__ == "__main__":
    asyncio.run(
        run_dev_combined(
            PostgresExtractorWithPyatlan,
            example_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/postgres/1728518400",
                },
                "credential_guid": "your-credential-guid",
            },
        )
    )
