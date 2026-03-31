# V3 Architecture — Quick Reference

## Three Core Ideas

### 1. One App Class

Every application subclasses `App` directly. There is no workflow/activities split.

```python
from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output

class MyApp(App):
    @task(timeout_seconds=600)
    async def fetch(self, input: FetchInput) -> FetchOutput:
        # @task = side effects allowed (I/O, network, files)
        return FetchOutput(count=42)

    async def run(self, input: MyInput) -> MyOutput:
        # run() = deterministic orchestration only
        result = await self.fetch(FetchInput())
        return MyOutput(total=result.count)
```

**Registration is automatic.** When you define a class that subclasses `App` and implements `run()` with typed Input/Output, the framework registers it at import time. No manual registration needed.

**Name derivation:** `SnowflakeConnector` → `snowflake-connector` (used for task queue, logging). Override with `name = "custom-name"` class attribute.

### 2. Typed Contracts

Every boundary (run(), @task, handler) uses typed Pydantic models.

```python
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, MaxItems
from typing import Annotated

class ExtractInput(Input):
    source_id: str
    batch_size: int = 1000
    tags: Annotated[list[str], MaxItems(50)]

class ExtractOutput(Output):
    record_count: int = 0
    output_ref: FileReference = FileReference()
```

Validated at **import time**. If you use a forbidden type, the app won't even start.

### 3. Infrastructure via Context

Access state, secrets, storage through `self.context` inside @task methods:

```python
@task(timeout_seconds=300)
async def my_task(self, input: MyInput) -> MyOutput:
    secret = await self.context.get_secret("api-key")
    await self.context.save_state("checkpoint", {"offset": 100})
    state = await self.context.load_state("checkpoint")
    await self.context.upload_bytes("output/data.json", data)
    return MyOutput()
```

In tests, the framework injects mock implementations. In production, it uses Dapr. You never import infrastructure directly.

## App Lifecycle

1. **Import time:** App class defined → registered in AppRegistry. Contracts validated for payload safety.
2. **Startup:** `run_dev_combined()` or CLI creates Temporal worker + FastAPI handler.
3. **Execution:** Temporal calls `run()` → `run()` calls `@task` methods → tasks do real work.
4. **Replay:** If worker restarts mid-run, Temporal replays `run()` deterministically, skipping completed tasks.

## Key Properties on App

| Property/Method | Where | What it does |
|----------------|-------|-------------|
| `self.context` | `@task` | Infrastructure access (state, secrets, storage) |
| `self.logger` | `run()` + `@task` | Structured logger (bound to app name, run ID) |
| `self.now()` | `run()` | Deterministic datetime (safe for replay) |
| `self.uuid()` | `run()` | Deterministic UUID (safe for replay) |
| `self.heartbeat(details)` | `@task` | Manual progress heartbeat |
| `self.app_state` | `@task` | In-memory state (lost on restart) |
| `self.persistent_state` | `@task` | Durable state (survives restarts) |
| `self.run_in_thread(fn)` | `@task` | Run blocking code in thread pool |
| `self.require(val, name)` | anywhere | Assert non-None or raise NonRetryableError |
| `self.call(OtherApp, input)` | `run()` | Call another app as a child workflow |
| `self.upload(UploadInput)` | `run()` | Framework task: upload files to object store |
| `self.download(DownloadInput)` | `run()` | Framework task: download files from object store |
| `self.continue_with(new_input)` | `run()` | Restart with new input (fresh history) |

## @task Decorator Options

```python
@task(
    timeout_seconds=3600,           # Max execution time (default: 600)
    heartbeat_timeout_seconds=120,  # Temporal kills if no heartbeat
    auto_heartbeat_seconds=30,      # Framework sends heartbeat automatically
    retry_max_attempts=3,           # Number of retries (default: 3)
    retry_max_interval_seconds=30,  # Max backoff between retries
)
```

## Error Types

| Error | When to use |
|-------|------------|
| `NonRetryableError` | Auth failure, invalid input, config error — will NOT retry |
| `AppError` | General app error — will retry per retry policy |
| `HandlerError(message, http_status=400)` | Handler validation error — returns HTTP error |
