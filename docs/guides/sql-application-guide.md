# Building SQL Applications with Application SDK v3

This guide walks through building a SQL metadata extraction connector using the v3 Application SDK. By the end, you will have a working connector that extracts databases, schemas, tables, and columns, transforms them using an asset mapper, and publishes the results.

v3 replaces the v2 `BaseSQLMetadataExtractionWorkflow` + `BaseSQLMetadataExtractionActivities` split with a single `SqlMetadataExtractor` class. If you are migrating an existing v2 connector, see the [Migration Guide](../migration-guide-v3.md) for a step-by-step checklist.

## Overview

A v3 SQL connector has five parts:

1. **App (extractor)** --- a `SqlMetadataExtractor` subclass with `@task` methods that fetch and transform metadata.
2. **Handler** --- a `Handler` subclass that implements HTTP endpoints for authentication, preflight checks, and metadata discovery.
3. **Client** --- a client class (e.g., `BaseSQLClient` subclass or custom `ClientInterface` implementation) that manages the database connection.
4. **Asset mapper** --- Python functions that map raw extraction results to pyatlan entity objects (replaces YAML transformers).
5. **Contracts** --- typed Pydantic models for credentials, configuration, and task inputs.

The SDK orchestrates everything through Temporal workflows. You never import from `temporalio` directly --- the `@task` decorator handles activity registration, heartbeating, and retry configuration.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [Temporal](https://docs.temporal.io/) server (local or remote)
- [Dapr](https://docs.dapr.io/) runtime (for state and secret stores in production)

Install the SDK:

```bash
uv add atlan-application-sdk
```

Start local infrastructure (Temporal + Dapr sidecars):

```bash
uv run poe start-deps
```

## Project Structure

A v3 SQL connector follows this layout:

```
my-connector/
  app/
    __init__.py
    clients.py          # Client class (BaseSQLClient or ClientInterface)
    contracts.py        # Typed Pydantic models (credentials, config, task inputs)
    handler.py          # Handler subclass
    asset_mapper.py     # Python mapper functions (raw rows → pyatlan entities)
    connector.py        # SqlMetadataExtractor subclass (fetch + transform tasks)
    constants.py        # Connector-specific constants
    sql/                # SQL query files (*.sql)
    transformers/       # Additional transformation logic (optional)
    generated/          # Pkl-generated contract JSON files
      ...
  tests/
    test_connector.py
    test_handler.py
  main.py              # Local dev entry point (run_dev_combined)
  Dockerfile
  pyproject.toml
  atlan.yaml           # App manifest (app_id, execution_mode, Dapr config)
```

## Contracts

Define typed Pydantic models for your connector's credentials and configuration. This replaces the v2 pattern of passing `Dict[str, Any]` everywhere.

```python
# app/contracts.py
from pydantic import BaseModel
from application_sdk.templates.contracts.sql_metadata import ExtractionTaskInput


class MyCredential(BaseModel):
    """Normalize credentials from both handler (list of key-value pairs) and
    workflow (dict) formats into a single typed model."""
    host: str
    port: int = 5432
    username: str
    password: str
    database: str

    @classmethod
    def from_list(cls, credentials: list) -> "MyCredential":
        """Parse from handler's AuthInput.credentials (list of {key, value} dicts)."""
        creds = {c.key: c.value for c in credentials}
        return cls(**creds)

    @classmethod
    def from_dict(cls, credentials: dict) -> "MyCredential":
        """Parse from workflow credential dict."""
        return cls(**credentials)

    def to_dict(self) -> dict:
        return self.model_dump()


class MetadataConfig(BaseModel):
    """Connector-specific configuration parsed from the workflow input."""
    include_filter: str = ".*"
    exclude_filter: str = ""
    temp_table_regex: str = ""
    exclude_views: bool = False


class MyFetchInput(ExtractionTaskInput):
    """Extended task input with connector-specific config."""
    metadata_config: MetadataConfig = MetadataConfig()
```

## Client

Extend `BaseSQLClient` to define the connection string template for your database. The SDK uses this template to build a SQLAlchemy connection URL.

```python
# app/clients.py
from application_sdk.clients.sql import BaseSQLClient, DatabaseConfig


class PostgresClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )
```

The `required` list declares which credential keys must be present. The SDK validates these at connection time and raises a clear error if any are missing.

## Handler

The `Handler` ABC defines three HTTP endpoints that the Atlan platform calls during connector setup and operation. Each method receives a typed input and returns a typed output. Use your typed credential model to normalize the raw credential payload.

```python
# app/handler.py
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
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
from app.clients import PostgresClient
from app.contracts import MyCredential


class PostgresHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        """Verify that the provided credentials can connect to the database."""
        try:
            cred = MyCredential.from_list(input.credentials)
            client = PostgresClient()
            await client.load(credentials=cred.to_dict())
            return AuthOutput(status=AuthStatus.SUCCESS, message="Connected")
        except Exception as e:
            return AuthOutput(status=AuthStatus.FAILED, message=str(e))

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Run pre-extraction checks (connectivity, permissions, table counts)."""
        return PreflightOutput(status=PreflightStatus.READY, message="All checks passed")

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        """Return catalog/schema pairs for the Atlan UI filter tree."""
        cred = MyCredential.from_list(input.credentials)
        client = PostgresClient()
        await client.load(credentials=cred.to_dict())

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
| `fetch_metadata` | `MetadataInput` | `SqlMetadataOutput` | Return catalog/schema tree for UI filters |

`AuthOutput.status` uses `AuthStatus` (SUCCESS, FAILED, EXPIRED, INVALID_CREDENTIALS). `PreflightOutput.status` uses `PreflightStatus` (READY, NOT_READY, PARTIAL).

## Asset Mapper

v3 uses Python mapper functions to transform raw extraction results into pyatlan entity objects. This replaces the v2 YAML-based `AtlasTransformer` / `QueryBasedTransformer` approach with direct, testable Python code.

```python
# app/asset_mapper.py
from pyatlan.model.assets import Database, Schema, Table, Column


def map_database(row: dict, connection_qualified_name: str) -> Database:
    """Map a raw database row to a pyatlan Database entity."""
    return Database(
        name=row["database_name"],
        qualified_name=f"{connection_qualified_name}/{row['database_name']}",
        connection_qualified_name=connection_qualified_name,
    )


def map_schema(row: dict, connection_qualified_name: str, database_name: str) -> Schema:
    return Schema(
        name=row["schema_name"],
        qualified_name=f"{connection_qualified_name}/{database_name}/{row['schema_name']}",
        connection_qualified_name=connection_qualified_name,
        database_qualified_name=f"{connection_qualified_name}/{database_name}",
    )


def map_table(row: dict, connection_qualified_name: str) -> Table:
    qn = f"{connection_qualified_name}/{row['table_schema']}/{row['table_name']}"
    return Table(
        name=row["table_name"],
        qualified_name=qn,
        connection_qualified_name=connection_qualified_name,
        schema_qualified_name=f"{connection_qualified_name}/{row['table_schema']}",
    )


def map_column(row: dict, connection_qualified_name: str) -> Column:
    table_qn = f"{connection_qualified_name}/{row['table_schema']}/{row['table_name']}"
    return Column(
        name=row["column_name"],
        qualified_name=f"{table_qn}/{row['column_name']}",
        connection_qualified_name=connection_qualified_name,
        table_qualified_name=table_qn,
        data_type=row.get("data_type", ""),
        order=row.get("ordinal_position", 0),
    )


def serialize_entity(entity) -> dict:
    """Convert a pyatlan entity to Atlas nested-entity dict format for publishing."""
    return {
        "typeName": entity.type_name,
        "attributes": entity.attributes.dict(exclude_none=True),
    }
```

Each mapper function is a pure function: easy to unit test, no framework dependencies, no YAML files to maintain. The extractor calls these in its `transform` task (see below).

## App (Extractor)

The core of your connector is a `SqlMetadataExtractor` subclass. Declare which entities to extract using `ExtractableEntity`, then implement `@task` methods for each. Each entity's `task_name` maps directly to a method on the class --- no naming-convention magic. The base `run()` method handles orchestration automatically --- no need to override it.

```python
# app/connector.py
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.entity import ExtractableEntity
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesOutput,
    FetchSchemasOutput,
    FetchTablesOutput,
    FetchColumnsOutput,
)
from application_sdk.app import task
from application_sdk.observability.logger_adaptor import get_logger

from app.clients import PostgresClient
from app.contracts import MyFetchInput

logger = get_logger(__name__)


class PostgresApp(SqlMetadataExtractor):
    # Declare entities --- grouped by phase, concurrent within each phase.
    # Each task_name maps directly to the method that fetches this entity.
    entities = [
        ExtractableEntity(task_name="fetch_databases", phase=1),
        ExtractableEntity(task_name="fetch_schemas",   phase=1),
        ExtractableEntity(task_name="fetch_tables",    phase=1),
        ExtractableEntity(task_name="fetch_columns",   phase=1),
    ]

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: MyFetchInput) -> FetchDatabasesOutput:
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
    async def fetch_schemas(self, input: MyFetchInput) -> FetchSchemasOutput:
        """Fetch non-system schemas, applying include/exclude filters."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        cfg = input.metadata_config

        rows = await client.execute_query(f"""
            SELECT s.schema_name
            FROM information_schema.schemata s
            WHERE s.schema_name NOT LIKE 'pg_%'
              AND s.schema_name != 'information_schema'
              AND concat(s.catalog_name, '.', s.schema_name) !~ '{cfg.exclude_filter}'
              AND concat(s.catalog_name, '.', s.schema_name) ~ '{cfg.include_filter}'
        """)
        schemas = [r["schema_name"] for r in rows]

        return FetchSchemasOutput(
            schemas=schemas,
            chunk_count=1,
            total_record_count=len(schemas),
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: MyFetchInput) -> FetchTablesOutput:
        """Fetch tables matching the configured filters."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        cfg = input.metadata_config

        sql = f"""
            SELECT t.table_schema, t.table_name, t.table_type
            FROM information_schema.tables t
            WHERE concat(current_database(), '.', t.table_schema) !~ '{cfg.exclude_filter}'
              AND concat(current_database(), '.', t.table_schema) ~ '{cfg.include_filter}'
        """
        if cfg.temp_table_regex:
            sql += f" AND t.table_name !~ '{cfg.temp_table_regex}'"

        rows = await client.execute_query(sql)
        tables = [f"{r['table_schema']}.{r['table_name']}" for r in rows]

        return FetchTablesOutput(
            tables=tables,
            chunk_count=1,
            total_record_count=len(tables),
        )

    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: MyFetchInput) -> FetchColumnsOutput:
        """Fetch column metadata for all matching tables."""
        client = PostgresClient()
        await client.load(credential_ref=input.credential_ref)
        cfg = input.metadata_config

        sql = f"""
            SELECT c.table_schema, c.table_name, c.column_name,
                   c.data_type, c.ordinal_position, c.is_nullable
            FROM information_schema.columns c
            WHERE concat(current_database(), '.', c.table_schema) !~ '{cfg.exclude_filter}'
              AND concat(current_database(), '.', c.table_schema) ~ '{cfg.include_filter}'
        """
        if cfg.temp_table_regex:
            sql += f" AND c.table_name !~ '{cfg.temp_table_regex}'"

        rows = await client.execute_query(sql)

        return FetchColumnsOutput(
            chunk_count=1,
            total_record_count=len(rows),
        )
