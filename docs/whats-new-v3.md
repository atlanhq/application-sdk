# What's New in v3

Application SDK v3 is a ground-up rethink of how you build apps on the Atlan platform.
If you know v2 well, this guide will orient you quickly — it explains *what* changed, *why*,
and shows each change side-by-side.

> For a step-by-step upgrade checklist, see [`upgrade-guide-v3.md`](upgrade-guide-v3.md).
> For automated tooling, see [`tools/migrate_v3/`](../tools/migrate_v3/README.md).

---

## At a Glance

Three interlocking changes define v3:

| Change | v2 | v3 |
|--------|----|----|
| **App model** | `WorkflowInterface` + `ActivitiesInterface` (two classes) | `App` + `@task` (one class) |
| **Data contracts** | `Dict[str, Any]` everywhere | Typed `Input` / `Output` Pydantic models |
| **Infrastructure** | Direct Dapr/Temporal SDK calls | Protocol-based interfaces via `self.context` |

All v2 imports still work in **v3.0.x** — they emit `DeprecationWarning` and will be removed in v3.1.0.

**Quick import mapping:**

| v2 | v3 |
|----|----|
| `application_sdk.workflows.WorkflowInterface` | `application_sdk.app.App` |
| `application_sdk.activities.ActivitiesInterface` | `application_sdk.app.task` |
| `application_sdk.handlers.HandlerInterface` | `application_sdk.handler.Handler` |
| `application_sdk.worker.Worker` | `application_sdk.execution.create_worker` |
| `application_sdk.application.BaseApplication` | `application_sdk.main.run_dev_combined` |
| `application_sdk.services.objectstore.ObjectStore` | `application_sdk.storage` |
| `application_sdk.services.statestore.StateStore` | `application_sdk.infrastructure.StateStore` |
| `application_sdk.services.secretstore.SecretStore` | `application_sdk.infrastructure.SecretStore` |
| `application_sdk.clients.atlan.get_async_client` | `application_sdk.credentials.atlan_client.create_async_atlan_client` |
| `application_sdk.test_utils.*` | `application_sdk.testing.*` |

---

## The Three Big Ideas

### 1. One class, not two

In v2, every connector required a `WorkflowInterface` subclass (the orchestrator) and an
`ActivitiesInterface` subclass (the workers), wired together with `execute_activity_method`
calls and explicit class references. To do this correctly you had to understand Temporal's
workflow/activity split, determinism rules, sandbox restrictions, and heartbeating protocol.

In v3, you write one `App` subclass. Orchestration logic goes in `run()`. Anything with an
external side-effect gets a `@task` decorator. The framework applies the Temporal decorators
under the hood, wires heartbeating automatically, and registers everything with the worker
on startup. You never import from `temporalio` directly.

### 2. Types at the boundary, validated at import time

In v2, every workflow and activity passed `Dict[str, Any]`. This was flexible but fragile:
Temporal has a hard 2 MB payload limit, and an unbounded list or a large bytes blob would
silently work in development and blow up in production. There was no type checking, no
self-documentation in the Temporal UI, and no safe evolution story.

In v3, every `App.run()` and `@task` method takes exactly one `Input` and returns exactly
one `Output` — both Pydantic models. The SDK validates your contract fields at *import time*:
`Any`, bare `bytes`, and unbounded `list`/`dict` raise `PayloadSafetyError` before your app
even starts. Large data that won't fit in a Temporal payload goes through `FileReference`
(stored in object storage; only a lightweight reference crosses the Temporal boundary).

### 3. Infrastructure behind a context, not a sidecar

In v2, accessing state, secrets, or object storage meant calling Dapr-coupled service classes
(`StateStore.get_state()`, `SecretStore.get_credentials()`, `ObjectStore.upload_file()`).
These calls required a running Dapr sidecar, made tests slow and environment-dependent, and
tied your code to Dapr's gRPC message size limits.

In v3, infrastructure is accessed through `self.context` inside tasks and handlers. The
framework injects the right implementation — Dapr-backed in production, in-memory mocks in
tests. The object store is now backed by `obstore` (talking directly to S3/GCS/Azure/LocalFS),
so there are no gRPC size limits and no sidecar dependency for storage.

