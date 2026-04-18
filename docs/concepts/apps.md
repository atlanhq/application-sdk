# Apps

The `App` class is the central abstraction in v3. It replaces v2's split between `WorkflowInterface` (orchestration) and `ActivitiesInterface` (work). In v3, you write one class: orchestration lives in `run()`, side-effects get `@task`, and the framework handles everything else.

## Defining an App

```python
from application_sdk.app import App, task
from application_sdk.contracts import Input, Output

class ExtractionInput(Input):
    connection_id: str

class ExtractionOutput(Output):
    rows_extracted: int

class MyConnector(App):
    @task(timeout_seconds=3600, auto_heartbeat_seconds=10)
    async def fetch_data(self, input: ExtractionInput) -> ExtractionOutput:
        # Side-effect: calls an external system
        return ExtractionOutput(rows_extracted=42)

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_data(input)
```

**Key points:**

- `run()` is the orchestration entry point. It defines what tasks execute and in what order.
- `@task` methods contain external side-effects (I/O, API calls, database queries).
- Calling `await self.fetch_data(input)` inside `run()` routes through Temporal activities automatically -- no `execute_activity_method` needed.
- `App.__init_subclass__` applies Temporal decorators under the hood. You never import from `temporalio`.

### Multiple Entry Points

If your connector needs more than one independently-triggerable workflow (e.g. metadata extraction *and* query mining), decorate each entry method with `@entrypoint` instead of overriding `run()`:

```python
from application_sdk.app import App, entrypoint, task

class SnowflakeApp(App):
    @entrypoint
    async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_tables(input)

    @entrypoint
    async def mine_queries(self, input: MiningInput) -> MiningOutput:
        ...
```

Each `@entrypoint` method becomes its own Temporal workflow (`{app-name}:{entry-point-name}`). All entry points share the same `@task` methods, handler, and `AppContext`. Trigger a specific entry point via `POST /workflows/v1/start?entrypoint=<name>`. See [Entry Points](entry-points.md) for full detail.

## Orchestration in run()

The `run()` method is where you compose tasks. It supports sequential, parallel, and conditional patterns:

```python
import asyncio

class MyConnector(App):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDbInput) -> FetchDbOutput: ...

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemaInput) -> FetchSchemaOutput: ...

    @task(timeout_seconds=3600)
    async def transform_data(self, input: TransformInput) -> TransformOutput: ...

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        # Sequential
        db_out = await self.fetch_databases(
            FetchDbInput(connection_id=input.connection_id)
        )

        # Parallel (asyncio.gather works inside run)
        schema_out, transform_out = await asyncio.gather(
            self.fetch_schemas(FetchSchemaInput(databases=db_out.databases)),
            self.transform_data(TransformInput(data=db_out.data)),
        )

        return ExtractionOutput(
            rows_extracted=schema_out.count + transform_out.count
        )
```

## TaskRegistry Auto-Discovery

You never register tasks or apps manually. When Python imports your `App` subclass:

1. `App.__init_subclass__` scans the class for `@task` methods.
2. Each task is registered in the global `TaskRegistry`.
3. The `App` subclass itself is registered in `AppRegistry`.
4. At startup, `create_worker()` reads both registries and configures the Temporal worker.

## Templates

The SDK provides pre-built `App` subclasses for common patterns. Override only the tasks you need to customize.

### SqlMetadataExtractor

Extracts metadata (databases, schemas, tables, columns, procedures) from SQL sources:

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesInput, FetchDatabasesOutput,
    ExtractionInput, ExtractionOutput,
)
from application_sdk.app import task

class MyExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(
        self, input: FetchDatabasesInput
    ) -> FetchDatabasesOutput:
        # Custom database fetching logic
        return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
```

### SqlQueryExtractor

Extracts query history/logs from SQL sources:

```python
from application_sdk.templates import SqlQueryExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput, QueryBatchOutput,
    QueryFetchInput, QueryFetchOutput,
)
from application_sdk.app import task

class MyQueryExtractor(SqlQueryExtractor):
    @task(timeout_seconds=600)
    async def get_query_batches(
        self, input: QueryBatchInput
    ) -> QueryBatchOutput: ...

    @task(timeout_seconds=3600)
    async def fetch_queries(
        self, input: QueryFetchInput
    ) -> QueryFetchOutput: ...
```

### IncrementalSqlMetadataExtractor

Runs a 4-phase incremental extraction:

1. Write an incremental marker (current timestamp)
2. Fetch the current full state into a local DuckDB file
3. Diff against the previous state and emit only changed rows
4. Finalize by persisting the new state to object storage

```python
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts.incremental_sql import (
    FetchColumnsIncrementalInput, FetchColumnsOutput,
)
from application_sdk.app import task

class MyIncrementalExtractor(IncrementalSqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_columns(
        self, input: FetchColumnsIncrementalInput
    ) -> FetchColumnsOutput: ...
```

### BaseMetadataExtractor

For non-SQL sources (REST APIs, file systems). Provides the same task structure but without SQL-specific defaults.

## Lifecycle Hooks

### on_complete

Called after `run()` finishes, whether it succeeded or raised an exception:

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput: ...

    async def on_complete(self) -> None:
        await self.notify_downstream()
        await super().on_complete()  # preserves built-in file/storage cleanup
```

### Built-in Cleanup Tasks

Two cleanup tasks are available on every `App`:

- `cleanup_files()` -- removes local temporary files whose paths are tracked via `FileReference` objects from task outputs.
- `cleanup_storage()` -- removes object store artifacts uploaded with `StorageTier.TRANSIENT`. Files with `StorageTier.RETAINED` or `StorageTier.PERSISTENT` are left untouched.

Both can also be called mid-run to reclaim space after large intermediate steps.

## Passthrough Modules

If your app imports third-party libraries that must be available inside the Temporal sandbox, declare them as class parameters:

```python
class MyConnector(App, passthrough_modules=["my_connector", "third_party_lib"]):
    ...
```

In v2, passthrough modules were passed to the `Worker` constructor. In v3, they live on the class definition.

## Customizing SQL Queries

For SQL template apps, override SQL query class attributes or load from files:

```python
from application_sdk.common.utils import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/queries")

class MyExtractor(SqlMetadataExtractor):
    fetch_database_sql = SQL_QUERIES.get("FETCH_DATABASES")
    fetch_table_sql = SQL_QUERIES.get("FETCH_TABLES")
```

## Testing Apps

Test `@task` methods directly without Temporal or Dapr:

```python
import pytest
from application_sdk.testing import MockSecretStore, MockStateStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    return ctx

async def test_fetch(infra):
    connector = MyConnector()
    output = await connector.fetch_data(
        ExtractionInput(connection_id="test")
    )
    assert output.rows_extracted > 0
```

Use the `clean_app_registry` fixture to prevent `App` subclass registrations from leaking between tests:

```python
# conftest.py
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401
```
