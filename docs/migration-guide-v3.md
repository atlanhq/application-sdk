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

## Step 7: Migrate to Typed Credentials

v3 introduces a typed credential system that replaces bare `credential_guid: str` +
`Dict[str, Any]` with a `CredentialRef` → typed `Credential` pipeline.

### Before (credential_guid pattern)

```python
from dataclasses import dataclass
from application_sdk.contracts.base import Input

@dataclass
class ExtractionInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""

# In @task method:
from application_sdk.services.secretstore import SecretStore  # deprecated v2

credentials = await SecretStore.get_credentials({"credential_guid": input.credential_guid})
await client.load(credentials)  # dict[str, Any]
```

### After (CredentialRef pattern)

```python
from dataclasses import dataclass
from application_sdk.contracts.base import Input
from application_sdk.credentials import CredentialRef, api_key_ref, ApiKeyCredential

@dataclass
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
import dataclasses

@dataclasses.dataclass(frozen=True)
class MyCredential:
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

### Testing with MockCredentialStore

Instead of mocking Dapr or patching secret stores:

```python
from application_sdk.test_utils.credentials import MockCredentialStore
from application_sdk.credentials.resolver import CredentialResolver

mock = MockCredentialStore()
ref = mock.add_api_key("test-key", api_key="test-secret")

resolver = CredentialResolver(mock.secret_store)
cred = await resolver.resolve(ref)
assert cred.api_key == "test-secret"
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