```

### What happens under the hood

Each `@task` method becomes a Temporal activity. The `entities` list drives orchestration:

1. Entities are grouped by `phase`. All entities in the same phase run **concurrently** via `asyncio.gather`.
2. Phase N+1 starts only after all phase N entities complete.
3. For each entity, `run()` dispatches to the method named by `task_name` (e.g. `ExtractableEntity(task_name="fetch_databases")` calls `fetch_databases()`).
4. After all entities complete, results are uploaded to Atlan.
5. Any result key matching an `ExtractionOutput` field (e.g. `databases_extracted`) is populated automatically. Unmatched keys are logged as warnings.
6. If one entity in a phase fails, sibling entities still run to completion before the error is raised.

If `entities` is not set (empty list), the extractor falls back to the default 4 entities: fetch_databases, fetch_schemas, fetch_tables, fetch_columns.

### Available task methods

| Method | Input | Output | Default behavior |
|--------|-------|--------|-----------------|
| `fetch_databases` | `FetchDatabasesInput` | `FetchDatabasesOutput` | Required --- raises `NotImplementedError` |
| `fetch_schemas` | `FetchSchemasInput` | `FetchSchemasOutput` | Required --- raises `NotImplementedError` |
| `fetch_tables` | `FetchTablesInput` | `FetchTablesOutput` | Required --- raises `NotImplementedError` |
| `fetch_columns` | `FetchColumnsInput` | `FetchColumnsOutput` | Required --- raises `NotImplementedError` |
| `fetch_procedures` | `FetchProceduresInput` | `FetchProceduresOutput` | Optional --- add `ExtractableEntity(task_name="fetch_procedures")` |
| `fetch_views` | `FetchViewsInput` | `FetchViewsOutput` | Optional --- add `ExtractableEntity(task_name="fetch_views")` |
| `transform_data` | `TransformInput` | `TransformOutput` | Override to map raw results via asset mapper |

### Adding custom entities

Add new entity types by extending the `entities` list and implementing the method named by `task_name`. No need to override `run()`.

```python
from application_sdk.templates.entity import ExtractableEntity
from application_sdk.templates.contracts.sql_metadata import FetchViewsOutput


