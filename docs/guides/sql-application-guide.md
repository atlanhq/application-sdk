# Building SQL Applications with Application SDK v3

This guide walks through building a SQL metadata extraction connector using the v3 Application SDK. By the end, you will have a working PostgreSQL connector that extracts databases, schemas, tables, and columns.

v3 replaces the v2 `BaseSQLMetadataExtractionWorkflow` + `BaseSQLMetadataExtractionActivities` split with a single `SqlMetadataExtractor` class. If you are migrating an existing v2 connector, see the [Migration Guide](../migration-guide-v3.md) for a step-by-step checklist.

## Overview

A SQL connector built with v3 has four parts:

1. **App (extractor)** --- a `SqlMetadataExtractor` subclass with `@task` methods that fetch metadata from your database.
2. **Handler** --- a `Handler` subclass that implements HTTP endpoints for authentication, preflight checks, and metadata discovery.
3. **SQL Client** --- a `BaseSQLClient` subclass that defines the connection string template for your database dialect.
4. **Entry point** --- the `ATLAN_APP_MODULE` environment variable and `application-sdk` CLI that start your app.

The SDK orchestrates everything through Temporal workflows. You never import from `temporalio` directly --- the `@task` decorator handles activity registration, heartbeating, and retry configuration.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [Temporal](https://docs.temporal.io/) server (local or remote)
- [Dapr](https://docs.dapr.io/) runtime (for state and secret stores in production)

Install the SDK:

```bash
uv add application-sdk
```

Start local infrastructure (Temporal + Dapr sidecars):

```bash
uv run poe start-deps
```

## Project Structure

A v3 SQL connector follows this layout:

```
my-postgres-connector/
  app/
    __init__.py
    client.py          # BaseSQLClient subclass
    extractor.py       # SqlMetadataExtractor subclass
    handler.py         # Handler subclass
    generated/         # Pkl-generated contract JSON files
      ...
  tests/
    test_extractor.py
    test_handler.py
  Dockerfile
  pyproject.toml
```

## SQL Client

Extend `BaseSQLClient` to define the connection string template for your database. The SDK uses this template to build a SQLAlchemy connection URL.

```python
# app/client.py
from application_sdk.clients.sql import BaseSQLClient


class PostgresClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }
```

The `required` list declares which credential keys must be present. The SDK validates these at connection time and raises a clear error if any are missing.

## Handler

The `Handler` ABC defines three HTTP endpoints that the Atlan platform calls during connector setup and operation. Each method receives a typed input and returns a typed output.

```python
# app/handler.py
from application_sdk.handler import (
    Handler,
    AuthInput,
    AuthOutput,
    AuthStatus,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    MetadataInput,
    MetadataOutput,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from application_sdk.clients.sql import BaseSQLClient

from app.client import PostgresClient


class PostgresHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Verify that the provided credentials can connect to the database."""
        try:
            client = PostgresClient()
            creds = {c.key: c.value for c in input.credentials}
            await client.load(credentials=creds)
            # Run a simple connectivity check
            return AuthOutput(status=AuthStatus.SUCCESS, message="Connected")
        except Exception as e:
            return AuthOutput(status=AuthStatus.FAILED, message=str(e))

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run pre-extraction checks (connectivity, permissions, table counts)."""
        return PreflightOutput(status=PreflightStatus.READY, message="All checks passed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        """Return catalog/schema pairs for the Atlan UI filter tree."""
        client = PostgresClient()
        creds = {c.key: c.value for c in input.credentials}
        await client.load(credentials=creds)

        rows = await client.execute_query(
            "SELECT catalog_name AS TABLE_CATALOG, schema_name AS TABLE_SCHEMA "
            "FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' "
            "AND schema_name != 'information_schema'"
        )
        objects = [
            SqlMetadataObject(TABLE_CATALOG=r["TABLE_CATALOG"], TABLE_SCHEMA=r["TABLE_SCHEMA"])
            for r in rows
        ]
        return SqlMetadataOutput(objects=objects)
```

### Handler contracts

| Method | Input | Output | Purpose |
|--------|-------|--------|---------|
| `test_auth` | `AuthInput` | `AuthOutput` | Verify credentials work |
| `preflight_check` | `PreflightInput` | `PreflightOutput` | Check connectivity, permissions, table counts |
| `fetch_metadata` | `MetadataInput` | `MetadataOutput` | Return catalog/schema tree for UI filters |

`AuthOutput.status` uses `AuthStatus` (SUCCESS, FAILED, EXPIRED, INVALID_CREDENTIALS). `PreflightOutput.status` uses `PreflightStatus` (READY, NOT_READY, PARTIAL).

## App (Extractor)

The core of your connector is a `SqlMetadataExtractor` subclass. Override `@task` methods to implement the SQL queries that extract metadata from your database.

```python
# app/extractor.py
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesInput,
    FetchTablesOutput,
    FetchColumnsInput,
    FetchColumnsOutput,
)
from application_sdk.app import task
from application_sdk.observability.logger_adaptor import get_logger

from app.client import PostgresClient

logger = get_logger(__name__)


class PostgresExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch the current database name from PostgreSQL."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)

        rows = await client.execute_query(
            "SELECT datname AS database_name FROM pg_database WHERE datname = current_database()"
        )
        databases = [r["database_name"] for r in rows]

        return FetchDatabasesOutput(
            databases=databases,
            chunk_count=1,
            total_record_count=len(databases),
        )

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch non-system schemas, applying include/exclude filters."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)

        rows = await client.execute_query(f"""
            SELECT s.schema_name
            FROM information_schema.schemata s
            WHERE s.schema_name NOT LIKE 'pg_%'
              AND s.schema_name != 'information_schema'
              AND concat(s.catalog_name, '.', s.schema_name) !~ '{input.exclude_filter}'
              AND concat(s.catalog_name, '.', s.schema_name) ~ '{input.include_filter}'
        """)
        schemas = [r["schema_name"] for r in rows]

        return FetchSchemasOutput(
            schemas=schemas,
            chunk_count=1,
            total_record_count=len(schemas),
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch tables matching the configured filters."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)

        sql = f"""
            SELECT t.table_schema, t.table_name, t.table_type
            FROM information_schema.tables t
            WHERE concat(current_database(), '.', t.table_schema) !~ '{input.exclude_filter}'
              AND concat(current_database(), '.', t.table_schema) ~ '{input.include_filter}'
        """
        if input.temp_table_regex:
            sql += f" AND t.table_name !~ '{input.temp_table_regex}'"

        rows = await client.execute_query(sql)
        tables = [f"{r['table_schema']}.{r['table_name']}" for r in rows]

        return FetchTablesOutput(
            tables=tables,
            chunk_count=1,
            total_record_count=len(tables),
        )

    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch column metadata for all matching tables."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)

        sql = f"""
            SELECT c.table_schema, c.table_name, c.column_name,
                   c.data_type, c.ordinal_position, c.is_nullable
            FROM information_schema.columns c
            WHERE concat(current_database(), '.', c.table_schema) !~ '{input.exclude_filter}'
              AND concat(current_database(), '.', c.table_schema) ~ '{input.include_filter}'
        """
        if input.temp_table_regex:
            sql += f" AND c.table_name !~ '{input.temp_table_regex}'"

        rows = await client.execute_query(sql)

        return FetchColumnsOutput(
            chunk_count=1,
            total_record_count=len(rows),
        )
```

### What happens under the hood

Each `@task` method becomes a Temporal activity. The `SqlMetadataExtractor.run()` method (inherited from the base class) orchestrates the full extraction:

1. Fetch databases, schemas, tables, and columns **in parallel** via `asyncio.gather`
2. Aggregate counts into an `ExtractionOutput`
3. Return the result to Temporal

You can override `run()` to change the orchestration --- for example, to add a `fetch_views` step or to run tasks sequentially.

### Available task methods

| Method | Input | Output | Default behavior |
|--------|-------|--------|-----------------|
| `fetch_databases` | `FetchDatabasesInput` | `FetchDatabasesOutput` | Required --- raises `NotImplementedError` |
| `fetch_schemas` | `FetchSchemasInput` | `FetchSchemasOutput` | Required --- raises `NotImplementedError` |
| `fetch_tables` | `FetchTablesInput` | `FetchTablesOutput` | Required --- raises `NotImplementedError` |
| `fetch_columns` | `FetchColumnsInput` | `FetchColumnsOutput` | Required --- raises `NotImplementedError` |
| `fetch_procedures` | `FetchProceduresInput` | `FetchProceduresOutput` | Optional --- not called from default `run()` |
| `transform_data` | `TransformInput` | `TransformOutput` | Optional --- override for custom transformation |

### Adding custom tasks

Use `@task` to define additional extraction steps:

```python
from application_sdk.templates.contracts.sql_metadata import (
    FetchViewsInput,
    FetchViewsOutput,
)

class PostgresExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_views(self, input: FetchViewsInput) -> FetchViewsOutput:
        """Fetch views from PostgreSQL."""
        # ... implementation
        return FetchViewsOutput(chunk_count=1, total_record_count=len(views))

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        """Override run() to include views in the extraction."""
        # Call the base extraction
        result = await super().run(input)

        # Add views
        views_result = await self.fetch_views(
            FetchViewsInput(
                workflow_id=input.workflow_id,
                connection=input.connection,
                credential_ref=input.credential_ref,
            )
        )
        result.views_extracted = views_result.total_record_count
        return result
```

## Typed Contracts

v3 replaces `Dict[str, Any]` with Pydantic models for all task inputs and outputs. This provides:

- **Type safety** --- errors caught at import time, not at runtime
- **Payload validation** --- Temporal has a 2 MB payload limit; contracts forbid unbounded types (`Any`, bare `bytes`, unbounded `list`)
- **Self-documenting APIs** --- contract fields visible in Temporal UI and IDE autocompletion

### Contract hierarchy

All SQL metadata extraction contracts live in `application_sdk.templates.contracts.sql_metadata`:

```
ExtractionInput          -- top-level input to run()
ExtractionOutput         -- top-level output from run()
ExtractionTaskInput      -- shared fields for all per-task inputs
  FetchDatabasesInput
  FetchSchemasInput
  FetchTablesInput
  FetchColumnsInput
  FetchProceduresInput
  FetchViewsInput
  TransformInput
FetchDatabasesOutput
FetchSchemasOutput
FetchTablesOutput
FetchColumnsOutput
FetchProceduresOutput
FetchViewsOutput
TransformOutput
```

### Bounded collections

Unbounded `list` and `dict` are forbidden in contracts. Use `MaxItems` to declare an upper bound:

```python
from typing import Annotated
from pydantic import Field
from application_sdk.contracts.types import MaxItems

class FetchDatabasesOutput(Output):
    databases: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0
```

### FileReference for large data

When a task produces data too large for a Temporal payload, use `FileReference`. The SDK uploads it to object storage automatically:

```python
from application_sdk.contracts.types import FileReference

class FetchOutput(Output):
    results: FileReference  # automatically uploaded on task output

class ProcessInput(Input):
    results: FileReference  # automatically downloaded on task input
```

## Entry Point

v3 uses the `application-sdk` CLI to start your app. The `ATLAN_APP_MODULE` environment variable tells the CLI which class to load.

### For local development

```python
# main.py
import asyncio
from application_sdk.main import run_dev_combined
from app.extractor import PostgresExtractor
from app.handler import PostgresHandler

asyncio.run(run_dev_combined(PostgresExtractor, handler_class=PostgresHandler))
```

`run_dev_combined` starts both the Temporal worker and the HTTP handler in a single process.

### CLI modes

The `application-sdk` CLI supports three modes:

| Mode | What it runs | Use case |
|------|-------------|----------|
| `worker` | Temporal worker only | Production worker pods |
| `handler` | HTTP handler only | Production handler pods |
| `combined` | Worker + handler | Local dev, SDR (single-deploy runtime) |

```bash
# Local development
application-sdk --mode combined --app app.extractor:PostgresExtractor

# Production (separate pods)
application-sdk --mode worker
application-sdk --mode handler
```

In production, the `--app` flag is optional --- `ATLAN_APP_MODULE` is the recommended approach (see Dockerfile section).

## Dockerfile

```dockerfile
FROM ghcr.io/atlanhq/application-sdk:v3 AS base

WORKDIR /app

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY app/ app/

# Set the app module --- this is mandatory in production
ENV ATLAN_APP_MODULE=app.extractor:PostgresExtractor
ENV ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated

CMD ["application-sdk", "--mode", "combined"]
```

Key environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLAN_APP_MODULE` | Yes | Python import path to your `SqlMetadataExtractor` subclass (e.g., `app.extractor:PostgresExtractor`) |
| `ATLAN_CONTRACT_GENERATED_DIR` | No | Path to Pkl-generated contract JSON files (defaults to `/app/app/generated`) |

The entry point hard-fails at startup if `ATLAN_APP_MODULE` is not set.

## Testing

v3 provides in-memory mock implementations of infrastructure services so you can test without running Dapr or Temporal sidecars.

```python
# tests/test_extractor.py
import pytest
from application_sdk.testing.mocks import MockStateStore, MockSecretStore
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesInput,
    FetchDatabasesOutput,
)
from app.extractor import PostgresExtractor


@pytest.fixture
def state_store():
    return MockStateStore()


@pytest.fixture
def secret_store():
    return MockSecretStore()


@pytest.mark.asyncio
async def test_fetch_databases_contract():
    """Verify fetch_databases returns the correct contract shape."""
    # The task method can be called directly in tests
    # (Temporal decorators are no-ops outside the worker)
    extractor = PostgresExtractor()

    input = FetchDatabasesInput(
        workflow_id="test-workflow-1",
        credential_ref=None,
    )

    # In a real test, you would mock the SQL client
    # Here we verify the contract types are correct
    assert isinstance(input, FetchDatabasesInput)
    assert input.workflow_id == "test-workflow-1"
```

### Mock infrastructure

| Mock | Replaces | Purpose |
|------|----------|---------|
| `MockStateStore` | Dapr state store | In-memory key-value store with call tracking |
| `MockSecretStore` | Dapr secret store | In-memory secret store with call tracking |

Both mocks record every call for assertions:

```python
async def test_state_store_tracking():
    store = MockStateStore()
    await store.save("key", {"value": "data"})

    assert store.get_save_calls() == [("key", {"value": "data"})]
    result = await store.load("key")
    assert result == {"value": "data"}
```

## Putting It All Together

Here is a complete, minimal PostgreSQL connector using v3 patterns:

```python
# app/client.py
from application_sdk.clients.sql import BaseSQLClient


class PostgresClient(BaseSQLClient):
    DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
    }
