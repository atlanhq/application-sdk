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

## Reporting Failure as Data

Most tasks report failure by raising -- Temporal marks the activity failed and retries it
per the task's retry policy. A few must not: when *retrying is itself the damage*, the task
catches the exception and returns a structured failure on its `Output`, and the caller
(`run()`) decides what to do.

`SqlApp.prime_sql_auth` is the SDK's worked example. It issues one `SELECT 1` to warm the
source's auth cache before the parallel extract burst. If the source rejects the
credential, an activity-level retry just stacks the source's `failed_login_attempts`
counter -- accelerating the very lockout the probe exists to prevent. So the task is
declared `retry_max_attempts=1` and returns `PrimeAuthOutput(success=False, failure=...)`
instead of raising.

### The typed failure contract

`failure` is a `FailureDetails` -- the SDK's wire envelope for a classified error (see
[common.md](common.md#wire-envelope)). It carries routing (`category`, `audience`,
`retryable`, `code`), a `message`, an optional `suggested_action`, and per-error
`evidence`.

`SqlApp.run()` consumes it by rebuilding the typed error and raising it, so the workflow
fails with the verdict the task reached -- routing, evidence and remediation hint intact:

```python
prime_result = await self.prime_sql_auth(task_input)
if not prime_result.success:
    raise self._classify_prime_failure(prime_result)
```

**Classify where the exception is, not where the strings are.** Only serialisable fields
cross an activity boundary, and `BaseSQLClient.get_results` rewraps every driver exception
in one wrapper class with one fixed message -- so a DNS failure, a TLS error and a
credential rejection arrive at the caller looking identical. A caller classifying from
those strings routes all three to `INTERNAL` / `APP_OWNER`, telling Atlan to investigate a
customer credential problem. Classifying inside the task, where the live `__cause__` chain
still exists, is what makes `AuthError` / `USER` reach the right first-responder.

Apply the same shape to your own task: classify against the live exception, put the verdict
on the output, and let the caller rebuild it.

### Legacy string fields

`PrimeAuthOutput` also carries `error_type` and `error_message`. They are a fallback, not
the contract -- read `failure` first:

| Field | Semantics |
|---|---|
| `failure` | Authoritative typed verdict. `None` on success, and on outputs from an older SDK. |
| `error_type` | Class name of the **root cause** (innermost `__cause__`), not the SDK wrapper around it. |
| `error_message` | Message of the **root cause**, secret-redacted and truncated to 500 chars. |

`_classify_prime_failure` falls back to matching those two strings only when `failure` is
absent -- an activity result written by an older SDK and replayed from Temporal history, or
a `PrimeAuthOutput` a connector built by hand.

### Redaction is the producer's job

A SQL driver message routinely embeds the whole connection string, password included, and
everything on an output is persisted in Temporal history and in logs. The SDK therefore
redacts at every boundary that writes to the wire: at capture (`error_message`), when
serialising a typed verdict (`message`, `suggested_action`, and recursively through nested
`evidence` values), and again when rebuilding an error from a replayed envelope -- history
may predate the redaction, and the envelope's denylist rejects secret-named evidence *keys*
only, never a secret sitting in a value.

If your task returns failure as data, redact before it leaves the task:

```python
from application_sdk.errors import redact_secrets, safe_traceback

@task(retry_max_attempts=1)
async def probe(self, input: ProbeInput) -> ProbeOutput:
    try:
        await self._connect(input)
    except Exception as exc:
        # Never exc_info=True here -- the traceback renders the driver
        # message, connection string and all, verbatim into the log.
        logger.error("probe failed\n%s", safe_traceback(exc))
        return ProbeOutput(success=False, error_message=redact_secrets(str(exc)))
    return ProbeOutput(success=True)
```

Evidence keys are constrained too: `FailureDetails` rejects secret-named keys
(`api_key`, `db_password`, `*_token`, …) outright. `prime_sql_auth` degrades such a verdict
by dropping the offending keys and keeping the typed routing -- never by falling back to a
string-matched guess, which would silently invert the connector's own verdict.

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
