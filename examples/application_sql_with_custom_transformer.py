"""SQL metadata extraction with custom YAML-based transformer (v3 pattern).

Demonstrates how to customize metadata transformation using YAML query
templates with SqlMetadataExtractor. In v3, the transformer is configured
via the asset_mapper pattern on the extractor class.

Key differences from the base SQL example:
- Custom YAML templates override default transformation for specific asset types
- Templates are loaded from a local directory (examples/sql_query_templates/)

Usage:
    python examples/application_sql_with_custom_transformer.py
"""

import asyncio

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

logger = get_logger(__name__)


class SQLClient(BaseSQLClient):
    """PostgreSQL connection configuration."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )


class PostgresExtractorWithCustomTransformer(SqlMetadataExtractor):
    """PostgreSQL extractor with custom YAML-based transformation.

    The custom_templates_path points to YAML files that override the
    default transformation for specific asset types (e.g., DATABASE).
    See examples/sql_query_templates/database.yaml for the template format.
    """

    sql_client_class = SQLClient

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
        """Transform extracted metadata using custom YAML templates.

        The custom templates at examples/sql_query_templates/ override the
        default transformation for DATABASE, TABLE, COLUMN, SCHEMA, etc.
        """
        return await super().transform_data(input)


class SampleSQLHandler(Handler):
    """Handler for the PostgreSQL connector with custom transformer."""

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
            PostgresExtractorWithCustomTransformer,
            example_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/postgres/1728518400",
                },
                "credential_guid": "your-credential-guid",
            },
        )
    )
