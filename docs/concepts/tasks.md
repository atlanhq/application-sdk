# Tasks

Tasks are the units of work in a v3 application. They replace v2's `ActivitiesInterface` and `@activity.defn` with a single `@task` decorator on `App` methods.

## Defining a Task

Decorate any `async` method on your `App` subclass with `@task`. Each task method takes exactly one `Input` and returns exactly one `Output` -- both Pydantic models.

```python
from application_sdk.app import App, task
from application_sdk.contracts import Input, Output

class FetchInput(Input):
    connection_id: str

class FetchOutput(Output):
    rows_fetched: int

class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        # ... fetch logic
        return FetchOutput(rows_fetched=42)
```

Under the hood, `@task` applies the Temporal `@activity.defn` decorator, registers the method in the `TaskRegistry`, and wires heartbeating. You never import from `temporalio` directly.

## Typed Input/Output Contracts

Every task boundary is a typed contract. The SDK validates contract fields at class definition time -- forbidden types raise `PayloadSafetyError` before your app starts.

**Forbidden types:** `Any`, `bytes`, `bytearray`, unbounded `list[T]`, unbounded `dict[K, V]`.

**Safe alternatives:**

| Need | Use |
|------|-----|
| Bounded list | `Annotated[list[str], MaxItems(1000)]` |
| Large binary / file data | `FileReference` (stored in object store) |
| Enum values | `SerializableEnum` |

```python
from typing import Annotated
from application_sdk.contracts import Input, Output
from application_sdk.contracts.types import MaxItems, FileReference

class ProcessInput(Input):
    items: Annotated[list[str], MaxItems(5000)]

class ProcessOutput(Output):
    results: FileReference  # large data stored in object store
```

## Timeouts and Auto-Heartbeating

Configure timeouts and heartbeating as keyword arguments on `@task`:

```python
class MyConnector(App):
    @task(
        timeout_seconds=3600,            # Temporal kills the task after 1 hour
        heartbeat_timeout_seconds=60,    # Temporal kills the task if no heartbeat in 60s
        auto_heartbeat_seconds=10,       # framework sends a heartbeat every 10s
    )
    async def long_running(self, input: MyInput) -> MyOutput:
        # heartbeats run automatically in a background asyncio loop
        ...
```

There is no `@auto_heartbeater` decorator in v3. Heartbeating is declarative.

## Manual Heartbeats with Progress

For tasks that should resume from where they left off after a retry, send typed heartbeat details:

```python
from application_sdk.contracts import HeartbeatDetails

class MyProgress(HeartbeatDetails):
    last_id: str
    records_done: int

class MyConnector(App):
    @task(heartbeat_timeout_seconds=60)
    async def process_batches(self, input: MyInput) -> MyOutput:
        prev = await self.task_context.get_heartbeat_details(MyProgress)
        start_id = prev.last_id if prev else None

        for batch in get_batches(start_from=start_id):
            process(batch)
            await self.task_context.heartbeat(
                MyProgress(last_id=batch.id, records_done=batch.count)
            )
```

## Infrastructure Access via self.context

Inside a `@task` method, access infrastructure through `self.context`:

```python
class MyConnector(App):
    @task
    async def fetch(self, input: FetchInput) -> FetchOutput:
        # State store
        prev_state = await self.context.load_state("last_run")
        await self.context.save_state("last_run", {"timestamp": "2025-01-01"})

        # Secret store
        api_key = await self.context.get_secret("my-api-key")

        # Credential resolution
        cred = await self.context.resolve_credential(input.credential_ref)
        ...
```

You do not create `DaprClient` instances or call `StateStore`/`SecretStore` statics. Infrastructure is injected by the framework -- Dapr-backed in production, in-memory mocks in tests.

## Blocking Sync Code

If your task calls a blocking (non-async) library, use `run_in_thread` to avoid stalling the event loop and blocking heartbeats:

```python
class MyConnector(App):
    @task(heartbeat_timeout_seconds=60)
    async def fetch(self, input: FetchInput) -> FetchOutput:
        result = await self.task_context.run_in_thread(sync_db_call, input.query)
        return FetchOutput(data=result)
```

## FileReference: Passing Large Data Between Tasks

Temporal has a ~2 MB payload limit. Use `FileReference` for large data:

```python
from application_sdk.contracts.types import FileReference

class FetchOutput(Output):
    results: FileReference  # automatically uploaded when output leaves the task

class ProcessInput(Input):
    results: FileReference  # automatically downloaded before the task runs

class MyConnector(App):
    @task
    async def fetch(self, input: FetchInput) -> FetchOutput:
        write_parquet(data, "/tmp/results.parquet")
        return FetchOutput(results=FileReference.from_local("/tmp/results.parquet"))

    @task
    async def process(self, input: ProcessInput) -> ProcessOutput:
        data = read_parquet(input.results.local_path)
        ...

    async def run(self, input: FetchInput) -> ProcessOutput:
        fetch_out = await self.fetch(input)
        return await self.process(ProcessInput(results=fetch_out.results))
```

## Auto-Discovery

You do not register tasks manually. When your `App` subclass is defined, `App.__init_subclass__` scans it for `@task` methods and registers them in the `TaskRegistry`. The worker discovers all registered tasks at startup via `create_worker()`.

## Testing Tasks

Tasks can be tested without any Dapr sidecar or Temporal server by injecting in-memory infrastructure:

```python
import pytest
from application_sdk.testing import MockSecretStore, MockStateStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    return ctx

async def test_fetch_data(infra):
    connector = MyConnector()
    output = await connector.fetch_data(FetchInput(connection_id="test"))
    assert output.rows_fetched > 0
```