class SnowflakeApp(SqlMetadataExtractor):
    entities = [
        # Phase 1: core entities (run in parallel)
        ExtractableEntity(task_name="fetch_databases", phase=1),
        ExtractableEntity(task_name="fetch_schemas",   phase=1),
        ExtractableEntity(task_name="fetch_tables",    phase=1),
        ExtractableEntity(task_name="fetch_columns",   phase=1),
        # Phase 2: connector-specific entities (run after phase 1)
        ExtractableEntity(task_name="fetch_stages",  phase=2),
        ExtractableEntity(task_name="fetch_streams", phase=2),
        # Disabled entity (skipped at runtime)
        ExtractableEntity(task_name="fetch_views", phase=1, enabled=False),
    ]

    @task(timeout_seconds=1800)
    async def fetch_stages(self, input) -> FetchDatabasesOutput:
        """Fetch Snowflake stages."""
        # ... implementation
        return FetchDatabasesOutput(total_record_count=len(stages))

    @task(timeout_seconds=1800)
    async def fetch_streams(self, input) -> FetchDatabasesOutput:
        """Fetch Snowflake streams."""
        # ... implementation
        return FetchDatabasesOutput(total_record_count=len(streams))
```

### ExtractableEntity fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `task_name` | `str` | (required) | Exact method name to call (e.g. `"fetch_databases"`) |
| `phase` | `int` | `1` | Execution phase --- entities in the same phase run concurrently |
| `enabled` | `bool` | `True` | Set to `False` to skip this entity |
| `timeout_seconds` | `int` | `1800` | Task timeout for this entity |
| `result_key` | `str` | `""` | Key in `ExtractionOutput` for the count (defaults to `{base}_extracted` where base is task_name minus `fetch_` prefix) |
| `depends_on` | `tuple[str, ...]` | `()` | Task names that must complete before this one (reserved for future use) |

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
from app.connector import PostgresApp

asyncio.run(run_dev_combined(PostgresApp))
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
application-sdk --mode combined --app app.connector:PostgresApp

# Production (separate pods)
application-sdk --mode worker
application-sdk --mode handler
```

