# Building SQL Applications with Application SDK v3

This guide walks through building a SQL metadata extraction connector using the v3 Application SDK. By the end, you will have a working connector that extracts databases, schemas, tables, and columns, transforms them using an asset mapper, and publishes the results.

v3 replaces the v2 `BaseSQLMetadataExtractionWorkflow` + `BaseSQLMetadataExtractionActivities` split with a single `SqlMetadataExtractor` class. If you are upgrading an existing v2 connector, see the [Upgrade Guide](../upgrade-guide-v3.md) for a step-by-step checklist.

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
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.models import DatabaseConfig


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

        rows = []
        async for batch in client.run_query(
            "SELECT catalog_name AS table_catalog, schema_name AS table_schema "
            "FROM information_schema.schemata "
            "WHERE schema_name NOT LIKE 'pg_%' "
            "AND schema_name != 'information_schema'"
        ):
            rows.extend(batch)
        # BaseSQLClient.run_query lowercases all column names; use lowercase keys here.
        objects = [
            SqlMetadataObject(TABLE_CATALOG=r["table_catalog"], TABLE_SCHEMA=r["table_schema"])
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

> **Note:** The examples below import from the legacy `pyatlan` package (consistent with the
> built-in `AtlasTransformer` transformers, which still depend on `pyatlan`). If you are
> building a new connector that uses the `pyatlan_v9` client elsewhere, prefer
> `from pyatlan_v9.model.assets import ...` and use the v9 serialisation API instead of
> `.dict()`.

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

The core of your connector is a `SqlMetadataExtractor` subclass. Override `@task` methods for fetching metadata and transforming it via asset mappers. The `run()` method orchestrates the full pipeline.

```python
# app/connector.py
import asyncio
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
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

from app.clients import PostgresClient
from app.contracts import MyCredential, MetadataConfig, MyFetchInput

logger = get_logger(__name__)


def _make_task_input(cls, src: ExtractionInput, **extra):
    """Build a typed task input from the top-level extraction input."""
    return cls(
        workflow_id=src.workflow_id,
        connection=src.connection,
        credential_guid=src.credential_guid,
        credential_ref=src.credential_ref,
        output_prefix=src.output_prefix,
        output_path=src.output_path,
        exclude_filter=src.exclude_filter,
        include_filter=src.include_filter,
        temp_table_regex=src.temp_table_regex,
        **extra,
    )


class PostgresApp(SqlMetadataExtractor):
    sql_client_class = PostgresClient  # enables default credential loading via super()

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: MyFetchInput) -> FetchDatabasesOutput:
        """Fetch the current database name from PostgreSQL."""
        client = await self._load_sql_client(input)  # resolves credentials via context

        rows = []
        async for batch in client.run_query(
            "SELECT datname AS database_name FROM pg_database WHERE datname = current_database()"
        ):
            rows.extend(batch)

        databases = [r["database_name"] for r in rows]
        return FetchDatabasesOutput(
            databases=databases,
            chunk_count=1,
            total_record_count=len(databases),
        )

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: MyFetchInput) -> FetchSchemasOutput:
        """Fetch non-system schemas."""
        client = await self._load_sql_client(input)

        rows = []
        async for batch in client.run_query("""
            SELECT s.schema_name
            FROM information_schema.schemata s
            WHERE s.schema_name NOT LIKE 'pg_%'
              AND s.schema_name != 'information_schema'
        """):
            rows.extend(batch)

        schemas = [r["schema_name"] for r in rows]
        return FetchSchemasOutput(
            schemas=schemas,
            chunk_count=1,
            total_record_count=len(schemas),
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: MyFetchInput) -> FetchTablesOutput:
        """Fetch tables from information_schema."""
        client = await self._load_sql_client(input)

        rows = []
        async for batch in client.run_query("""
            SELECT t.table_schema, t.table_name
            FROM information_schema.tables t
            WHERE t.table_type = 'BASE TABLE'
        """):
            rows.extend(batch)

        tables = [f"{r['table_schema']}.{r['table_name']}" for r in rows]
        return FetchTablesOutput(
            tables=tables,
            chunk_count=1,
            total_record_count=len(tables),
        )

    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: MyFetchInput) -> FetchColumnsOutput:
        """Fetch column metadata."""
        client = await self._load_sql_client(input)

        count = 0
        async for batch in client.run_query("""
            SELECT c.table_schema, c.table_name, c.column_name,
                   c.data_type, c.ordinal_position
            FROM information_schema.columns c
        """):
            count += len(batch)

        return FetchColumnsOutput(chunk_count=1, total_record_count=count)

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        """Orchestrate the full extraction pipeline."""
        task_input = _make_task_input(MyFetchInput, input)

        db_result, schema_result, table_result, col_result = await asyncio.gather(
            self.fetch_databases(task_input),
            self.fetch_schemas(task_input),
            self.fetch_tables(task_input),
            self.fetch_columns(task_input),
        )

        return ExtractionOutput(
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=col_result.total_record_count,
        )
```

### What happens under the hood

Each `@task` method becomes a Temporal activity. The `run()` method orchestrates the full pipeline:

1. **Fetch phase** --- fetch databases, schemas, tables, and columns **in parallel** via `asyncio.gather`
2. **Transform phase** --- map raw results to pyatlan entities using the asset mapper
3. **Return** --- aggregate counts into an `ExtractionOutput`

### Available task methods

| Method | Input | Output | Default behavior |
|--------|-------|--------|-----------------|
| `fetch_databases` | `FetchDatabasesInput` | `FetchDatabasesOutput` | Default uses `sql_client_class` + `fetch_database_sql`; raises `NotImplementedError` if those are unset |
| `fetch_schemas` | `FetchSchemasInput` | `FetchSchemasOutput` | Default uses `sql_client_class` + `fetch_schema_sql`; raises `NotImplementedError` if those are unset |
| `fetch_tables` | `FetchTablesInput` | `FetchTablesOutput` | Default uses `sql_client_class` + `fetch_table_sql`; raises `NotImplementedError` if those are unset |
| `fetch_columns` | `FetchColumnsInput` | `FetchColumnsOutput` | Default uses `sql_client_class` + `fetch_column_sql`; raises `NotImplementedError` if those are unset |
| `fetch_views` | `FetchViewsInput` | `FetchViewsOutput` | Optional --- add for databases with views |
| `transform_data` | `TransformInput` | `TransformOutput` | Override to map raw results via asset mapper |

### Adding custom tasks

Use `@task` to define additional extraction steps and override `run()` to include them:

```python
from application_sdk.templates.contracts.sql_metadata import (
    FetchViewsInput,
    FetchViewsOutput,
)

class PostgresApp(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_views(self, input: MyFetchInput) -> FetchViewsOutput:
        """Fetch views from PostgreSQL."""
        # ... implementation
        return FetchViewsOutput(chunk_count=1, total_record_count=len(views))

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        """Override run() to include views in the extraction."""
        result = await super().run(input)

        views_result = await self.fetch_views(_make_task_input(MyFetchInput, input))
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
  FetchDatabasesInput    -- passed to fetch_databases()
  FetchSchemasInput      -- passed to fetch_schemas()
  FetchTablesInput       -- passed to fetch_tables()
  FetchColumnsInput      -- passed to fetch_columns()
  FetchProceduresInput   -- passed to fetch_procedures()
  FetchViewsInput        -- passed to fetch_views()
  TransformInput         -- passed to transform(); carries typename, file_names, chunk_start
FetchDatabasesOutput     -- databases: list[str], chunk_count, total_record_count
FetchSchemasOutput       -- schemas: list[str], chunk_count, total_record_count
FetchTablesOutput        -- tables: list[str], chunk_count, total_record_count
FetchColumnsOutput       -- chunk_count, total_record_count
FetchProceduresOutput    -- chunk_count, total_record_count
FetchViewsOutput         -- chunk_count, total_record_count
TransformOutput          -- typename, total_record_count, chunk_count
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
FROM registry.atlan.com/public/app-runtime-base:3

WORKDIR /app

# Install dependencies first (better caching)
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

# Copy application code
COPY --chown=appuser:appuser . .

# App-specific environment variables
ENV ATLAN_HANDLER_PORT=8000
ENV ATLAN_APP_MODULE=app.connector:PostgresApp
ENV ATLAN_CONTRACT_GENERATED_DIR=app/generated
```

Key points:

- **Base image**: `registry.atlan.com/public/app-runtime-base:3` --- includes Dapr, the `application-sdk` CLI, and the entrypoint.
- **No `CMD` needed**: The base image handles mode selection at runtime.
- **`COPY . .`**: Copies the entire project (including `app/`, `main.py`, SQL files, etc.). The `.dockerignore` should exclude `.git`, `tests/`, etc.
- **`--no-install-project`**: Installs only dependencies, not the project itself (the app code is copied separately).
- **`ATLAN_CONTRACT_GENERATED_DIR`**: Relative path --- the base image `WORKDIR` is `/app`.

| Variable | Required | Description |
|----------|----------|-------------|
| `ATLAN_APP_MODULE` | Yes | Python import path to your `App` subclass (e.g., `app.connector:PostgresApp`) |
| `ATLAN_HANDLER_PORT` | Recommended | HTTP port for the handler service (default: `8000`). Fallback: `ATLAN_APP_HTTP_PORT`. |
| `ATLAN_CONTRACT_GENERATED_DIR` | Recommended | Path to Pkl-generated contract JSON files (default: `app/generated`) |

The base image entrypoint hard-fails at startup if `ATLAN_APP_MODULE` is not set.

## Testing

v3 provides in-memory mock implementations of infrastructure services so you can test without running Dapr or Temporal sidecars.

```python
# tests/test_extractor.py
import pytest
from application_sdk.testing import MockStateStore, MockSecretStore
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
FROM registry.atlan.com/public/app-runtime-base:3

WORKDIR /app

COPY --chown=appuser:appuser pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project

COPY --chown=appuser:appuser . .

ENV ATLAN_HANDLER_PORT=8000
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
7. **Test without sidecars.** Use `MockStateStore` and `MockSecretStore` from `application_sdk.testing` to test your connector and handler without Dapr or Temporal running.
8. **Set `ATLAN_APP_MODULE` in the Dockerfile.** This locks the app module to the image and avoids runtime misconfiguration.
9. **Use `on_complete` for cleanup.** Override `on_complete()` for post-run cleanup (see [Upgrade Guide Step 12](../upgrade-guide-v3.md#step-12-app-lifecycle-hooks)).

## Next Steps

- [What's New in v3](../whats-new-v3.md) --- detailed comparison of v2 and v3 patterns
- [Upgrade Guide](../upgrade-guide-v3.md) --- step-by-step upgrade from v2 to v3
- [Getting Started](getting-started.md) --- development environment setup
- [Architecture](architecture.md) --- SDK architecture and component overview