---

## Deep Dives

### App + @task replaces Workflow + Activities

#### v2 — two classes, Temporal decorators, manual wiring

```python
from temporalio import workflow, activity
from application_sdk.workflows import WorkflowInterface
from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.common.utils import auto_heartbeater

@workflow.defn
class MyConnectorWorkflow(WorkflowInterface):
    activities_cls = MyConnectorActivities

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        activities = MyConnectorActivities()
        await workflow.execute_activity_method(
            activities.fetch_data,
            args=[workflow_config],
            start_to_close_timeout=timedelta(seconds=3600),
            heartbeat_timeout=timedelta(seconds=60),
        )

    @staticmethod
    def get_activities(activities):
        return [activities.fetch_data]

class MyConnectorActivities(ActivitiesInterface):
    @auto_heartbeater
    @activity.defn
    async def fetch_data(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        # ... fetch logic
        return {"rows_fetched": 42}
```

#### v3 — one class, @task, heartbeating built-in

```python
from application_sdk.app import App, task, Input, Output

class FetchInput(Input):
    connection_id: str

class FetchOutput(Output):
    rows_fetched: int

class MyConnector(App):
    @task(timeout_seconds=3600, heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10)
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        # ... fetch logic
        return FetchOutput(rows_fetched=42)

    async def run(self, input: FetchInput) -> FetchOutput:
        return await self.fetch_data(input)
```

**What changed and why:**

- `@workflow.defn` / `@activity.defn` are applied automatically by `App.__init_subclass__` —
  you never need to import from `temporalio` directly.
- `execute_activity_method` is replaced by a direct `await self.fetch_data(input)` call.
  The framework detects you're in a workflow context and routes the call through Temporal
  activities correctly, including retry and timeout handling.
- `@auto_heartbeater` is gone. Heartbeating is configured as keyword arguments on `@task`.
  The framework runs the heartbeat loop in the background — your task body doesn't change.
- `get_activities()` is gone. The worker discovers all `@task` methods automatically via
  `TaskRegistry` at startup.
- `activities_cls` attribute is gone. Tasks are private to their parent `App`.

**Passthrough modules** were previously passed to the `Worker` constructor. In v3 they live
on the class:

```python
# v2 — at worker startup
Worker(workflow_client=client, passthrough_modules=["my_connector"])

# v3 — on the class
class MyConnector(App, passthrough_modules=["my_connector", "third_party_lib"]):
    ...
```

**SQL template apps** get an even bigger reduction. The whole
`BaseSQLMetadataExtractionWorkflow` + `BaseSQLMetadataExtractionActivities` split collapses
into a single `SqlMetadataExtractor` subclass where you only override the tasks you
customise:

```python
# v2
@workflow.defn
class MyWorkflow(BaseSQLMetadataExtractionWorkflow):
    activities_cls = MyActivities

class MyActivities(BaseSQLMetadataExtractionActivities):
    async def fetch_databases(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
        return ActivityStatistics(chunk_count=1, total_record_count=10)

# v3
class MyExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
```

---

### Typed Contracts replace Dict[str, Any]

Every `App.run()` and `@task` method in v3 takes a single `Input` subclass and returns a
single `Output` subclass. Both inherit from Pydantic `BaseModel`.

#### Why this matters

Temporal serialises workflow and activity arguments as JSON. If a payload exceeds ~2 MB,
Temporal rejects it — and in v2, nothing stopped you from accidentally putting a large list
or a raw `bytes` blob into a `Dict[str, Any]`. In v3, `Input`/`Output` subclasses are
inspected at *class definition time*. Forbidden types raise `PayloadSafetyError` immediately:

```python
# v2 — no validation, silently dangerous
async def fetch(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
    return {"rows": [row for row in big_table]}  # could be megabytes — no warning

# v3 — forbidden types caught at import time
class FetchOutput(Output):
    rows: list[str]  # PayloadSafetyError: unbounded list not allowed
    rows: Annotated[list[str], MaxItems(10_000)]  # ok — bounded
    data: bytes  # PayloadSafetyError: bytes not allowed
    data: FileReference  # ok — large data goes to object store
```