```

```python
# app/handler.py
from application_sdk.handler import (
    Handler,
    AuthInput,
    AuthOutput,
    AuthStatus,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    MetadataInput,
    MetadataOutput,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from app.client import PostgresClient


class PostgresHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        try:
            client = PostgresClient()
            creds = {c.key: c.value for c in input.credentials}
            await client.load(credentials=creds)
            return AuthOutput(status=AuthStatus.SUCCESS, message="Connected")
        except Exception as e:
            return AuthOutput(status=AuthStatus.FAILED, message=str(e))

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="All checks passed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        client = PostgresClient()
        creds = {c.key: c.value for c in input.credentials}
        await client.load(credentials=creds)

        rows = await client.execute_query(
            "SELECT catalog_name AS TABLE_CATALOG, schema_name AS TABLE_SCHEMA "
            "FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' AND schema_name != 'information_schema'"
        )
        return SqlMetadataOutput(
            objects=[
                SqlMetadataObject(TABLE_CATALOG=r["TABLE_CATALOG"], TABLE_SCHEMA=r["TABLE_SCHEMA"])
                for r in rows
            ]
        )
```

```python
# app/extractor.py
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesInput, FetchDatabasesOutput,
    FetchSchemasInput, FetchSchemasOutput,
    FetchTablesInput, FetchTablesOutput,
    FetchColumnsInput, FetchColumnsOutput,
)
from application_sdk.app import task
from application_sdk.observability.logger_adaptor import get_logger
from app.client import PostgresClient