In production, the `--app` flag is optional --- `ATLAN_APP_MODULE` is the recommended approach (see Dockerfile section).

## Dockerfile

The base image handles the entrypoint, Dapr, and the `application-sdk` CLI. You only set your app module and copy your code. No `ENTRYPOINT`, custom `entrypoint.sh`, or `CMD` is needed. The base image handles mode selection at runtime.

```dockerfile
# Application-sdk v3 base image (Chainguard-based)
FROM registry.atlan.com/public/app-runtime-base:refactor-v3-latest

WORKDIR /app

# Install dependencies first (better caching)
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

# Copy application code
COPY --chown=appuser:appuser . .

# App-specific environment variables
ENV ATLAN_APP_HTTP_PORT=8000
ENV ATLAN_APP_MODULE=app.connector:PostgresApp
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated
```

Key points:

- **Base image**: `registry.atlan.com/public/app-runtime-base:refactor-v3-latest` --- includes Dapr, the `application-sdk` CLI, and the entrypoint.
- **No `CMD` needed**: The base image handles mode selection at runtime.
- **`COPY . .`**: Copies the entire project (including `app/`, `main.py`, SQL files, etc.). The `.dockerignore` should exclude `.git`, `tests/`, etc.
- **`--no-install-project`**: Installs only dependencies, not the project itself (the app code is copied separately).
- **`ATLAN_CONTRACT_GENERATED_DIR`**: Relative path --- the base image `WORKDIR` is `/app`.

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLAN_APP_MODULE` | Yes | Python import path to your `App` subclass (e.g., `app.connector:PostgresApp`) |
| `ATLAN_APP_HTTP_PORT` | Recommended | HTTP port for the handler service (default: `8000`) |
| `ATLAN_CONTRACT_GENERATED_DIR` | Recommended | Path to Pkl-generated contract JSON files (default: `app/generated`) |

The base image entrypoint hard-fails at startup if `ATLAN_APP_MODULE` is not set.

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
from app.connector import PostgresApp


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
    extractor = PostgresApp()

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

Here is a complete, minimal connector using v3 patterns.

```python
# main.py
import asyncio
from application_sdk.main import run_dev_combined
from app.connector import PostgresApp