**Forbidden types:** `Any`, `bytes`, `bytearray`, unbounded `list[T]`, unbounded `dict[K, V]`.

**Safe alternatives:**

| Need | Use |
|------|-----|
| Bounded list | `Annotated[list[str], MaxItems(1000)]` |
| Large binary / file data | `FileReference` (stored in object store) |
| Enum values | `SerializableEnum` (StrEnum that survives Temporal replay) |

#### FileReference: large data across tasks

When a task produces a file too large to pass through Temporal, declare it as a `FileReference`
in the contract. The framework handles the rest automatically:

- A `FileReference` in a task **output** is uploaded to object storage before the result
  crosses the Temporal boundary.
- A `FileReference` in a task **input** is downloaded to the local filesystem before the
  task runs — but only if the file isn't already there. If the two tasks run back-to-back on
  the same worker, the local file from the previous task is still present and the download is
  skipped entirely (verified via sha256 sidecar comparison).

```python
from application_sdk.contracts.types import FileReference

class FetchOutput(Output):
    results: FileReference  # automatically uploaded when this output leaves the task

class ProcessInput(Input):
    results: FileReference  # automatically downloaded (or skipped) before the task runs

class MyConnector(App):
    @task
    async def fetch(self, input: FetchInput) -> FetchOutput:
        write_parquet(data, "/tmp/results.parquet")
        return FetchOutput(results=FileReference.from_local("/tmp/results.parquet"))

    @task
    async def process(self, input: ProcessInput) -> ProcessOutput:
        data = read_parquet(input.results.local_path)  # file is already present, even if worker crashed in-between
        ...

    async def run(self, input: FetchInput) -> ProcessOutput:
        fetch_out = await self.fetch(input)
        return await self.process(ProcessInput(results=fetch_out.results))
```

#### Contract evolution rules

Because Temporal may replay a workflow with a payload serialised before you deployed a new
version, contracts must evolve carefully:

- **Safe:** add a new field with a default value
- **Unsafe:** remove a field, rename a field, change a field's type

```python
# Safe evolution — existing running workflows see retry_count=3 for old payloads
class FetchInput(Input):
    connection_id: str
    retry_count: int = 3  # new field — must have a default
```

---

### Infrastructure Abstraction

#### v2 — direct Dapr calls

```python
from application_sdk.services.statestore import StateStore
from application_sdk.services.secretstore import SecretStore
from application_sdk.services.objectstore import ObjectStore

state = await StateStore.get_state(workflow_id, StateType.WORKFLOWS)
creds = await SecretStore.get_credentials({"credential_guid": guid})
await ObjectStore.upload_file(source="/local/file.parquet", key="output/file.parquet")
data = await ObjectStore.get_content("config/settings.json")
```

#### v3 — self.context inside tasks and handlers

```python
class MyConnector(App):
    @task
    async def fetch(self, input: FetchInput) -> FetchOutput:
        # State store
        prev_state = await self.context.load_state("last_run")
        await self.context.save_state("last_run", {"timestamp": self.now().isoformat()})

        # Secret store
        api_key = await self.context.get_secret("my-api-key")

        # Object storage (upload/download via built-in App tasks)
        result = await self.upload(UploadInput(
            local_path="/tmp/data.parquet",
            storage_path="output/data.parquet",
        ))
        ...
```

**The key improvements:**

- **No Dapr sidecar in tests.** Inject `MockStateStore` / `MockSecretStore` from `application_sdk.testing.mocks` and run your task methods in pure Python — no sidecar, no gRPC, no environment variables.
- **No gRPC size limits.** Object storage is now backed by `obstore`, which talks directly to
  S3/GCS/Azure or the local filesystem. The Dapr gRPC binding had a ~4 MB message limit that
  bit connectors with large files; that limit is gone.
- **Portable.** Because infrastructure is injected via Protocols, the framework could swap
  Temporal or Dapr for another engine without touching your app code.

**Infrastructure access outside of tasks** (standalone scripts, utilities):

```python
from application_sdk.storage import upload_file, download_file, list_keys, delete

await upload_file("output/file.parquet", local_path="/local/file.parquet")
await download_file("config/settings.json", local_path="/tmp/settings.json")
keys = await list_keys(prefix="output/")
await delete("output/old.parquet")
```

