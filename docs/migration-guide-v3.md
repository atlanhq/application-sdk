# Migration Guide: v2 → v3

Application SDK v3.0 introduces three major improvements:

1. **Schema-driven contracts** — typed `Input`/`Output` Pydantic models replace `Dict[str, Any]`
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
| `from application_sdk.worker import Worker` | `from application_sdk.execution import create_worker` |
| `from application_sdk.application import BaseApplication` | `from application_sdk.main import run_dev_combined` |
| `from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow` | `from application_sdk.templates import SqlMetadataExtractor` |
| `from application_sdk.workflows.metadata_extraction.incremental_sql import IncrementalSQLMetadataExtractionWorkflow` | `from application_sdk.templates import IncrementalSqlMetadataExtractor` |
| `from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow` | `from application_sdk.templates import SqlQueryExtractor` |
| `from application_sdk.services.objectstore import ObjectStore` | `from application_sdk.storage import upload_file, download_file` |
| `from application_sdk.services.secretstore import SecretStore` | `from application_sdk.infrastructure import SecretStore` |
| `from application_sdk.services.statestore import StateStore` | `from application_sdk.infrastructure import StateStore` |
| `from application_sdk.clients.atlan import get_async_client` | `from application_sdk.credentials.atlan_client import create_async_atlan_client` |
| `from application_sdk.activities.common.models import ActivityStatistics` | `from application_sdk.common.models import TaskStatistics` |
| `from application_sdk.test_utils.credentials import MockCredentialStore` | `from application_sdk.testing import MockCredentialStore` |

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
- Typed `Input`/`Output` Pydantic models instead of `Dict[str, Any]`
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

## Step 3: Migrate Incremental SQL Extraction

### v2

```python
from application_sdk.workflows.metadata_extraction.incremental_sql import (
    IncrementalSQLMetadataExtractionWorkflow,
)
from application_sdk.activities.metadata_extraction.incremental import (
    BaseSQLIncrementalMetadataExtractionActivities,
)

@workflow.defn
class MyIncrementalWorkflow(IncrementalSQLMetadataExtractionWorkflow):
    activities_cls = MyIncrementalActivities

class MyIncrementalActivities(BaseSQLIncrementalMetadataExtractionActivities):
    async def fetch_columns(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
        ...
```

### v3

```python
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts.incremental_sql import (
    IncrementalExtractionInput, IncrementalExtractionOutput,
    FetchColumnsInput, FetchColumnsOutput,
)
from application_sdk.app import task

class MyIncrementalExtractor(IncrementalSqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        ...
```

`IncrementalSqlMetadataExtractor` runs a 4-phase extraction by default:
1. Write an incremental marker (current timestamp)
2. Fetch the current full state into a local DuckDB file
3. Diff against the previous state and emit only changed rows
4. Finalize by persisting the new state to object storage

Override `run()` if you need to customise this sequence. The incremental state files are managed by the framework and cleaned up automatically via the cleanup lifecycle.

---

## Step 4: Migrate the Handler

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

The `load()` method is removed — handler context (secrets, state) is injected automatically via `self.context`. Access credentials in handler methods with `await self.context.get_secret(name)`.

---

## Step 5: Migrate the Application Entry Point

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

**`ATLAN_APP_MODULE` is mandatory in production.** The entrypoint hard-fails at startup if it
is not set. Set it in your app's `Dockerfile` — it should never be left to Helm values or
runtime defaults:

```dockerfile
ENV ATLAN_APP_MODULE=app.app:MyMetadataExtractor \
    ATLAN_CONTRACT_GENERATED_DIR=/app/contract/generated
CMD ["application-sdk", "--mode", "combined"]
```

`ATLAN_CONTRACT_GENERATED_DIR` tells the SDK where to find the generated contract JSON files
(configmaps, manifest). The base image sets this to `/app/contract/generated`; override it in
your app's `Dockerfile` if your generated files land elsewhere.

