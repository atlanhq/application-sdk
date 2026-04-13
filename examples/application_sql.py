"""SQL metadata extraction example using v3 SqlMetadataExtractor template.

Demonstrates how to build a PostgreSQL metadata extractor by subclassing
SqlMetadataExtractor with declarative EntityDef-driven orchestration.

Key components:
- PostgresExtractor: Single class with entity definitions and @task methods
- SampleSQLHandler: Handler for auth, preflight, and metadata endpoints
- SQLClient: Database connection configuration

Entity-driven orchestration:
- Declare entities via the ``entities`` class variable
- Entities in the same phase run concurrently
- Phase N+1 starts only after phase N completes
- For each entity, run() dispatches to fetch_{name}()

Usage:
    python examples/application_sql.py
"""

import asyncio
from typing import Any

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
)
from application_sdk.templates.entity import EntityDef

logger = get_logger(__name__)


class SQLClient(BaseSQLClient):
    """PostgreSQL connection configuration."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )


class PostgresExtractor(SqlMetadataExtractor):
    """PostgreSQL metadata extractor.

    Declares entities and implements fetch tasks with Postgres-specific SQL.
    The orchestration (run method) is inherited from SqlMetadataExtractor
    and dispatches to fetch_{name}() for each entity, grouped by phase.
    """

    entities = [
        EntityDef(name="databases", phase=1),
        EntityDef(name="schemas", phase=1),
        EntityDef(name="tables", phase=1),
        EntityDef(name="columns", phase=1),
    ]

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch PostgreSQL databases."""
        client = SQLClient(credentials={"host": "localhost", "port": 5432})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        async for batch in client.run_query(
            "SELECT datname AS database_name "
            "FROM pg_database WHERE datname = current_database()"
        ):
            rows.extend(batch)
        databases = [r["database_name"] for r in rows]
        return FetchDatabasesOutput(
            databases=databases,
            chunk_count=1,
            total_record_count=len(databases),
        )

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch PostgreSQL schemas."""
        client = SQLClient(credentials={"host": "localhost", "port": 5432})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        async for batch in client.run_query(
            "SELECT s.schema_name "
            "FROM information_schema.schemata s "
            "WHERE s.schema_name NOT LIKE 'pg_%' "
            "AND s.schema_name != 'information_schema'"
        ):
            rows.extend(batch)
        schemas = [r["schema_name"] for r in rows]
        return FetchSchemasOutput(
            schemas=schemas,
            chunk_count=1,
            total_record_count=len(schemas),
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch PostgreSQL tables."""
        client = SQLClient(credentials={"host": "localhost", "port": 5432})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        async for batch in client.run_query(
            "SELECT t.table_schema, t.table_name "
            "FROM information_schema.tables t "
            "WHERE t.table_schema NOT LIKE 'pg_%' "
            "AND t.table_schema != 'information_schema'"
        ):
            rows.extend(batch)
        tables = [f"{r['table_schema']}.{r['table_name']}" for r in rows]
        return FetchTablesOutput(
            tables=tables,
            chunk_count=1,
            total_record_count=len(tables),
        )

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch PostgreSQL columns."""
        client = SQLClient(credentials={"host": "localhost", "port": 5432})
        await client.load(credentials=client.credentials)
        rows: list[dict[str, Any]] = []
        async for batch in client.run_query(
            "SELECT c.table_schema, c.table_name, c.column_name, c.data_type "
            "FROM information_schema.columns c "
            "WHERE c.table_schema NOT LIKE 'pg_%' "
            "AND c.table_schema != 'information_schema'"
        ):
            rows.extend(batch)
        return FetchColumnsOutput(
            chunk_count=1,
            total_record_count=len(rows),
        )


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