logger = get_logger(__name__)


class PostgresExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        rows = await client.execute_query(
            "SELECT datname AS database_name FROM pg_database WHERE datname = current_database()"
        )
        databases = [r["database_name"] for r in rows]
        return FetchDatabasesOutput(databases=databases, chunk_count=1, total_record_count=len(databases))

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        rows = await client.execute_query(f"""
            SELECT s.schema_name FROM information_schema.schemata s
            WHERE s.schema_name NOT LIKE 'pg_%' AND s.schema_name != 'information_schema'
              AND concat(s.catalog_name, '.', s.schema_name) !~ '{input.exclude_filter}'
              AND concat(s.catalog_name, '.', s.schema_name) ~ '{input.include_filter}'
        """)
        schemas = [r["schema_name"] for r in rows]
        return FetchSchemasOutput(schemas=schemas, chunk_count=1, total_record_count=len(schemas))

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        sql = f"""
            SELECT t.table_schema, t.table_name, t.table_type
            FROM information_schema.tables t
            WHERE concat(current_database(), '.', t.table_schema) !~ '{input.exclude_filter}'
              AND concat(current_database(), '.', t.table_schema) ~ '{input.include_filter}'
        """
        if input.temp_table_regex:
            sql += f" AND t.table_name !~ '{input.temp_table_regex}'"
        rows = await client.execute_query(sql)
        tables = [f"{r['table_schema']}.{r['table_name']}" for r in rows]
        return FetchTablesOutput(tables=tables, chunk_count=1, total_record_count=len(tables))

    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        sql = f"""
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type, c.ordinal_position
            FROM information_schema.columns c
            WHERE concat(current_database(), '.', c.table_schema) !~ '{input.exclude_filter}'
              AND concat(current_database(), '.', c.table_schema) ~ '{input.include_filter}'
        """
        if input.temp_table_regex:
            sql += f" AND c.table_name !~ '{input.temp_table_regex}'"
        rows = await client.execute_query(sql)
        return FetchColumnsOutput(chunk_count=1, total_record_count=len(rows))