The `--app` CLI flag is an alternative (takes precedence over the env var), but `ENV` in the
Dockerfile is the recommended approach so the value is locked to the image:

```dockerfile
CMD ["application-sdk", "--mode", "combined", "--app", "app.app:MyMetadataExtractor"]
```

For local dev, pass the class directly — `run_dev_combined` derives the module path automatically:

```python
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(MyMetadataExtractor, handler_class=MyHandler))
```

Three modes are available:
- `worker` — Temporal worker only (production worker pods)
- `handler` — HTTP handler only (production handler pods)
- `combined` — both in one process (local dev, SDR)

---

## Step 6: Migrate Worker Setup

In v2, connecting to Temporal and registering workflow/activity classes was done explicitly.
In v3, `create_worker()` auto-discovers all `App` subclasses and their `@task` methods — you
don't register anything manually.

### v2

```python
from application_sdk.worker import Worker
from application_sdk.clients.temporal import TemporalWorkflowClient

client = TemporalWorkflowClient(host=..., namespace=...)
await client.load()

worker = Worker(
    workflow_client=client,
    workflow_classes=[MyWorkflow],
    workflow_activities=[MyActivities()],
    passthrough_modules=["my_connector"],
)
await worker.run()
```

### v3

Worker setup is fully automatic when you use the CLI or `run_dev_combined()`. If you need
a worker handle directly (e.g., in integration tests):

```python
from application_sdk.execution import create_worker
from application_sdk.execution._temporal.backend import create_temporal_client

client = await create_temporal_client()  # reads TEMPORAL_HOST, TEMPORAL_NAMESPACE, etc.

# All registered App subclasses auto-discovered — no explicit list
worker = await create_worker(client)
await worker.run()
```

Passthrough modules are declared on the `App` class itself, not at worker startup:

```python
class MyConnector(App, passthrough_modules=["my_connector", "third_party_lib"]):
    ...
```

---

## Step 7: Define Typed Contracts

v3 uses `Input`/`Output` Pydantic models for all task boundaries. The SDK validates these at import time.

```python
from application_sdk.contracts import Input, Output
from application_sdk.contracts.types import MaxItems
from typing import Annotated

class MyTaskInput(Input):
    workflow_id: str
    connection: str
    items: Annotated[list[str], MaxItems(1000)]  # bounded list required

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

### ActivityStatistics / ActivityResult renamed

The v2 activity return types are renamed in v3:

| v2 (`application_sdk.activities.common.models`) | v3 (`application_sdk.common.models`) |
|----|-----|
| `ActivityStatistics` | `TaskStatistics` |
| `ActivityResult` | `TaskResult` |

In v3 templates these are returned inside typed `Output` models, so you generally won't
import them directly unless you are building a custom `App` from scratch.

### FileReference: passing large data between tasks

Temporal has a payload size limit (~2 MB). Use `FileReference` to store large data in object
storage and pass only a lightweight reference through the workflow:

```python
from application_sdk.contracts import Input, Output
from application_sdk.contracts.types import FileReference

class FetchOutput(Output):
    results: FileReference  # stored in object store, safe to pass between tasks

class ProcessInput(Input):
    results: FileReference

# In the fetch task — write to a local file, then upload:
async def fetch(self, input: FetchInput) -> FetchOutput:
    local_path = "/tmp/results.parquet"
    write_parquet(data, local_path)
    ref = await self.context.upload_file("output/results.parquet", local_path)
    return FetchOutput(results=ref)

# In the next task — download and read:
async def process(self, input: ProcessInput) -> ProcessOutput:
    local_path = await self.context.download_file(input.results.storage_path)
    data = read_parquet(local_path)
    ...
```

`FileReference` objects are tracked automatically and cleaned up by the framework's
`cleanup_files()` / `cleanup_storage()` tasks.

---

## Step 8: Migrate Infrastructure Access

In v3, infrastructure services are created automatically at startup (by `main.py`) and
injected into `@task` methods and handlers via `self.context`.
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

### Migrating ObjectStore calls

```python
# v2 — all calls went through the Dapr binding
from application_sdk.services.objectstore import ObjectStore