**Local dev with custom secrets:**

```python
from application_sdk.testing.mocks import MockSecretStore
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(
    MyConnector,
    handler_class=MyHandler,
    secret_store=MockSecretStore({"my-api-key": "dev-secret"}),
))
```

---

### Handler — typed contracts, no load()

#### v2 — untyped, manual load()

```python
from application_sdk.handlers import HandlerInterface

class MyHandler(HandlerInterface):
    async def load(self, *args, **kwargs) -> None:
        # manually bootstrap: connect clients, load secrets, etc.
        self._client = await connect(...)

    async def test_auth(self, *args, **kwargs) -> bool:
        return await self._client.ping()

    async def preflight_check(self, *args, **kwargs):
        return {"status": "ready"}

    async def fetch_metadata(self, *args, **kwargs):
        return [{"value": "schema", "title": "Schema", "children": [...]}]
```

#### v3 — typed, context injected automatically

```python
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus,
    MetadataInput, MetadataOutput,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = await self.context.get_secret("my-api-key")
        ok = await verify_key(api_key)
        return AuthOutput(status=AuthStatus.SUCCESS if ok else AuthStatus.FAILED)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(fields=[])
```

**What changed and why:**

- `load()` is removed. The service layer injects `self.context` before each method call and
  clears it after. This eliminates the bootstrapping ceremony and makes handlers stateless,
  which is important for concurrent requests.
- Method signatures are now fully typed. This enables input validation, OpenAPI schema
  generation, and pyright type checking end-to-end.
- The `fetch_metadata` response shape changed: v2 returned a nested `[{value, title, children}]`
  tree; v3 returns a flat `SqlMetadataOutput(objects=[SqlMetadataObject(...)])`.
- Error handling is structured: raise `HandlerError(message, http_status=400)` to get a
  consistent HTTP error response.

---

### Entry Points and Worker Setup

#### v2 — BaseApplication + explicit Worker

```python
from application_sdk.application.metadata_extraction.sql import (
    BaseSQLMetadataExtractionApplication,
)
from application_sdk.worker import Worker
from application_sdk.clients.temporal import TemporalWorkflowClient

# Wiring the application
app = BaseSQLMetadataExtractionApplication(
    name="my-connector",
    client_class=MyClient,
    handler_class=MyHandler,
)
await app.setup_workflow(
    workflow_and_activities_classes=[(MyWorkflow, MyActivities)]
)
await app.start()

# Or manually:
client = TemporalWorkflowClient(host=host, namespace=namespace)
await client.load()
worker = Worker(
    workflow_client=client,
    workflow_classes=[MyWorkflow],
    workflow_activities=[MyActivities()],
    passthrough_modules=["my_connector"],
)
await worker.run()
```

#### v3 — CLI modes or run_dev_combined

```bash
# Production — separate pods for handler and worker
application-sdk --mode handler --app my_package.apps:MyExtractor
application-sdk --mode worker  --app my_package.apps:MyExtractor

# Local dev / SDR — combined
application-sdk --mode combined --app my_package.apps:MyExtractor
```

```python
# Programmatic (local dev, integration tests)
from application_sdk.main import run_dev_combined

asyncio.run(run_dev_combined(MyExtractor, handler_class=MyHandler))
```

**Three modes:**

| Mode | What runs | Typical use |
|------|-----------|-------------|
| `worker` | Temporal worker only | Production worker pods |
| `handler` | HTTP handler service only | Production handler pods |
| `combined` | Both in one process | Local dev, SDR |

**Worker auto-discovery** — you no longer register workflow or activity classes explicitly.
`create_worker()` scans `AppRegistry` and `TaskRegistry` (populated at import time when your
`App` subclasses are defined) and registers everything automatically:

```python
from application_sdk.execution import create_worker
from application_sdk.execution._temporal.backend import create_temporal_client

client = await create_temporal_client()  # reads TEMPORAL_HOST, TEMPORAL_NAMESPACE etc.
worker = await create_worker(client)     # discovers all App subclasses automatically
await worker.run()
```

---

### Credentials

#### v2 — credential_guid + raw dict

