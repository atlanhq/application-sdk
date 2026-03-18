# Migration Guide: v2 → v3

Application SDK v3.0 introduces three major improvements:

1. **Schema-driven contracts** — typed `Input`/`Output` dataclasses replace `Dict[str, Any]`
2. **Infrastructure abstraction** — Protocol-based interfaces decouple services from Dapr
3. **Temporal abstraction** — `App` + `@task` replace `@workflow.defn` + `@activity.defn`

All v2 imports remain functional in v3.0.x with `DeprecationWarning`. They will be removed in v3.1.0.

---

## Quick Reference

| v2 | v3 |
|---|---|
| `from application_sdk.workflows import WorkflowInterface` | `from application_sdk.app import App` |
| `from application_sdk.activities import ActivitiesInterface` | `from application_sdk.app import task` |
| `from application_sdk.handlers import HandlerInterface` | `from application_sdk.handler import Handler` |
| `from application_sdk.services.statestore import StateStore` | `from application_sdk.infrastructure import StateStore` |
| `from application_sdk.worker import Worker` | `from application_sdk.execution import create_worker` |
| `from application_sdk.application import BaseApplication` | `from application_sdk.main import run_dev_combined` |
| `from application_sdk.server.fastapi.models import WorkflowRequest` | `from application_sdk.handler.contracts import ...` |
| `from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow` | `from application_sdk.templates import SqlMetadataExtractor` |
| `from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow` | `from application_sdk.templates import SqlQueryExtractor` |

---

## Step 1: Migrate SQL Metadata Extraction

### v2

```python
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities

@workflow.defn
class MyMetadataWorkflow(BaseSQLMetadataExtractionWorkflow):
    activities_cls = MyMetadataActivities

class MyMetadataActivities(BaseSQLMetadataExtractionActivities):
    async def fetch_databases(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
        # fetch databases
        return ActivityStatistics(chunk_count=1, total_record_count=10)
```

### v3

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesInput, FetchDatabasesOutput,
    ExtractionInput, ExtractionOutput,
)
from application_sdk.app import task

class MyMetadataExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        # fetch databases
        return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
```

Key changes:
- Single class instead of workflow + activities split
- Typed `Input`/`Output` dataclasses instead of `Dict[str, Any]`
- `@task` decorator instead of `@activity.defn` + manual `execute_activity_method`
- Override `run()` to customize orchestration (default: parallel fetch of all metadata types)

---

## Step 2: Migrate SQL Query Extraction

### v2

```python
from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow
from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities

@workflow.defn
class MyQueryWorkflow(SQLQueryExtractionWorkflow):
    activities_cls = MyQueryActivities
```

### v3

```python
from application_sdk.templates import SqlQueryExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput, QueryBatchOutput,
    QueryFetchInput, QueryFetchOutput,
)
from application_sdk.app import task

class MyQueryExtractor(SqlQueryExtractor):
    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        ...

    @task(timeout_seconds=3600)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        ...
```

---

## Step 3: Migrate the Handler

### v2

```python
from application_sdk.handlers import HandlerInterface

class MyHandler(HandlerInterface):
    async def load(self, *args, **kwargs) -> None: ...
    async def test_auth(self, *args, **kwargs) -> bool: ...
    async def preflight_check(self, *args, **kwargs): ...
    async def fetch_metadata(self, *args, **kwargs): ...
```

### v3

```python
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus,
    MetadataInput, MetadataOutput,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # test connection
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        # run preflight checks
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        # return connector config
        return MetadataOutput(fields=[])
```

---

## Step 4: Migrate the Application Entry Point

### v2

```python
from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)

app = BaseSQLMetadataExtractionApplication(
    name="my-connector",
    client_class=MyClient,
    handler_class=MyHandler,
)
await app.start()
```

### v3

```python
# pyproject.toml or Dockerfile CMD
# application-sdk --mode combined --app my_package.apps:MyMetadataExtractor

