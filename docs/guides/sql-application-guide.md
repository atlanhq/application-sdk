# Building SQL Applications with Application SDK v3

This guide walks through building a SQL metadata extraction connector using the v3 Application SDK. By the end, you will have a working connector that extracts databases, schemas, tables, and columns, transforms them using an asset mapper, and publishes the results.

v3 replaces the v2 `BaseSQLMetadataExtractionWorkflow` + `BaseSQLMetadataExtractionActivities` split with a single `SqlApp` subclass. (`SqlMetadataExtractor`, the first v3 template, is deprecated and removed in v4.0.0; do not start on it.) If you are upgrading an existing v2 connector, see the [Upgrade Guide](../upgrade-guide-v3.md) for a step-by-step checklist.

## Overview

A v3 SQL connector has five parts:

1. **App (extractor)** --- a `SqlApp` subclass that declares the SQL to run and the `map_*` method for each entity.
2. **Handler** --- a `Handler` subclass that implements HTTP endpoints for authentication, preflight checks, and metadata discovery.
3. **Client** --- a client class (e.g., `BaseSQLClient` subclass or custom `ClientInterface` implementation) that manages the database connection.
4. **Asset mapper** --- `map_*` methods that turn each raw SQL row into a `pyatlan_v9` asset (replaces YAML transformers).
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

