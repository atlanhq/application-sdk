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

Runs a 5-phase incremental extraction:

1. **Prerequisites** — fetch the prior run marker and the previous state snapshot.
2. **Base extraction** — extract databases, schemas (in parallel), then tables.
3. **Incremental columns** — batch preparation followed by parallel column discovery.
4. **Write state** — create and upload the new current-state snapshot.
5. **Update marker** — persist the new run marker after the state write succeeds.

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

- `cleanup_files()` — removes tracked `FileReference` local paths from task outputs, **then** convention-based temp directories (using `input.extra_paths` if provided, otherwise `ATLAN_CLEANUP_BASE_PATHS`, otherwise the default temp path).
- `cleanup_storage()` — removes object store artifacts by tier:
  - `StorageTier.TRANSIENT` refs are always removed.
  - `StorageTier.PERSISTENT` refs are always left untouched.
  - `StorageTier.RETAINED` refs under the run-scoped prefix are removed **only** when `input.include_prefix_cleanup=True` is set (opt-in); otherwise they are left untouched.

Both are called automatically by the default `on_complete()` implementation. Do not call them directly from `run()` — the cleanup contract is tied to workflow completion, not mid-run state.

## Passthrough Modules

If your app imports third-party libraries that must be available inside the Temporal sandbox, declare them as a class-level attribute:

```python
class MyConnector(App):
    passthrough_modules = {"my_connector", "third_party_lib"}
    ...
```

The type is `ClassVar[set[str] | None]` — use a set literal, not a list. In v2, passthrough modules were passed to the `Worker` constructor. In v3, they live on the `App` subclass as a `ClassVar`. Do **not** pass `passthrough_modules` as a class-kwarg — it is not accepted by `App.__init_subclass__`.

## Customizing SQL Queries

For SQL template apps, override SQL query class attributes or load from files:

```python
from application_sdk.common.sql_filters import read_sql_files

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
from application_sdk.infrastructure import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    yield ctx
    clear_infrastructure()

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

For testing credential resolution, use `MockCredentialStore`:

```python
from application_sdk.testing import MockCredentialStore

store = MockCredentialStore()
ref = store.add_api_key("my-service", api_key="secret123")
# Or: store.add_basic("db", username="user", password="pass")
# Or: store.add_bearer_token("svc", token="tok")

ctx = InfrastructureContext(secret_store=store.secret_store)
set_infrastructure(ctx)
```

For testing tasks that emit heartbeats, use `MockHeartbeatController`:

```python
from application_sdk.testing import MockHeartbeatController

controller = MockHeartbeatController()
# Pass to AppContext or inject via fixture; inspect calls after the task runs:
calls = controller.get_heartbeat_calls()
```

---

## App State

`app_state` is in-memory state scoped to the current workflow execution. Use it to pass values between tasks without encoding them in task contracts.

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        out = await self.fetch_databases(FetchDbInput(connection_id=input.connection_id))
        # Store for later tasks to read:
        self.app_state.set("db_list", out.databases)
        return await self.transform_data(TransformInput(...))

    @task
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        dbs = self.app_state.get("db_list")
        ...
```

`persistent_state` provides durable access to state stored externally (object store). It survives workflow restarts and is shared across runs.

---

## Continuing with New Input

`continue_with()` restarts the current App with new input while preserving correlation context. It truncates the Temporal workflow history and starts a new run — useful for long-running Apps that accumulate too much history.

```python
class IncrementalExtractor(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        out = await self.fetch_batch(FetchInput(cursor=input.cursor))
        if out.has_more:
            # Restart with the next cursor — never accumulates unbounded history
            self.continue_with(ExtractionInput(cursor=out.next_cursor))
        return ExtractionOutput(total=out.count)
```

`continue_with()` does not return — it raises a framework signal internally.

---

## Retry Policies

Pass a `RetryPolicy` to `@task` via `retry_policy` to override the default (3 attempts, exponential backoff up to 5 minutes):

```python
from datetime import timedelta

from application_sdk.app import RetryPolicy

NO_RETRY = RetryPolicy(max_attempts=1)
AGGRESSIVE = RetryPolicy(
    max_attempts=10,
    initial_interval=timedelta(seconds=5),
    max_interval=timedelta(minutes=2),
    backoff_coefficient=1.5,
)

class MyConnector(App):
    @task(retry_policy=NO_RETRY)
    async def send_webhook(self, input: WebhookInput) -> WebhookOutput: ...

    @task(retry_policy=AGGRESSIVE)
    async def fetch_flaky_api(self, input: FetchInput) -> FetchOutput: ...
```

`RetryPolicy` is a frozen dataclass with fluent builder methods:

```python
policy = RetryPolicy().with_max_attempts(5).with_non_retryable(ValueError)
```

---

## Atlan Client Mixin

Mix in `AtlanClientMixin` when your App needs to call the Atlan API. It provides `get_or_create_async_atlan_client()`, which caches the `AsyncAtlanClient` per execution and reuses any client already created during `validate()`.

```python
from application_sdk.credentials import AtlanClientMixin

class MyConnector(AtlanClientMixin, App):
    @task
    async def update_lineage(self, input: LineageInput) -> LineageOutput:
        client = await self.get_or_create_async_atlan_client(input.credential)
        await client.asset.upsert(...)
        return LineageOutput(updated=True)
```

Import path: `application_sdk.credentials.AtlanClientMixin`.