# Or programmatically (e.g., for local dev):
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(MyMetadataExtractor, handler_class=MyHandler))
```

Or run via CLI:

```bash
application-sdk --mode combined --app my_package.apps:MyMetadataExtractor
```

---

## Step 5: Define Typed Contracts

v3 uses `Input`/`Output` dataclasses for all task boundaries. The SDK validates these at import time.

```python
from dataclasses import dataclass
from application_sdk.contracts import Input, Output
from application_sdk.contracts.types import MaxItems
from typing import Annotated

@dataclass
class MyTaskInput(Input):
    workflow_id: str
    connection: str
    items: Annotated[list[str], MaxItems(1000)]  # bounded list required

@dataclass
class MyTaskOutput(Output):
    items_processed: int
    success: bool
```

Forbidden field types (raise `PayloadSafetyError` at class definition):
- `Any`, `bytes`, `bytearray`
- Unbounded `list[T]` or `dict[K, V]`

Safe alternatives:
- `Annotated[list[T], MaxItems(N)]` for bounded lists
- `FileReference` for large data (stored in object store, not in-memory)
- `allow_unbounded_fields=True` escape hatch (use sparingly)

---

## Step 6: Migrate Infrastructure Access

In v3, infrastructure services are created automatically at startup (by `main.py`) and
injected into `@task` methods via `self.context` and into handlers via `self.context`.
You do not need to create `DaprClient` instances in your code.

### Infrastructure available in `@task` methods

| v2 | v3 (`self.context` in `@task` methods) |
|----|----------------------------------------|
| `services.statestore.StateStore.get_state(...)` | `await self.context.load_state(key)` |
| `services.statestore.StateStore.save_state(...)` | `await self.context.save_state(key, value)` |
| `services.secretstore.SecretStore.get_credentials(...)` | `await self.context.get_secret(name)` |
| `services.objectstore.ObjectStore.get_content(key)` | `await self.context.download_bytes(key)` |
| `services.objectstore.ObjectStore.upload_file(src, key)` | `await self.context.upload_bytes(key, data)` |
| `services.eventstore.EventStore.publish_event(event)` | Automatic via interceptor |

### Infrastructure available in handlers

| v2 | v3 (`self.context` in handler methods) |
|----|----------------------------------------|
| `services.secretstore.SecretStore.get_credentials(...)` | `await self.context.get_secret(name)` |
| `/workflows/v1/config/{id}` (503 error) | Works automatically (state store wired) |
| `/workflows/v1/file` (503 error) | Works automatically (storage binding wired) |

### Local development with custom secrets

```python
from application_sdk.infrastructure.secrets import InMemorySecretStore
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyApp,
    credential_stores={
        "secretstore": InMemorySecretStore({"my-api-key": "test-value"}),
    },
))
```

### v2 (Dapr-coupled, direct usage)

```python
from application_sdk.services.secretstore import SecretStore

secrets = await SecretStore.get_credentials(workflow_args)
```

### v3 (Protocol-based, automatic injection)

```python
from application_sdk.app import App, task
from application_sdk.contracts import Input, Output

class MyApp(App, app_name="my-app"):
    @task(timeout_seconds=60)
    async def my_task(self, input: MyInput) -> MyOutput:
        # Secret store is automatically injected by the execution layer
        api_key = await self.context.get_secret("my-api-key")
        # State store
        await self.context.save_state("progress", {"step": 1})
        # Storage
        await self.context.upload_bytes("output/result.json", data)
```

---

## Removed in v3.1.0

The following modules will be removed:

- `application_sdk.workflows.metadata_extraction.sql`
- `application_sdk.activities.metadata_extraction.sql`
- `application_sdk.workflows.query_extraction.sql`
- `application_sdk.activities.query_extraction.sql`
- `application_sdk.application.BaseApplication`
- `application_sdk.application.metadata_extraction.sql.BaseSQLMetadataExtractionApplication`