You do not need to start any infrastructure: `run_dev_combined()` starts an embedded Dapr runtime and an in-process Temporal server (see [Getting Started](./getting-started.md)). To run against a standalone Temporal server and Dapr sidecar instead, see the [Setup Overview](../standards/setup.md#optional-external-temporal-and-dapr).

## Project Structure

A v3 SQL connector follows this layout:

```
my-connector/
  app/
    __init__.py
    clients.py          # Client class (BaseSQLClient or ClientInterface)
    contracts.py        # Typed Pydantic models (credentials, config, task inputs)
    handler.py          # Handler subclass
    connector.py        # SqlApp subclass (SQL templates + map_* methods)
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
from application_sdk.templates.contracts import ExtractionTaskInput


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
    exclude_views: bool = False


class MyFetchInput(ExtractionTaskInput):
    """Extended task input with connector-specific config."""
    metadata_config: MetadataConfig = MetadataConfig()
```

## Client

Extend `BaseSQLClient` to define the connection string template for your database. The SDK uses this template to build a SQLAlchemy connection URL.

```python
# app/clients.py
from application_sdk.clients import BaseSQLClient, DatabaseConfig


class PostgresClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )
```

The `required` list declares which credential keys must be present. The SDK validates these at connection time and raises a clear error if any are missing.

Each value is made URL-safe before it fills the template, so a connection-form value cannot add a driver parameter or override one in `defaults` (such as `sslmode`):

- `host` must be a hostname, an IPv4 address, or an IPv6 address in square brackets (`[::1]`). A value with a scheme, port, path or query raises `SqlCredentialsParseError`.
- `port` must be a whole number from 1 to 65535, as an `int` or a string of digits. Anything else raises `SqlCredentialsParseError`.
- Every other field, `password` included, is percent-encoded, wherever it sits in the template. SQLAlchemy decodes it, so the driver receives the original value. Store raw values, not pre-encoded ones.

`host` and `port` are only validated when the template contains `{host}` or `{port}`.

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
        # BaseSQLClient.run_query lowercases column names, so dict keys are lowercase.
        # SqlMetadataObject keeps the uppercase field names from the wire schema.
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

`AuthOutput.status` uses `AuthStatus` (SUCCESS, FAILED, EXPIRED, INVALID_CREDENTIALS). A handler sets `PreflightOutput.status` to `PreflightStatus.READY` when extraction can proceed (advisory check failures included) or `NOT_READY` when it cannot. `PARTIAL` is deprecated (removal anchored at v3.40.0); return `READY` with the failed advisory checks listed instead.

## App (Extractor)

The core of your connector is a `SqlApp` subclass. You declare three things and the template does the rest:

- `sql_client_class` --- the client from [Client](#client).
- `fetch_database_sql`, `fetch_schema_sql`, `fetch_table_sql`, `fetch_column_sql` --- one query per entity. Each must stand alone: the default `run()` issues all four at once, so each query applies its own filters (here, the same system-schema exclusions in all three).
- `map_database`, `map_schema`, `map_table`, `map_column` --- one asset mapper per entity. Each takes one raw row and the connection's qualified name and returns a `pyatlan_v9` asset. All four are required for the default `run()`: the base ones raise `Map*UnimplementedError`.

```python
# app/connector.py
from pyatlan_v9.model.assets import Column, Database, Schema, Table

from application_sdk.templates import SqlApp

from app.clients import PostgresClient


class PostgresApp(SqlApp):
    sql_client_class = PostgresClient

    fetch_database_sql = (
        "SELECT datname AS database_name FROM pg_database "
        "WHERE datname = current_database()"
    )
    fetch_schema_sql = """
        SELECT s.catalog_name AS database_name, s.schema_name
        FROM information_schema.schemata s
        WHERE s.schema_name NOT LIKE 'pg_%'
          AND s.schema_name != 'information_schema'
    """
    fetch_table_sql = """
        SELECT t.table_catalog AS database_name, t.table_schema AS schema_name,
               t.table_name
        FROM information_schema.tables t
        WHERE t.table_type = 'BASE TABLE'
          AND t.table_schema NOT LIKE 'pg_%'
          AND t.table_schema != 'information_schema'
    """
    fetch_column_sql = """
        SELECT c.table_catalog AS database_name, c.table_schema AS schema_name,
               c.table_name, c.column_name, c.data_type, c.ordinal_position
        FROM information_schema.columns c
        WHERE c.table_schema NOT LIKE 'pg_%'
          AND c.table_schema != 'information_schema'
    """

    def map_database(self, record, connection_qn):
        return Database.creator(
            name=record["database_name"],
            connection_qualified_name=connection_qn,
        )

    def map_schema(self, record, connection_qn):
        database = self.map_database(record, connection_qn)
        return Schema.creator(
            name=record["schema_name"],
            database_qualified_name=database.qualified_name,
        )

    def map_table(self, record, connection_qn):
        schema = self.map_schema(record, connection_qn)
        return Table.creator(
            name=record["table_name"],
            schema_qualified_name=schema.qualified_name,
        )

    def map_column(self, record, connection_qn):
        table = self.map_table(record, connection_qn)
        column = Column.creator(
            name=record["column_name"],
            parent_qualified_name=table.qualified_name,
            parent_type=Table,
            order=record["ordinal_position"],
        )
        column.data_type = record["data_type"]
        return column
```

Build every asset with its `pyatlan_v9` `.creator()` factory. Each one computes its own qualified name from its parent's, so the naming grammar lives in pyatlan rather than in a hand-written `f"{connection_qn}/..."` string per connector (conformance rule P028 flags those). A mapper is a plain method of one row: unit test it by calling it with a dict.

> **Note:** New connectors must use `pyatlan_v9`. The deprecated `AtlasTransformer` / `QueryBasedTransformer` YAML transformers, which still use `pyatlan` internally, are not part of a `SqlApp` connector.

**Return the asset, not a dict.** The SDK serialises whatever the mapper returns through `application_sdk.common.asset_serialization.entity_bytes()`, which encodes a `pyatlan_v9` asset natively and stamps the framework-injected attributes on it — `connectionName`, and the three `lastSync*` attributes that make an asset clickable back to the run that last touched it. Do **not** stamp `last_sync_*` in the mapper: the only workflow id a mapper can reach is the child workflow's Temporal id, which is not the AE-dispatched run (see [Asset Serialisation](../concepts/common.md#asset-serialisation)). Hand-rolling a `serialize_entity()` helper to convert the asset to a dict first costs two extra JSON passes per record and gives up the type checking that is the point of the asset mapper — `asset.column_cont = 7` is a pyright error, `payload["columnCont"] = 7` is not. A return value the seam does not recognise raises `UnserializableMapperResultError` rather than falling back to anything.

**Declare envelope overrides, don't post-process.** If your connector needs the finished line to differ — a `sqlDialect` on definition-bearing assets, or a root-level field a downstream app reads — set `entity_envelope` on the app class or override `decorate_entity()`, rather than reaching past `entity_bytes()` to mutate the serialised dict. See [Entity envelope](../concepts/common.md#entity-envelope). Four connectors hand-rolled that pass and produced four incompatible wire shapes; a declared override is the same change with the fleet able to see it.

### What happens under the hood

Each `@task` method on `SqlApp` becomes a Temporal activity. The default `run()`:

1. **Prime auth** --- one serial connection before the parallel burst, so a source that caches authentication (MySQL 8's `caching_sha2_password`) does not lock the account out. A failure here raises its typed error and nothing else runs.
2. **Extract** --- `extract_databases`, `extract_schemas`, `extract_tables` and `extract_columns` run **in parallel**, each streaming its query's rows verbatim into `raw/<entity>/records.json`.
3. **Transform** --- the matching `transform_*` tasks run in parallel, each reading its raw file and running every row through its `map_*` method into `transformed/<entity>/entities.json`.
4. **Finalize** --- `finalize_extraction` verifies the transformed tree is whole and returns the `ExtractionOutput` publish reads.

Every non-empty extract and transform task output is a `FileReference` with a canonical `storage_path`, which the task's persist step uploads to the **deployment store**. Do not add `self.upload()` calls or an upload task for that.

Publish reads Atlan's store, which in an SDR deployment is a different store from the deployment store ([ADR-0014](../adr/0014-two-store-storage-architecture.md)). So hand the transformed tree on with `upload_refs()`, by reference, and return the prefix it delivered to. atlan-mysql-app does this on every run; in a single-store deployment the same call writes to the one store:

```python
from application_sdk.contracts.base import Output
from application_sdk.contracts.storage import DeclaredFile, UploadRefsInput
from application_sdk.templates.contracts.sql_metadata import ExtractionInput


class PostgresExtractionOutput(Output):
    connection_qualified_name: str = ""
    transformed_data_prefix: str = ""
    publish_state_prefix: str = ""
    current_state_prefix: str = ""


class PostgresApp(SqlApp):
    ...  # SQL templates and map_* methods as above

    async def run(self, input: ExtractionInput) -> PostgresExtractionOutput:
        out = await super().run(input)
        delivered = await self.upload_refs(
            UploadRefsInput(
                files=[DeclaredFile(ref=ref) for ref in out.transformed_files],
                source_prefix=out.transformed_data_prefix,
                prefix=out.transformed_data_prefix,
            )
        )
        return PostgresExtractionOutput(
            connection_qualified_name=out.connection_qualified_name,
            transformed_data_prefix=delivered.prefix,
            publish_state_prefix=out.publish_state_prefix,
            current_state_prefix=out.current_state_prefix,
        )
```

`delivered.prefix` is `""` when there was nothing to deliver, which tells publish not to diff the tenant against an empty tree. Return your own output model, not `ExtractionOutput`: its validator back-fills an empty `transformed_data_prefix` from `output_path`, which undoes that signal.

### Available task methods

| Method | Input | Output | Default behavior |
|--------|-------|--------|-----------------|
| `extract_databases` / `extract_schemas` / `extract_tables` / `extract_columns` | `ExtractionTaskInput` | `ExtractionTaskOutput` | Runs the matching `fetch_*_sql` through `sql_client_class` |
| `extract_views` / `extract_procedures` | `ExtractionTaskInput` | `ExtractionTaskOutput` | Optional: not driven by the default `run()`; set `fetch_view_sql` / `fetch_procedure_sql` and call them from a `run()` override |
| `transform_databases` / `transform_schemas` / `transform_tables` / `transform_columns` | `TransformInput` | `TransformOutput` | Runs each raw row through the matching `map_*` method |
| `transform_views` / `transform_procedures` | `TransformInput` | `TransformOutput` | Optional, as for the extract pair |

Override an `extract_*` or `transform_*` task to change one entity's SQL or mapper; call `extract_entity` / `transform_entity` from the override rather than writing the files yourself.

### Excluding tables and views by name

The connector form's "Exclude regex for tables & views" field arrives as the
workflow arg `exclude_table_regex` (form key `exclude-table-regex`, flat or
nested under `metadata`). `ExtractionInput` copies it into `temp_table_regex`,
the SDK's name for the same filter, before validation, so it goes through the
same pattern and SQL-injection checks. An explicit `temp_table_regex` wins.

`SqlApp` substitutes the value into `extract_temp_table_regex_table_sql` at
`{exclude_table_regex}` and injects the result at `{temp_table_regex_sql}`.
The column query uses `extract_temp_table_regex_column_sql` instead when you
declare one (for example `AND C.TABLE_NAME NOT REGEXP '{exclude_table_regex}'`).
Otherwise it gets the table fragment too, which then needs a `T` alias in
the column query. Wrap `{exclude_table_regex}` in single quotes in
the fragment. Block comments in the fragment are stripped before substitution,
so a documented fragment is safe to inject into a template that mentions
`{temp_table_regex_sql}` in its own header comment.

```sql
-- app/sql/extract_temp_table_regex_table.sql
AND T.TABLE_NAME NOT REGEXP '{exclude_table_regex}'
```

Read `input.temp_table_regex` in custom code, not `exclude_table_regex`.

### Adding custom tasks

A custom entity (SDK ≥ 3.60.0) is an `extract_*` /
`transform_*` task pair whose bodies call `extract_entity` and
`transform_entity`. Those two own the
streaming, the event-loop offload and the canonical `FileReference` keys
publish reads, so call them rather than writing the files yourself. A
`run()` that drives the extra pair folds its output in with
`finalize_extraction`, which verifies the whole transformed tree:

```python
from application_sdk.app import task
from application_sdk.templates import SqlApp
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    TransformInput,
    TransformOutput,
)


class PostgresApp(SqlApp):
    fetch_function_sql = "SELECT ..."

    def map_function(self, record, connection_qn):
        ...  # build and return a pyatlan_v9 Function asset

    @task()
    async def extract_functions(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        return await self.extract_entity(
            entity_type="function",
            sql_template=self.fetch_function_sql,
            input=input,
        )

    @task()
    async def transform_functions(self, input: TransformInput) -> TransformOutput:
        return await self.transform_entity(
            entity_type="function", mapper_fn=self.map_function, input=input
        )

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        base = await super().run(input)
        task_input = self.build_task_input(
            ExtractionTaskInput, input, cred_ref=self.resolve_credential_ref(input)
        )
        raw = await self.extract_functions(task_input)
        out = await self.transform_functions(
            self.build_transform_input(task_input, raw.raw_file)
        )
        return await self.finalize_extraction(base, [out])
```

A `run()` that replaces `SqlApp.run()` instead of calling `super().run()` must
still end in `finalize_extraction`; conformance rule P068 flags one that does
not. `_extract_entity`, `_transform_entity` and `_resolve_output_path` are the
names these methods had before 3.60.0; they are deprecated and removed in
v4.0.0.

## Typed Contracts

v3 replaces `Dict[str, Any]` with Pydantic models for all task inputs and outputs. This provides:

- **Type safety** --- errors caught at import time, not at runtime
- **Payload validation** --- Temporal has a 2 MB payload limit; contracts forbid unbounded types (`Any`, bare `bytes`, unbounded `list`)
- **Self-documenting APIs** --- contract fields visible in Temporal UI and IDE autocompletion

### Contract hierarchy

All SQL metadata extraction contracts live in `application_sdk.templates.contracts.sql_metadata`:

```
ExtractionInput          -- top-level input to run()
ExtractionOutput         -- top-level output from run(); what publish reads
ExtractionTaskInput      -- shared fields for every extract_* task; build it with build_task_input()
  TransformInput         -- passed to every transform_* task; build it with build_transform_input()
ExtractionTaskOutput     -- raw_file: FileReference | None (None for no SQL or zero rows), total_record_count
TransformOutput          -- transformed_file: FileReference | None (None for an empty input), total_record_count
```

### Bounded collections

Unbounded `list` and `dict` are forbidden in contracts. Use `MaxItems` to declare an upper bound:

```python
from typing import Annotated
from pydantic import Field
from application_sdk.contracts import MaxItems

class FetchDatabasesOutput(Output):
    databases: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0
```

### FileReference for large data

When a task produces data too large for a Temporal payload, use `FileReference`. The SDK uploads it to the **customer-owned deployment store** (`objectstore`) automatically when the task returns:

```python
from application_sdk.contracts import FileReference

class FetchOutput(Output):
    results: FileReference  # automatically uploaded on task output (to infra.storage)

class ProcessInput(Input):
    results: FileReference  # automatically downloaded on task input
```

`FileReference` is for **task-to-task** transfer within a run. To hand off artifacts to Atlan system apps (publish, QI, lineage), call `App.upload()` explicitly from `run()` — the interceptor's auto-upload only writes to the customer-owned store and is not visible to Atlan. See [file-reference.md](../concepts/file-reference.md) and [ADR-0014](../adr/0014-two-store-storage-architecture.md).

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
from app.connector import PostgresApp


def test_map_table_builds_qualified_name():
    """A mapper is a plain method of one row: no Temporal, no SQL client."""
    table = PostgresApp().map_table(
        {"database_name": "sales", "schema_name": "public", "table_name": "orders"},
        "default/postgres/1700000000",
    )

    assert table.name == "orders"
    assert table.qualified_name == "default/postgres/1700000000/sales/public/orders"
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
6. **Log with the SDK logger.** Use `get_logger` from `application_sdk.observability` for structured logging that integrates with Temporal.
7. **Test without sidecars.** Use `MockStateStore` and `MockSecretStore` from `application_sdk.testing` to test your connector and handler without Dapr or Temporal running.
8. **Set `ATLAN_APP_MODULE` in the Dockerfile.** This locks the app module to the image and avoids runtime misconfiguration.
9. **Use `on_complete` for cleanup.** Override `on_complete()` for post-run cleanup (see [Upgrade Guide Step 12](../upgrade-guide-v3.md#step-12-app-lifecycle-hooks)).

## Next Steps

- [What's New in v3](../whats-new-v3.md) --- detailed comparison of v2 and v3 patterns
- [Upgrade Guide](../upgrade-guide-v3.md) --- step-by-step upgrade from v2 to v3
- [Getting Started](getting-started.md) --- development environment setup
- [Architecture](architecture.md) --- SDK architecture and component overview
