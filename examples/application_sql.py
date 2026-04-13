"""SQL metadata extraction example using v3 SqlMetadataExtractor template.

Demonstrates how to build a PostgreSQL metadata extractor by subclassing
SqlMetadataExtractor. In v3, the workflow + activities split collapses into
a single App class. Override only the @task methods you need to customize.

Key components:
- PostgresClient: Database connection with AUTH_STRATEGIES
- PostgresExtractor: Single class replacing both workflow and activities
- SampleSQLHandler: Handler for auth, preflight, and metadata endpoints

Extraction steps (orchestrated by SqlMetadataExtractor.run()):
1. Fetch databases
2. Fetch schemas
3. Fetch tables
4. Fetch columns
5. Transform metadata
6. Upload results to object store

Usage:
    python examples/application_sql.py
"""

import asyncio

from application_sdk.app import task
from application_sdk.clients.auth_strategies import BasicAuthStrategy
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import SQLClient
from application_sdk.credentials.types import BasicCredential
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
)

logger = get_logger(__name__)


class PostgresClient(SQLClient):
    """PostgreSQL connection configuration with auth strategy."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )

    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
    }


class PostgresExtractor(SqlMetadataExtractor):
    """PostgreSQL metadata extractor.

    Overrides fetch tasks with Postgres-specific SQL queries.
    The orchestration (run method) is inherited from SqlMetadataExtractor.
    """

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
        """Fetch PostgreSQL databases."""
        return await super().fetch_databases(input)

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch PostgreSQL schemas."""
        return await super().fetch_schemas(input)

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch PostgreSQL tables."""
        return await super().fetch_tables(input)

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch PostgreSQL columns."""
        return await super().fetch_columns(input)


class SampleSQLHandler(Handler):
    """Handler for the PostgreSQL connector."""

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
            PostgresExtractor,
            example_input={
                "connection": {
                    "connection_name": "test-connection",
                    "connection_qualified_name": "default/postgres/1728518400",
                },
                "credential_guid": "your-credential-guid",
            },
        )
    )