await ObjectStore.upload_file(source="/local/file.parquet", key="output/file.parquet")
data = await ObjectStore.get_content("config/settings.json")
files = await ObjectStore.list_files(prefix="output/")
await ObjectStore.delete_file("output/old.parquet")

# v3 — inside an @task method, use self.context:
await self.context.upload_bytes("output/file.parquet", data)
data = await self.context.download_bytes("config/settings.json")

# v3 — outside an App (standalone scripts, utilities): use the storage module directly
from application_sdk.storage import upload_file, download_file, list_keys, delete

await upload_file("output/file.parquet", local_path="/local/file.parquet")
await download_file("config/settings.json", local_path="/tmp/settings.json")
keys = await list_keys(prefix="output/")
await delete("output/old.parquet")
```

### Migrating AtlanStorage

```python
# v2
from application_sdk.services.atlan_storage import AtlanStorage
summary = await AtlanStorage(store, atlan_store).migrate_from_objectstore_to_atlan(prefix)

# v3 — use the built-in App.upload() framework task, or compose storage calls directly
# Inside a workflow run():
await self.upload(source_prefix="output/", destination_prefix="atlan/")
```

### Local development with custom secrets

```python
from application_sdk.infrastructure.secrets import InMemorySecretStore
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyApp,
    secret_store=InMemorySecretStore({"my-api-key": "test-value"}),
))
```

---

## Step 9: Migrate to Typed Credentials

v3 introduces a typed credential system that replaces bare `credential_guid: str` +
`Dict[str, Any]` with a `CredentialRef` → typed `Credential` pipeline.

### Before (credential_guid pattern)

```python
from application_sdk.contracts.base import Input

class ExtractionInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""

# In @task method:
from application_sdk.services.secretstore import SecretStore  # deprecated v2

credentials = await SecretStore.get_credentials({"credential_guid": input.credential_guid})
await client.load(credentials)  # dict[str, Any]
```

### After (CredentialRef pattern)

```python
from application_sdk.contracts.base import Input
from application_sdk.credentials import CredentialRef, api_key_ref, ApiKeyCredential

class ExtractionInput(Input, allow_unbounded_fields=True):
    credential_ref: CredentialRef | None = None

# In @task method:
cred = await self.context.resolve_credential(input.credential_ref)
assert isinstance(cred, ApiKeyCredential)
headers = cred.to_headers()  # {"X-API-Key": "secret"}
```

### Backward compatibility

Both `credential_guid` and `credential_ref` work on `ExtractionInput` and
`QueryExtractionInput`. When only `credential_guid` is provided, the SDK
auto-wraps it via `legacy_credential_ref()`:

```python
# These are both valid:
ExtractionInput(credential_guid="abc-123")       # legacy — still works
ExtractionInput(credential_ref=api_key_ref("prod-key"))  # new typed path
```

If you need to call a legacy client that expects a raw dict, use
`resolve_credential_raw()`:

```python
raw = await self.context.resolve_credential_raw(ref)
await client.load(raw)  # dict[str, Any] — same as before
```

### Available credential types

| Type | Factory | Utility |
|------|---------|---------|
| `BasicCredential` | `basic_ref(name)` | `to_auth_header()` |
| `ApiKeyCredential` | `api_key_ref(name)` | `to_headers()` |
| `BearerTokenCredential` | `bearer_token_ref(name)` | `to_auth_header()`, `is_expired()` |
| `OAuthClientCredential` | `oauth_client_ref(name)` | `to_headers()`, `needs_refresh()` |
| `CertificateCredential` | `certificate_ref(name)` | — |
| `GitSshCredential` | `git_ssh_ref(name)` | — |
| `GitTokenCredential` | `git_token_ref(name)` | `to_auth_header()` |
| `AtlanApiToken` | `atlan_api_token_ref(name)` | `to_auth_header()`, `validate()` |
| `AtlanOAuthClient` | `atlan_oauth_client_ref(name)` | `to_headers()`, `validate()` |

### Custom credential types

```python
from application_sdk.credentials import register_credential_type
from pydantic import BaseModel