```

```python
# main.py
import asyncio
from application_sdk.main import run_dev_combined
from app.extractor import PostgresExtractor
from app.handler import PostgresHandler

if __name__ == "__main__":
    asyncio.run(run_dev_combined(PostgresExtractor, handler_class=PostgresHandler))
```

```dockerfile
# Dockerfile
FROM ghcr.io/atlanhq/application-sdk:v3 AS base
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app/ app/
ENV ATLAN_APP_MODULE=app.extractor:PostgresExtractor
ENV ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated
CMD ["application-sdk", "--mode", "combined"]
```

## Best Practices

1. **Use typed contracts.** Never pass `Dict[str, Any]` between tasks. The SDK validates contract fields at import time and catches payload safety issues before your app runs.
2. **Keep tasks focused.** Each `@task` method should do one thing --- fetch databases, fetch schemas, etc. The `run()` method handles orchestration.
3. **Use `FileReference` for large data.** If a task produces output larger than ~1 MB, store it in object storage via `FileReference` rather than passing it through Temporal.
4. **Load credentials via `credential_ref`.** Use `input.credential_ref` (the typed `CredentialRef`) rather than the legacy `credential_guid` string.
5. **Log with the SDK logger.** Use `application_sdk.observability.logger_adaptor.get_logger` for structured logging that integrates with Temporal.
6. **Test without sidecars.** Use `MockStateStore` and `MockSecretStore` from `application_sdk.testing.mocks` to test your extractor and handler without Dapr or Temporal running.
7. **Set `ATLAN_APP_MODULE` in the Dockerfile.** This locks the app module to the image and avoids runtime misconfiguration.

## Next Steps

- [What's New in v3](../whats-new-v3.md) --- detailed comparison of v2 and v3 patterns
- [Migration Guide](../migration-guide-v3.md) --- step-by-step migration from v2 to v3
- [Getting Started](getting-started.md) --- development environment setup
- [Architecture](architecture.md) --- SDK architecture and component overview