```python
class ExtractionInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""

# In an activity:
from application_sdk.services.secretstore import SecretStore
credentials = await SecretStore.get_credentials({"credential_guid": input.credential_guid})
await client.load(credentials)  # dict[str, Any]
```

#### v3 — CredentialRef → typed Credential

```python
from application_sdk.credentials import CredentialRef, api_key_ref, ApiKeyCredential

class ExtractionInput(Input):
    credential_ref: CredentialRef | None = None

# In a @task method:
cred = await self.context.resolve_credential(input.credential_ref)
assert isinstance(cred, ApiKeyCredential)
headers = cred.to_headers()  # {"X-API-Key": "secret"}
```

**Built-in credential types:**

| Type | Factory | Utility methods |
|------|---------|----------------|
| `BasicCredential` | `basic_ref(name)` | `to_auth_header()` |
| `ApiKeyCredential` | `api_key_ref(name)` | `to_headers()` |
| `BearerTokenCredential` | `bearer_token_ref(name)` | `to_auth_header()`, `is_expired()` |
| `OAuthClientCredential` | `oauth_client_ref(name)` | `to_headers()`, `needs_refresh()` |
| `CertificateCredential` | `certificate_ref(name)` | — |
| `GitSshCredential` | `git_ssh_ref(name)` | — |
| `GitTokenCredential` | `git_token_ref(name)` | `to_auth_header()` |
| `AtlanApiToken` | `atlan_api_token_ref(name)` | `to_auth_header()`, `validate()` |
| `AtlanOAuthClient` | `atlan_oauth_client_ref(name)` | `to_headers()`, `validate()` |

**Backward compatibility** — `credential_guid` still works. When only a GUID is present in
the input, the SDK auto-wraps it via `legacy_credential_ref()`, so you can upgrade gradually:

```python
ExtractionInput(credential_guid="abc-123")          # legacy — still works
ExtractionInput(credential_ref=api_key_ref("prod")) # new typed path
```

If you need to pass credentials to a legacy client that expects a raw dict:

```python
raw = await self.context.resolve_credential_raw(ref)
await legacy_client.load(raw)  # dict[str, Any]
```

**AtlanClientMixin** — for apps that call the Atlan SDK directly:

```python
from application_sdk.credentials.atlan_client import AtlanClientMixin
from application_sdk.credentials import atlan_api_token_ref

class MyConnector(App, AtlanClientMixin):
    @task
    async def upload_to_atlan(self, input: UploadInput) -> UploadOutput:
        ref = atlan_api_token_ref("atlan-token")
        client = await self.get_or_create_async_atlan_client(ref)
        # client is an AsyncAtlanClient, cached per run
```

If you previously used `AtlanAuthClient` to obtain headers for raw HTTP calls to Atlan,
the v3 answer is to stop making raw requests entirely — use the `AsyncAtlanClient` you get
from `AtlanClientMixin` (shown above) as a full-fledged SDK client instead.

---

### Heartbeating

#### v2 — @auto_heartbeater decorator

```python
from application_sdk.activities.common.utils import auto_heartbeater

class MyActivities(ActivitiesInterface):
    @auto_heartbeater          # sends a heartbeat every 10s via a background thread
    @activity.defn
    async def long_running(self, args: Dict[str, Any]) -> Dict[str, Any]:
        ...
```

#### v3 — parameters on @task

```python
class MyConnector(App):
    @task(
        timeout_seconds=3600,
        heartbeat_timeout_seconds=60,   # Temporal kills the task if no heartbeat in 60s
        auto_heartbeat_seconds=10,      # framework sends a heartbeat every 10s
    )
    async def long_running(self, input: MyInput) -> MyOutput:
        ...  # heartbeats run automatically in a background asyncio loop
```

**Typed progress heartbeats** allow a task to resume from where it left off if Temporal
restarts it (e.g., after a worker pod is replaced):

```python
from application_sdk.contracts import HeartbeatDetails

class MyProgress(HeartbeatDetails):
    last_id: str
    records_done: int

@task(heartbeat_timeout_seconds=60)
async def process_batches(self, input: MyInput) -> MyOutput:
    prev = await self.task_context.get_heartbeat_details(MyProgress)
    start_id = prev.last_id if prev else None

    for batch in get_batches(start_from=start_id):
        process(batch)
        await self.task_context.heartbeat(MyProgress(last_id=batch.id, records_done=batch.count))
```

