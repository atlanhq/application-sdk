# Architecture

The Application SDK builds durable, fault-tolerant applications on top of two underlying technologies:

- **[Temporal](https://docs.temporal.io/)** — durable workflow orchestration. Workflows survive failures and resume from the last checkpoint; the SDK hides all workflow/activity plumbing behind `App` and `@task`.
- **[Dapr](https://dapr.io/)** — portable infrastructure runtime. State, secrets, pub/sub, and bindings are accessed through Protocol-based interfaces; Dapr is an implementation detail, not a dependency of app code.

Neither technology is exposed directly to app developers. Everything surfaces through the abstractions described below.

---

## Mental Model: Apps and Tasks

Think of it like rock climbing with anchors.

- Your app's `run()` method is your **route** — which steps to take and in what order.
- `@task` methods are **anchors** you set along the way.

Each time a task completes, you've clipped in — progress is secured. If something fails (network timeout, pod restart, OOM), execution resumes from the last completed task, not from scratch.

**Completed tasks hold.** Once a task succeeds, it is never re-executed on retry. The framework remembers.

| Task duration | Assessment | Example |
|---------------|------------|---------|
| < 1 min | Too granular | Individual API calls |
| **1–5 min** | **Ideal** | Batch of API calls, a page of records |
| 5–10 min | Acceptable | Large batch, complex transformation |
| > 15 min | Risky | Add more anchors |

---

## Core Abstractions

### App — the route

An `App` is a unit of durable execution. It has a typed input, a typed output, and a `run()` method that defines the sequence of steps.

```python
from application_sdk.app import App, Input, Output, task

class MyInput(Input):
    source_id: str

class MyOutput(Output):
    record_count: int

class MyConnector(App):

    @task
    async def extract(self, input: MyInput) -> MyOutput:
        # Side effects happen here: network calls, file I/O, database access
        records = await fetch_records(input.source_id)
        return MyOutput(record_count=len(records))

    async def run(self, input: MyInput) -> MyOutput:
        # run() is the plan — deterministic, no side effects
        return await self.extract(input)
```

Key properties:
- **Auto-registered** — `App.__init_subclass__` registers the class in `AppRegistry` automatically; no manual wiring.
- **Name derived from class** — `MyConnector` → `my-connector` (PascalCase → kebab-case).
- **Determinism required in `run()`** — use `self.now()` instead of `datetime.now()`, `self.uuid()` instead of `uuid.uuid4()`. Tasks can do anything; `run()` must be deterministic because Temporal may replay it.

### @task — the anchors

Tasks are where side effects happen. Each completed task is a durable checkpoint.

```python
@task(
    timeout_seconds=600,            # Activity start-to-close timeout (default 10 min)
    retry_max_attempts=3,           # Default 3 retries
    retry_max_interval_seconds=30,  # Max backoff between retries
)
async def my_task(self, input: TaskInput) -> TaskOutput:
    ...
```

- `@task` validates the single-model contract (one `Input` model, one `Output` model) **at class definition time** — before any code runs.
- Tasks run outside the Temporal sandbox; they can import any library without passthrough concerns.
- Auto-heartbeating is built in. Use `self.task_context.run_in_thread(fn, *args)` to run blocking code without blocking the event loop (see [ADR-0010](../adr/0010-async-first-blocking-code.md)).

### Typed Contracts — Input and Output

Every `run()` and `@task` boundary uses exactly one `Input` model and one `Output` model. The framework enforces this at class definition time.

```python
class ExtractInput(Input):
    connection_id: str
    max_records: int = 1000          # Default = safe for backwards compatibility

class ExtractOutput(Output):
    record_count: int
    checkpoint: str
```

**Evolution rules:** add new fields with defaults (backwards compatible); never remove fields or change types. See [ADR-0006](../adr/0006-schema-driven-contracts.md).

**Payload safety:** `Any`, `bytes`, and unbounded `list`/`dict` fields are rejected at class definition time — before they can cause a Temporal 2MB payload error in production. Use `Annotated[list[T], MaxItems(N)]` for bounded collections, or `FileReference` to store large data externally. See [ADR-0008](../adr/0008-payload-safe-bounded-types.md).

---

## Infrastructure Layer

All infrastructure is accessed through Protocol-based interfaces, not concrete implementations:

| Protocol | Methods | Production impl | Test impl |
|----------|---------|-----------------|-----------|
| `StateStore` | `save`, `load`, `delete`, `list_keys` | `DaprStateStore` | `InMemoryStateStore` |
| `SecretStore` | `get`, `get_optional`, `get_bulk` | `DaprSecretStore` | `InMemorySecretStore`, `EnvironmentSecretStore` |
| `PubSub` | `publish`, `subscribe` | `DaprBinding` | `InMemoryPubSub` |
| `Binding` | `invoke` | `DaprBinding` | `InMemoryBinding` |
| `CapacityPool` | `acquire`, `release`, `renew` | Redis-backed | `LocalCapacityPool` |

An `InfrastructureContext` (frozen dataclass) holds all of these, stored in a `ContextVar`. Set once at startup via `application_sdk.main`; accessed anywhere via `get_infrastructure()`.

This means **unit tests never need a Dapr sidecar or Temporal server** — swap in the in-memory implementations and run pure Python. See [ADR-0005](../adr/0005-infrastructure-abstraction.md).

---

## Handler and Worker

Every app deployment consists of two components with different lifecycles:

```
Handler Deployment (always-on, min 1 replica)
├── FastAPI on :8000
├── /auth, /preflight, /metadata, /health
└── Handles synchronous HTTP requests from the UI

Worker Deployment (scale 0→N via KEDA)
├── Temporal worker
├── Health endpoint on :8081
└── Executes workflows and activities from the task queue
```

Handlers are always-on because users expect immediate HTTP responses. Workers scale to zero when their task queue is empty — idle apps consume zero resources. See [ADR-0009](../adr/0009-separate-handler-worker-deployments.md) and [ADR-0001](../adr/0001-per-app-handlers.md).

### Handler

Implement `Handler` to provide typed pre-execution operations:

```python
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput,
    MetadataInput, MetadataOutput,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        ...

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        ...

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        ...
```

### Credential System

Credentials are resolved through a typed system — no more `dict["password"]` bugs:

```python
from application_sdk.credentials.ref import basic_ref
from application_sdk.credentials.types import BasicCredential

ref = basic_ref("my-db-creds")
cred: BasicCredential = await self.context.resolve_credential(ref)
# cred.username, cred.password — statically typed
```

Built-in types: `BasicCredential`, `ApiKeyCredential`, `BearerTokenCredential`, `OAuthClientCredential`, `CertificateCredential`, `AtlanApiToken`, `AtlanOAuthClient`, `RawCredential` (legacy fallback).

For Atlan clients specifically, mix in `AtlanClientMixin` and call `get_or_create_async_atlan_client(credential_ref)` — the client is cached per execution and reuses any client already created during `validate()`.

---

## Storage

Object storage bypasses the Dapr sidecar entirely, using the `obstore` library directly:

```python
from application_sdk.storage import upload_file, download_file

await upload_file("artifacts/output.json", local_path="/tmp/output.json")
await download_file("artifacts/output.json", local_path="/tmp/output.json")
```

Higher-level: `App` provides `self.upload()` and `self.download()` framework tasks for directory-level transfer with automatic `FileReference` tracking for cleanup. See [ADR-0005](../adr/0005-infrastructure-abstraction.md).

---

## Observability

Structured logs and OTel traces flow from every worker and handler pod to the cluster's central OTLP collector. Workers configure `OTEL_EXPORTER_OTLP_ENDPOINT` to the node IP (`$(K8S_NODE_IP):4317`) at deploy time; the Helm chart wires this automatically.

`self.logger` is available in both `run()` and `@task` methods. It is automatically bound with `app_name`, `run_id`, and `correlation_id` on every entry. When apps call other apps, the correlation ID propagates automatically, linking distributed traces across services. See [ADR-0003](../adr/0003-per-app-observability.md) and [ADR-0011](../adr/0011-logging-level-guidelines.md).

Errors carry structured codes in `AAF-{COMPONENT}-{ID}` format.

---

## Deployment

Apps are deployed via the `helm/atlan-app/` Helm chart:

```bash
helm install my-connector oci://ghcr.io/atlanhq/charts/atlan-app \
  --set appName=my-connector \
  --set appModule=my_package.app:MyConnector \
  --set image.tag=1.2.3
```

Both deployments use the same container image with a different `--mode` argument:

```bash
# Handler pod
python -m application_sdk.main --mode handler

# Worker pod
python -m application_sdk.main --mode worker

# Local development (both in one process)
python -m application_sdk.main --mode combined
```

Key chart features: KEDA `ScaledObject` (worker scales to zero on empty queue), `imagePullSecret` for GHCR, configurable resource limits, Temporal TLS and auth, Redis-backed capacity pool, Dapr component mounts.

---

## Module Structure

```
application_sdk/
├── app/                    # Core: App ABC, @task, AppRegistry, TaskRegistry
│   ├── base.py             # App class, run() wrapper, determinism helpers
│   ├── task.py             # @task decorator, signature validation
│   ├── registry.py         # AppRegistry, TaskRegistry singletons
│   └── context.py          # AppContext, logging, infra/credential access
│
├── contracts/              # Typed cross-boundary contracts
│   ├── base.py             # Input, Output, HeartbeatDetails base classes
│   ├── types.py            # MaxItems, FileReference, GitReference, SerializableEnum
│   └── events.py           # Lifecycle event models
│
├── handler/                # HTTP handler framework
│   ├── base.py             # Handler ABC
│   ├── contracts.py        # AuthInput/Output, PreflightInput/Output, etc.
│   └── service.py          # create_app_handler_service() FastAPI factory
│
├── execution/              # Temporal abstraction layer (not for direct use)
│   ├── retry.py            # RetryPolicy (framework wrapper)
│   ├── heartbeat.py        # HeartbeatController, run_in_thread
│   ├── sandbox.py          # SandboxConfig with framework defaults
│   └── _temporal/          # Internal Temporal integration (never import directly)
│
├── infrastructure/         # Infrastructure protocols and implementations
│   ├── state.py            # StateStore Protocol + implementations
│   ├── secrets.py          # SecretStore Protocol + implementations
│   ├── pubsub.py           # PubSub Protocol + implementations
│   ├── bindings.py         # Binding Protocol + implementations
│   ├── capacity.py         # CapacityPool Protocol + implementations
│   ├── context.py          # InfrastructureContext, get_infrastructure()
│   ├── _dapr/              # Internal Dapr implementations
│   └── _redis/             # Internal Redis implementations
│
├── credentials/            # Typed credential system
│   ├── ref.py              # CredentialRef and helper constructors
│   ├── types.py            # Credential types (BasicCredential, etc.)
│   ├── resolver.py         # CredentialResolver
│   └── atlan_client.py     # AtlanClientMixin
│
├── storage/                # obstore-backed object storage
│   ├── ops.py              # upload_file, download_file, delete, exists
│   ├── factory.py          # create_local_store, create_memory_store, etc.
│   └── binding.py          # Dapr YAML → obstore config parsing
│
├── templates/              # High-level connector templates
│   ├── sql_metadata_extractor.py
│   ├── sql_query_extractor.py
│   └── incremental_sql_metadata_extractor.py
│
├── testing/                # In-memory mocks and pytest fixtures
│   ├── mocks.py            # MockStateStore, MockSecretStore, etc.
│   └── fixtures.py         # pytest fixtures (clean_app_registry, etc.)
│
└── main.py                 # Unified CLI entry point (--mode worker|handler|combined)
```

Packages prefixed with `_` (e.g., `execution/_temporal/`, `infrastructure/_dapr/`) are private implementation details — never import from them directly. The public interface is everything without the underscore prefix.

---

## Design Decisions

The following Architecture Decision Records document the key choices made in the SDK's design:

| ADR | Decision |
|-----|----------|
| [ADR-0001](../adr/0001-per-app-handlers.md) | Per-app handler deployments (not an uber-handler) |
| [ADR-0002](../adr/0002-per-app-workers.md) | Per-app workers with dedicated task queues (not an uber-worker) |
| [ADR-0003](../adr/0003-per-app-observability.md) | Per-app observability with correlation-based tracing |
| [ADR-0004](../adr/0004-build-time-type-safety.md) | Build-time type safety with Pydantic models |
| [ADR-0005](../adr/0005-infrastructure-abstraction.md) | Complete abstraction of Temporal and Dapr behind Protocols |
| [ADR-0006](../adr/0006-schema-driven-contracts.md) | Single-model contracts with additive evolution rules |
| [ADR-0007](../adr/0007-apps-as-coordination-unit.md) | Apps coordinate via child workflows (not shared activities) |
| [ADR-0008](../adr/0008-payload-safe-bounded-types.md) | Import-time payload safety validation (prevents 2MB Temporal limit failures) |
| [ADR-0009](../adr/0009-separate-handler-worker-deployments.md) | Separate handler and worker Kubernetes deployments |
| [ADR-0010](../adr/0010-async-first-blocking-code.md) | Async-first design; `run_in_thread()` for blocking code |
| [ADR-0011](../adr/0011-logging-level-guidelines.md) | Structured logging level conventions |