class MyCredential(BaseModel, frozen=True):
    api_token: str

    @property
    def credential_type(self) -> str:
        return "my_service"

    async def validate(self) -> None:
        pass

def _parse_my(data):
    return MyCredential(api_token=data.get("api_token", ""))

register_credential_type("my_service", MyCredential, _parse_my)
```

---

## Step 10: Migrate Atlan Client Access

### v2

```python
from application_sdk.clients.atlan import get_client, get_async_client

# Sync (removed in v3)
client = get_client()

# Async
client = await get_async_client(token="...", url="...")
```

### v3

Only async clients are exposed. Use the credential system to supply the token:

```python
from application_sdk.credentials.atlan_client import create_async_atlan_client
from application_sdk.credentials import AtlanApiToken

cred = AtlanApiToken(api_token="my-token", base_url="https://my-tenant.atlan.com")
client = create_async_atlan_client(cred)
```

Inside an `App`, use the `AtlanClientMixin` to get a cached, per-run client:

```python
from application_sdk.credentials.atlan_client import AtlanClientMixin
from application_sdk.credentials import atlan_api_token_ref

class MyConnector(App, AtlanClientMixin):
    @task
    async def my_task(self, input: MyInput) -> MyOutput:
        ref = atlan_api_token_ref("atlan-token")  # name of your secret store entry
        client = await self.get_or_create_async_atlan_client(ref)
        # client is an AsyncAtlanClient; cached for the lifetime of this run
```

### AtlanAuthClient

```python
# v2
from application_sdk.clients.atlan_auth import AtlanAuthClient
auth = AtlanAuthClient()
headers = await auth.get_authenticated_headers()

# v3
from application_sdk.credentials import OAuthClientCredential
from application_sdk.credentials.oauth import OAuthTokenService
cred = OAuthClientCredential(client_id="...", client_secret="...", token_url="...")
service = OAuthTokenService(cred)
headers = await service.get_authenticated_headers()
```

---

## Step 11: Migrate Heartbeating

In v2, you applied `@auto_heartbeater` to activity methods to prevent Temporal from
timing them out. In v3, heartbeating is built into `@task` — no decorator needed.

### v2

```python
from application_sdk.activities.common.utils import auto_heartbeater

