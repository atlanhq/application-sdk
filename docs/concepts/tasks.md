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
from application_sdk.contracts import MaxItems, FileReference

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

### Process-wide timeout defaults via env vars

When no explicit value is passed to `@task`, the framework reads two env vars at
process startup:

| Env var | Default | Controls |
|---|---|---|
| `ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS` | `600` (10 min) | `timeout_seconds` — Temporal kills the activity after this many seconds |
| `ATLAN_HEARTBEAT_TIMEOUT_SECONDS` | `60` | `heartbeat_timeout_seconds` — Temporal restarts the activity if no heartbeat is received within this window |

Set these in `atlan.yaml` (or your deployment env) to apply a fleet-wide default
without touching every `@task` decorator:

```yaml
# atlan.yaml
env:
  - name: ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS
    value: "1800"   # 30 min default for all tasks in this app
  - name: ATLAN_HEARTBEAT_TIMEOUT_SECONDS
    value: "120"    # 2 min heartbeat window
```

Explicit `@task(timeout_seconds=...)` values always take precedence over the env vars.

## Local Tasks (`local=True`)

Every `@task` is normally a full Temporal activity: it round-trips through the
task queue, waits in schedule-to-start, and counts as a billable action. For
small, fast steps that overhead dominates the actual work.

`@task(local=True)` runs the task as a Temporal **local activity** on the
workflow worker itself — no task-queue round-trip, no schedule-to-start
latency, one fewer action per invocation:

```python
class MyConnector(App):
    @task(local=True)
    async def normalize_paths(self, input: PathsInput) -> PathsOutput:
        return PathsOutput(paths=[p.strip().lower() for p in input.paths])
```

**Use local tasks for:** fast metadata operations, path/name normalization,
small payload shaping — anything that completes in seconds.

**Do NOT use local tasks for:** long-running work, anything that needs
heartbeats or resume-on-retry progress, or steps that should run on a
separately-scaled activity worker. Local tasks execute on the workflow
worker's pod and cannot heartbeat.

Constraints — enforced at class definition time (`LocalTaskConfigurationError`):

| Option | Rule |
|---|---|
| `heartbeat_timeout_seconds` / `auto_heartbeat_seconds` | Must be unset (or explicitly `None`). Local activities cannot heartbeat; the env-var heartbeat defaults are not applied. |
| `timeout_seconds` | Must be ≤ 300 s (the framework's short-task convention — the built-in cleanup tasks use the same bound). When unset, defaults to `min(ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS, 300)`. |

Retry options (`retry_max_attempts`, `retry_policy`, ...) still apply — local
activities retry on the workflow worker. No extra registration is needed: the
combined worker already registers every `@task` activity, and Temporal uses
the same definition for local execution.

See [ADR-0018](../adr/0018-local-activities-and-batched-fanout.md) for the
rationale.

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
        prev = self.task_context.get_heartbeat_details(MyProgress)
        start_id = prev.last_id if prev else None

        for batch in get_batches(start_from=start_id):
            process(batch)
            self.task_context.heartbeat(
                MyProgress(last_id=batch.id, records_done=batch.count)
            )
```

## Infrastructure Access via self.context

Inside a `@task` method, access infrastructure through `self.context`:

```python
class MyConnector(App):
    @task
    async def fetch(self, input: FetchInput) -> FetchOutput:
        # Secret store
        api_key = await self.context.get_secret("my-api-key")

        # Credential resolution
        cred = await self.context.resolve_credential(input.credential_ref)
        ...
```

You do not create `DaprClient` instances or call `SecretStore` statics. Infrastructure is injected by the framework -- Dapr-backed in production, in-memory mocks in tests.

## Blocking Sync Code

Prefer native async libraries wherever possible. For legacy sync code that cannot be rewritten, `self.task_context.run_in_thread(fn, *args)` offloads the call to a thread pool, preventing it from stalling the event loop and blocking heartbeats. See [ADR-0010](../adr/0010-async-first-blocking-code.md) for when this is appropriate and the required internal-timeout precautions.

## FileReference: Passing Large Data Between Tasks

Temporal has a ~2 MB payload limit. Use `FileReference` for large data:

```python
from application_sdk.contracts import FileReference

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

`FileReference` handles task-to-task data passing within a run (automatic via the activity
interceptor). For hand-off to Atlan system apps (publish, lineage, quality), call
`App.upload()` explicitly from `run()` — see [storage.md](storage.md) and
[file-reference.md](file-reference.md) for the two-store routing details.

## Batched Fan-Out with `map_batched`

Connectors that fan out activity-per-item multiply both action count and
schedule-to-start latency: 10,000 tables = 10,000 activities. Batching N items
per activity divides the action count by N.

`App.map_batched()` (call it from `run()`) chunks a list of items, invokes a
`@task` once per chunk, runs chunks concurrently (bounded), and returns one
`Output` per chunk **in chunk order**:

```python
from typing import Annotated
from application_sdk.contracts import Input, Output, MaxItems

class TableChunkInput(Input):
    tables: Annotated[list[str], MaxItems(500)]

class TableChunkOutput(Output):
    results: Annotated[list[str], MaxItems(500)]

class MyConnector(App):
    @task(timeout_seconds=900)
    async def process_tables(self, input: TableChunkInput) -> TableChunkOutput:
        return TableChunkOutput(results=[process(t) for t in input.tables])

    async def run(self, input: RunInput) -> RunOutput:
        outputs = await self.map_batched(
            self.process_tables,
            input.tables,                      # 10k tables ...
            batch_size=100,                    # ... become 100 activities
            max_concurrency=5,                 # at most 5 chunks in flight
            input_factory=lambda chunk: TableChunkInput(tables=chunk),
        )
        # Flatten per-item results from the ordered chunk outputs:
        all_results = [r for out in outputs for r in out.results]
        return RunOutput(count=len(all_results))
```

Things to keep in mind:

- **Payload safety.** Each chunk travels as a workflow payload, so
  `batch_size` × per-item size must stay well under Temporal's 2 MB limit.
  Bound the chunk field with `MaxItems`, and pass `FileReference` objects
  instead of inline data for large items (see
  [ADR-0008](../adr/0008-payload-safe-bounded-types.md)).
- **Retry granularity.** A chunk is the retry unit: the task's retry policy
  applies per chunk, and a chunk retries *together*. A chunk that exhausts
  its retries fails the whole `map_batched` call (first error propagates,
  like `asyncio.gather`). Smaller batches = finer retry granularity but more
  actions.
- **Works with local tasks.** `map_batched` invokes whatever task you hand
  it — remote or `local=True`.
- `batch_size` and `max_concurrency` must be ≥ 1; invalid values raise
  `MapBatchedInvalidArgumentError` at call time.

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