async def main():
    await run_dev_combined(PostgresApp)

if __name__ == "__main__":
    asyncio.run(main())
```

```dockerfile
# Dockerfile
FROM registry.atlan.com/public/app-runtime-base:refactor-v3-latest

WORKDIR /app

COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

COPY --chown=appuser:appuser . .

ENV ATLAN_APP_HTTP_PORT=8000
ENV ATLAN_APP_MODULE=app.connector:PostgresApp
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated
```

```yaml
# atlan.yaml
app_id: postgres
execution_mode: native
splitDeploymentEnabled: true
dapr:
  objectstore:
    enabled: true
  secretstore:
    enabled: true
```

## Best Practices

1. **Use typed contracts.** Never pass `Dict[str, Any]` between tasks. Define Pydantic models for credentials, config, and task inputs (see `contracts.py`).
2. **Use asset mappers for transformation.** Write pure Python mapper functions (raw rows → pyatlan entities) instead of YAML transformers. They are testable and explicit.
3. **Keep tasks focused.** Each `@task` method should do one thing --- fetch databases, fetch schemas, transform, etc. The `run()` method handles orchestration.
4. **Use `FileReference` for large data.** If a task produces output larger than ~1 MB, store it in object storage via `FileReference` rather than passing it through Temporal.
5. **Load credentials via `credential_ref`.** Use `input.credential_ref` (the typed `CredentialRef`) in `@task` methods. In handlers, use typed credential models to normalize `input.credentials`.
6. **Log with the SDK logger.** Use `application_sdk.observability.logger_adaptor.get_logger` for structured logging that integrates with Temporal.
7. **Test without sidecars.** Use `MockStateStore` and `MockSecretStore` from `application_sdk.testing.mocks` to test your connector and handler without Dapr or Temporal running.
8. **Set `ATLAN_APP_MODULE` in the Dockerfile.** This locks the app module to the image and avoids runtime misconfiguration.
9. **Use `on_complete` for cleanup.** Override `on_complete(success: bool)` for post-run cleanup (see [Migration Guide Step 12](../migration-guide-v3.md#step-12-app-lifecycle-hooks)).

## Next Steps

- [What's New in v3](../whats-new-v3.md) --- detailed comparison of v2 and v3 patterns
- [Migration Guide](../migration-guide-v3.md) --- step-by-step migration from v2 to v3
- [Getting Started](getting-started.md) --- development environment setup
- [Architecture](architecture.md) --- SDK architecture and component overview