**Blocking sync code** — if your task calls a blocking (non-async) library (e.g., a sync DB
driver), use `run_in_thread` to avoid stalling the event loop and blocking the heartbeat:

```python
@task(heartbeat_timeout_seconds=60)
async def fetch(self, input: FetchInput) -> FetchOutput:
    result = await self.task_context.run_in_thread(sync_db_call, input.query)
    return FetchOutput(data=result)
```

---

### Lifecycle Hooks

v3 adds structured lifecycle hooks that replace ad-hoc cleanup logic scattered across
activities or tacked on as a final workflow step.

#### on_complete

`on_complete(success: bool)` is called after `run()` finishes, whether it succeeded or raised
an exception. v2 had no equivalent — cleanup was either a final activity or omitted entirely.

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        ...

    async def on_complete(self, success: bool) -> None:
        if success:
            await self.notify_downstream()
        await self.cleanup_files()    # remove local temp files tracked via FileReference
        await self.cleanup_storage()  # remove object store artifacts from this run
```

#### Built-in cleanup tasks

Two cleanup tasks are available on every `App`:

- `cleanup_files()` — removes local temporary files whose paths are tracked via `FileReference`
  objects that appeared in task outputs during this run.
- `cleanup_storage()` — removes object store artifacts uploaded with the default
  `StorageTier.TRANSIENT` tier. Files uploaded with `StorageTier.RETAINED` (stored under
  `artifacts/apps/{app}/workflows/{wf_id}/{run_id}/`, subject to object store lifecycle
  policy) or `StorageTier.PERSISTENT` (stored under `persistent-artifacts/`, never deleted)
  are left untouched. Set the tier on a `FileReference` to opt specific files out of
  automatic cleanup.

Both can also be called mid-run if you need to reclaim space after a particularly large
intermediate step.

---

### Testing

#### Import path change

```python
# v2
from application_sdk.test_utils.credentials import MockCredentialStore

# v3
from application_sdk.testing import (
    MockStateStore, MockSecretStore, MockBinding,
    MockPubSub, MockCredentialStore, MockHeartbeatController,
)
```

#### Sidecar-free task testing

The biggest testing improvement: you can now test `@task` methods without any Dapr sidecar
or Temporal server. Inject in-memory implementations of the infrastructure interfaces:

```python
import pytest
from application_sdk.testing import MockSecretStore, MockStateStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure

@pytest.fixture(autouse=True)
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)

async def test_fetch_data():
    connector = MyConnector()
    output = await connector.fetch_data(FetchInput(connection_id="test"))
    assert output.rows_fetched > 0
```

#### clean_app_registry fixture

`App.__init_subclass__` registers every subclass in `AppRegistry` at import time. If test
modules define `App` subclasses, they can bleed into each other. Import the autouse fixture
to reset the registries between tests:

```python
# conftest.py
from application_sdk.testing.fixtures import clean_app_registry  # noqa: F401
```

#### MockHeartbeatController

For testing tasks that emit heartbeats without running inside Temporal:

```python
from application_sdk.testing import MockHeartbeatController

controller = MockHeartbeatController()
# inject into task context during test setup
# controller.recorded_heartbeats contains all heartbeat calls
```

---

## Further Reading

- **Step-by-step upgrade:** [`docs/upgrade-guide-v3.md`](upgrade-guide-v3.md)
- **Automated upgrade tooling:** [`tools/migrate_v3/README.md`](../tools/migrate_v3/README.md)
- **Design rationale (ADRs):**
  - [ADR-0005: Infrastructure Abstraction](adr/0005-infrastructure-abstraction.md)
  - [ADR-0006: Schema-Driven Contracts](adr/0006-schema-driven-contracts.md)
  - [ADR-0007: Apps as Coordination Unit](adr/0007-apps-as-coordination-unit.md)
  - [ADR-0008: Payload-Safe Bounded Types](adr/0008-payload-safe-bounded-types.md)
  - [ADR-0010: Async-First Blocking Code](adr/0010-async-first-blocking-code.md)