class MyActivities(ActivitiesInterface):
    @auto_heartbeater
    @activity.defn
    async def long_running_task(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # heartbeats sent every 10 seconds automatically
        ...
```

### v3

```python
from application_sdk.app import App, task

class MyConnector(App):
    # heartbeat_timeout_seconds: Temporal kills the task if no heartbeat is received
    # auto_heartbeat_seconds: framework sends a heartbeat every N seconds automatically
    @task(timeout_seconds=3600, heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10)
    async def long_running_task(self, input: MyInput) -> MyOutput:
        # heartbeats are sent in a background loop — no decorator needed
        ...
```

### Manual heartbeats with progress data

Send a heartbeat with progress details so the task can resume from where it left off
if Temporal restarts it:

```python
from application_sdk.contracts import HeartbeatDetails

class MyProgress(HeartbeatDetails):
    last_processed_id: str
    records_done: int

@task(heartbeat_timeout_seconds=60)
async def process_batches(self, input: MyInput) -> MyOutput:
    # Resume after a retry
    prev = await self.task_context.get_heartbeat_details(MyProgress)
    start_id = prev.last_processed_id if prev else None

    for batch in get_batches(start_from=start_id):
        process(batch)
        await self.task_context.heartbeat(MyProgress(
            last_processed_id=batch.id,
            records_done=batch.count,
        ))
```

### Blocking sync operations

If you call blocking (non-async) code inside a task (e.g., a sync SDK or driver),
use `run_in_thread` to avoid blocking the event loop and stalling heartbeats:

```python
@task(heartbeat_timeout_seconds=60)
async def fetch(self, input: MyInput) -> MyOutput:
    result = await self.task_context.run_in_thread(my_sync_function, arg1, arg2)
    return MyOutput(data=result)
```

---

## Step 12: App Lifecycle Hooks

v3 adds structured lifecycle hooks that replace ad-hoc cleanup logic that was
previously scattered across activities or added as a final workflow step.

### on_complete

`on_complete` is called after `run()` finishes (whether successful or not):

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        ...

    async def on_complete(self, success: bool) -> None:
        if success:
            await self.notify_downstream()
        # cleanup is automatic — see below
```

### Built-in cleanup tasks

Two cleanup tasks are built into every `App`. Call them from `run()` or `on_complete()`:

```python
async def on_complete(self, success: bool) -> None:
    # Remove local temp files tracked via FileReference
    await self.cleanup_files()

    # Remove object store artifacts uploaded during this run
    await self.cleanup_storage()
```

`cleanup_files()` and `cleanup_storage()` are also available as individual workflow steps
if you need to trigger cleanup mid-run (e.g., after a particularly large intermediate step).

---

## Step 13: Migrate Test Utilities

### Import paths

```python
# v2 — all of these are deprecated
from application_sdk.test_utils import ...
from application_sdk.test_utils.credentials import MockCredentialStore
from application_sdk.test_utils.scale_data_generator import ...

# v3
from application_sdk.testing import (
    MockStateStore,
    MockSecretStore,
    MockBinding,
    MockPubSub,
    MockCredentialStore,
    MockHeartbeatController,
)
from application_sdk.testing.scale_data_generator import ...
```

### Using mock infrastructure in tests

In v3 you can test `@task` methods without any Dapr sidecar running:

```python
import pytest
from application_sdk.testing import MockSecretStore, MockStateStore
from application_sdk.infrastructure.context import set_infrastructure
from application_sdk.infrastructure import InfrastructureContext

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    return ctx

async def test_my_task(infra):
    connector = MyConnector()
    output = await connector.my_task(MyInput(connection_id="test"))
    assert output.record_count > 0
```

### Pytest fixtures provided by the framework

```python
# conftest.py — import the autouse fixture to ensure registry isolation between tests
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401
```

The `clean_app_registry` fixture resets `AppRegistry` and `TaskRegistry` between tests,
preventing subclass registrations in one test from leaking into the next.

### MockCredentialStore

```python
from application_sdk.testing import MockCredentialStore
from application_sdk.credentials.resolver import CredentialResolver

mock = MockCredentialStore()
ref = mock.add_api_key("test-key", api_key="test-secret")

resolver = CredentialResolver(mock.secret_store)
cred = await resolver.resolve(ref)
assert cred.api_key == "test-secret"
```

### MockHeartbeatController

For testing tasks that use `self.task_context.heartbeat()` without running inside Temporal:

```python
from application_sdk.testing import MockHeartbeatController

controller = MockHeartbeatController()
# inject into task context or pass to the function under test
# controller.recorded_heartbeats contains all calls made
```

### Running locally without Dapr

```python
from application_sdk.infrastructure.secrets import InMemorySecretStore
from application_sdk.infrastructure.state import InMemoryStateStore
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyConnector,
    handler_class=MyHandler,
    secret_store=InMemorySecretStore({"my-api-key": "test-value"}),
    state_store=InMemoryStateStore(),
))
```

---

## Removed in v3.1.0

All of the following emit `DeprecationWarning` on import in v3.0.x and will be **removed** in v3.1.0.

### Application / Entry Point

| Deprecated | Replacement |
|---|---|
| `application_sdk.application.BaseApplication` | `application_sdk.app.App` + `application_sdk.main.run_dev_combined` |
| `application_sdk.application.metadata_extraction.sql.BaseSQLMetadataExtractionApplication` | `application_sdk.templates.SqlMetadataExtractor` |

### Worker

| Deprecated | Replacement |
|---|---|
| `application_sdk.worker.Worker` | `application_sdk.execution.create_worker` |

### Workflows

| Deprecated | Replacement |
|---|---|
| `application_sdk.workflows.WorkflowInterface` | `application_sdk.app.App` with `@task` |
| `application_sdk.workflows.metadata_extraction.MetadataExtractionWorkflow` | `application_sdk.templates.SqlMetadataExtractor` |
| `application_sdk.workflows.metadata_extraction.sql.BaseSQLMetadataExtractionWorkflow` | `application_sdk.templates.SqlMetadataExtractor` |
| `application_sdk.workflows.metadata_extraction.incremental_sql.IncrementalSQLMetadataExtractionWorkflow` | `application_sdk.templates.IncrementalSqlMetadataExtractor` |
| `application_sdk.workflows.query_extraction.QueryExtractionWorkflow` | `application_sdk.templates.SqlQueryExtractor` |
| `application_sdk.workflows.query_extraction.sql.SQLQueryExtractionWorkflow` | `application_sdk.templates.SqlQueryExtractor` |

### Activities

| Deprecated | Replacement |
|---|---|
| `application_sdk.activities.ActivitiesInterface` | `application_sdk.app.App` with `@task` |
| `application_sdk.activities.common.models.ActivityStatistics` | `application_sdk.common.models.TaskStatistics` |
| `application_sdk.activities.common.models.ActivityResult` | `application_sdk.common.models.TaskResult` |
| `application_sdk.activities.common.utils` | `application_sdk.execution._temporal.activity_utils` |
| `application_sdk.activities.common.sql_utils` | `application_sdk.common.sql_utils` |
| `application_sdk.activities.metadata_extraction.base` | `application_sdk.templates.BaseMetadataExtractor` |
| `application_sdk.activities.metadata_extraction.sql` | `application_sdk.templates.SqlMetadataExtractor` |
| `application_sdk.activities.metadata_extraction.incremental` | `application_sdk.templates.IncrementalSqlMetadataExtractor` |
| `application_sdk.activities.query_extraction.sql` | `application_sdk.templates.SqlQueryExtractor` |

### Handlers

| Deprecated | Replacement |
|---|---|
| `application_sdk.handlers.HandlerInterface` | `application_sdk.handler.base.Handler` |
| `application_sdk.handlers.base.BaseHandler` | `application_sdk.handler.base.DefaultHandler` |
| `application_sdk.handlers.sql.BaseSQLHandler` | `application_sdk.templates` (SQL logic absorbed) |

### Services

| Deprecated | Replacement |
|---|---|
| `application_sdk.services.objectstore.ObjectStore` | `application_sdk.storage` module |
| `application_sdk.services.statestore.StateStore` | `application_sdk.infrastructure.state.StateStore` |
| `application_sdk.services.secretstore.SecretStore` | `application_sdk.infrastructure.secrets.SecretStore` |
| `application_sdk.services.eventstore.EventStore` | automatic via interceptor; or `get_infrastructure().event_binding` |
| `application_sdk.services.atlan_storage.AtlanStorage` | `application_sdk.storage` or `App.upload()` / `App.download()` |

### Clients

| Deprecated | Replacement |
|---|---|
| `application_sdk.clients.atlan.get_client` | removed — sync client no longer supported |
| `application_sdk.clients.atlan.get_async_client` | `application_sdk.credentials.atlan_client.create_async_atlan_client` |
| `application_sdk.clients.atlan_auth.AtlanAuthClient` | `application_sdk.credentials.OAuthTokenService` |

### Test Utilities

| Deprecated | Replacement |
|---|---|
| `application_sdk.test_utils` | `application_sdk.testing` |
| `application_sdk.test_utils.credentials.MockCredentialStore` | `application_sdk.testing.MockCredentialStore` |
| `application_sdk.test_utils.scale_data_generator` | `application_sdk.testing.scale_data_generator` |
