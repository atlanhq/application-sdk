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

## Processing Large Datasets: `App.iterate`

Temporal force-terminates any workflow whose event history exceeds **~51,200 events / 50 MB**. A connector that dispatches **one activity per item** (10k tables = 10k activities) drives history linearly toward that cap and gets killed mid-run on large tenants. The root-cause fix is to **stop writing per-item activities**; `App.iterate` packages that fix (see [ADR-0017](../adr/0017-durable-page-iteration.md)):

1. **The fix — write one heartbeating page-task, not one activity per item.** A single `@task` processes a whole *page* internally, heartbeating per item (§Manual Heartbeats) and offloading blocking work via `run_in_thread`. That page is **one** activity — a handful of history events — no matter how many items it holds. At ~4–6 events per activity, coarse pages let a single run absorb **millions of items** with no history reset. For essentially every connector, this lever alone keeps history bounded.
2. **The safety net — continue-as-new between pages.** Because 50K is a hard limit, an extreme tenant whose history still approaches the cap must reset it. `App.iterate` checks `is_continue_as_new_suggested()` after each page and, *only when Temporal reports history is near the cap*, restarts with a fresh history via `continue_with`, resuming at the current cursor. **In the common case this never fires** — it is a backstop, not the mechanism you rely on.

```python
class CrawlApp(App):
    @task
    async def crawl_page(self, input: PageInput) -> PageOutput:
        rows, next_token = await self.run_in_thread(fetch_page, input.page_token)
        for i, row in enumerate(rows):
            persist(row)  # per-page results MUST be persisted here — see caveat
            self.heartbeat(PageProgress(page_token=input.page_token, index=i))
        return PageOutput(next_page_token=next_token)  # None => done

    async def run(self, input: CrawlInput) -> CrawlOutput:
        await self.iterate(
            self.crawl_page,
            cursor=input.page_token,                       # None first run; resumed after CAN
            input_factory=lambda tok: PageInput(page_token=tok),
            next_cursor=lambda out: out.next_page_token,   # None => iteration complete
            resume_input=lambda tok: replace(input, page_token=tok),
        )
        return CrawlOutput(...)
```

Design rules:

- **Page size is bounded by memory / payload, *not* by the heartbeat timeout** — heartbeating inside the activity is the timeout-prevention mechanism. Keep a page under the ~2 MB payload limit; use `FileReference` for large per-item data (§FileReference).
- **The cursor must round-trip through `run()`'s `Input`** so the workflow behaves identically first-run and after continue-as-new. Large cursors belong in `persistent_state` (claim-check), not inline.
- **Persist per-page results in the page-task** (object/state store). Values accumulated in workflow memory do **not** survive a continue-as-new boundary; `iterate` returns only the final generation's `Output`.
- **Cleanup runs every generation.** `continue_with` triggers `on_complete()` cleanup (`cleanup_files` / `cleanup_storage`) on each cycle — never rely on transient local temp surviving across a boundary.

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
