# Apps

The `App` class is the central abstraction in v3. It replaces v2's split between `WorkflowInterface` (orchestration) and `ActivitiesInterface` (work). In v3, you write one class: orchestration lives in `run()`, side-effects get `@task`, and the framework handles everything else.

## Defining an App

```python
from application_sdk.app import App, task
from application_sdk.contracts import Input, Output

class ExtractionInput(Input):
    connection_id: str

class ExtractionOutput(Output):
    rows_extracted: int

class MyConnector(App):
    @task(timeout_seconds=3600, auto_heartbeat_seconds=10)
    async def fetch_data(self, input: ExtractionInput) -> ExtractionOutput:
        # Side-effect: calls an external system
        return ExtractionOutput(rows_extracted=42)

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_data(input)
```

**Key points:**

- `run()` is the orchestration entry point. It defines what tasks execute and in what order.
- `@task` methods contain external side-effects (I/O, API calls, database queries).
- Calling `await self.fetch_data(input)` inside `run()` routes through Temporal activities automatically -- no `execute_activity_method` needed.
- `App.__init_subclass__` applies Temporal decorators under the hood. You never import from `temporalio`.

### Multiple Entry Points

If your connector needs more than one independently-triggerable workflow (e.g. metadata extraction *and* query mining), decorate each entry method with `@entrypoint` instead of overriding `run()`:

```python
from application_sdk.app import App, entrypoint, task

class SnowflakeApp(App):
    @entrypoint
    async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_tables(input)

    @entrypoint
    async def mine_queries(self, input: MiningInput) -> MiningOutput:
        ...
```

Each `@entrypoint` method becomes its own Temporal workflow (`{app-name}:{entry-point-name}`). All entry points share the same `@task` methods, handler, and `AppContext`. Trigger a specific entry point via `POST /workflows/v1/start?entrypoint=<name>`.

`run()` and `@entrypoint` methods can also **coexist** in the same class — useful when migrating an existing `run()`-only app incrementally. `run()` is always the default entry point in that case.

See [Entry Points — Default entrypoint resolution](entry-points.md#default-entrypoint-resolution) for the full resolution rules.

### Dynamic manifest (compute_manifest)

A static `manifest.json` is enough for most apps. A multi-entry-point app that must **compute** its manifest per submission (placeholder fill-in, SQL generation, full DAG rewrite) drops a `core.py` in its per-entry-point package exposing a `compute_manifest` hook:

```python
# app/asset_export_advanced/core.py
async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict:
    # `manifest` is the static manifest (already token-substituted);
    # `fe_inputs` is the decoded frontend form. Return the manifest to serve.
    ...
    return manifest
```

When the app defines it, `GET /workflows/v1/manifest?entrypoint=<name>&fe_inputs=<url-encoded-json>` hands the static manifest plus the decoded `fe_inputs` to the hook and serves its return value; apps without the hook get the static manifest unchanged. The hook must be **`async def`** and return a `dict` — a sync `def` is not discovered and the route serves the static manifest unchanged. If the hook does CPU/IO-bound work (SQL generation, full DAG rewrite) it owns offloading that off the event loop via the SDK's `run_in_thread()` (never the shared default executor — see conformance rule P031). Exceptions are logged internally and surface as a generic `500` (no internals leaked). See [Entry Points — Per-entry-point handler & core modules](entry-points.md#per-entry-point-handler--core-modules) for the module-naming convention.

> **`fe_inputs` size limit.** Because `fe_inputs` rides in the GET query string, it is bounded by the request-line cap of whatever proxy fronts the app (nginx defaults to 8 KB; ALB ~16 KB). The SDK rejects a decoded `fe_inputs` larger than **8 KB** with `413 Payload Too Large` so oversize surfaces as a clear error rather than an opaque upstream truncation. A fully-populated form for the largest connector we ship is ~1.6 KB (≈5 KB for a heavy multi-select), so this is comfortable headroom; forms that genuinely need more should move to a POST body rather than grow the query string.

## Orchestration in run()

The `run()` method is where you compose tasks. It supports sequential, parallel, and conditional patterns:

```python
import asyncio

class MyConnector(App):
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDbInput) -> FetchDbOutput: ...

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemaInput) -> FetchSchemaOutput: ...

    @task(timeout_seconds=3600)
    async def transform_data(self, input: TransformInput) -> TransformOutput: ...

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        # Sequential
        db_out = await self.fetch_databases(
            FetchDbInput(connection_id=input.connection_id)
        )

        # Parallel (asyncio.gather works inside run)
        schema_out, transform_out = await asyncio.gather(
            self.fetch_schemas(FetchSchemaInput(databases=db_out.databases)),
            self.transform_data(TransformInput(data=db_out.data)),
        )

        return ExtractionOutput(
            rows_extracted=schema_out.count + transform_out.count
        )
```

## TaskRegistry Auto-Discovery

You never register tasks or apps manually. When Python imports your `App` subclass:

1. `App.__init_subclass__` scans the class for `@task` methods.
2. Each task is registered in the global `TaskRegistry`.
3. The `App` subclass itself is registered in `AppRegistry`.
4. At startup, `create_worker()` reads both registries and configures the Temporal worker.

## Templates

The SDK provides pre-built `App` subclasses for common patterns. Override only the tasks you need to customize.

### SqlMetadataExtractor

Extracts metadata (databases, schemas, tables, columns, procedures) from SQL sources:

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts import (
    FetchDatabasesInput, FetchDatabasesOutput,
    ExtractionInput, ExtractionOutput,
)
from application_sdk.app import task

class MyExtractor(SqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_databases(
        self, input: FetchDatabasesInput
    ) -> FetchDatabasesOutput:
        # Custom database fetching logic
        return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
```

### SqlQueryExtractor

Extracts query history/logs from SQL sources:

```python
from application_sdk.templates import SqlQueryExtractor
from application_sdk.templates.contracts import (
    QueryBatchInput, QueryBatchOutput,
    QueryFetchInput, QueryFetchOutput,
)
from application_sdk.app import task

class MyQueryExtractor(SqlQueryExtractor):
    @task(timeout_seconds=600)
    async def get_query_batches(
        self, input: QueryBatchInput
    ) -> QueryBatchOutput: ...

    @task(timeout_seconds=3600)
    async def fetch_queries(
        self, input: QueryFetchInput
    ) -> QueryFetchOutput: ...
```

### IncrementalSqlMetadataExtractor

Runs a 5-phase incremental extraction:

1. **Prerequisites** — fetch the prior run marker and the previous state snapshot.
2. **Base extraction** — extract databases, schemas (in parallel), then tables.
3. **Incremental columns** — batch preparation followed by parallel column discovery.
4. **Write state** — create and upload the new current-state snapshot.
5. **Update marker** — persist the new run marker after the state write succeeds.

```python
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts import (
    FetchColumnsIncrementalInput, FetchColumnsOutput,
)
from application_sdk.app import task

class MyIncrementalExtractor(IncrementalSqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_columns(
        self, input: FetchColumnsIncrementalInput
    ) -> FetchColumnsOutput: ...
```

### BaseMetadataExtractor

Base class for all metadata-extraction Apps. Provides upload, cleanup, and lifecycle plumbing without committing to a SQL-specific task layout. `SqlMetadataExtractor` extends this with SQL-specific defaults and task structure.

## Lifecycle Hooks

### on_complete

Called after `run()` finishes, whether it succeeded or raised an exception:

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput: ...

    async def on_complete(self) -> None:
        await self.notify_downstream()
        await super().on_complete()  # preserves built-in file/storage cleanup
```

### Built-in Cleanup Tasks

Two cleanup tasks and two transfer tasks are available on every `App`:

- `upload(UploadInput(...))` — pushes a local file or directory to object storage. Routes to the Atlan-owned `atlan-objectstore` (`infra.upstream_storage`) in SDR deployments; falls back to the customer-owned `objectstore` (`infra.storage`) in local dev. This is the explicit hand-off step that downstream Atlan system apps (publish, lineage, quality) consume. See [file-reference.md](file-reference.md) and [ADR-0014](../adr/0014-two-store-storage-architecture.md).
- `download(DownloadInput(...))` — pulls a file or directory from object storage to a local path.
- `cleanup_files()` — removes tracked `FileReference` local paths from task outputs, **then** convention-based temp directories (using `input.extra_paths` if provided, otherwise `ATLAN_CLEANUP_BASE_PATHS`, otherwise the default temp path).
- `cleanup_storage()` — removes object store artifacts by tier:
  - `StorageTier.TRANSIENT` refs are always removed.
  - `StorageTier.PERSISTENT` refs are always left untouched.
  - `StorageTier.RETAINED` refs under the run-scoped prefix are removed **only** when `input.include_prefix_cleanup=True` is set (opt-in); otherwise they are left untouched.

Both are called automatically by the default `on_complete()` implementation. Do not call them directly from `run()` — the cleanup contract is tied to workflow completion, not mid-run state.

### SDR: Object-Store Access Preflight

When an app runs in **Self-Deployed Runtime (SDR) mode** (`ENABLE_ATLAN_UPLOAD=true`), the SDK
verifies read + write access to every configured object store at boot time, before the Temporal
worker accepts any connections. This catches misconfigurations that would otherwise cause every
workflow run to fail deep inside the task graph.

**When it runs:** `verify_object_store_access` is called once inside `_create_infrastructure`
immediately after the stores are constructed. It is a no-op in all other run modes.

**What it checks:**

| Store | Binding name | Required |
|---|---|---|
| Deployment store | `objectstore` | Always |
| Upstream Atlan store | `atlan-objectstore` | Always in SDR — hard-fail if absent |

For each store a round-trip probe is executed: write a sentinel object → `HEAD` the object →
delete it. Delete is best-effort — a delete failure is logged at WARNING but does not fail the
probe. A missing upload permission, wrong credentials, or unreachable endpoint surfaces here
rather than mid-run. Each probe is bounded by `ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS` (default: 30 s);
a blackholed endpoint times out instead of stalling the boot indefinitely.

**Failure mode:** any probe failure raises `ObjectStorePreflightError`, which propagates out of
`_create_infrastructure` and is caught by `main()` before the process exits non-zero. The error
message lists each failing store with a classified cause and a one-line remediation hint:

```
Object-store access check failed (1 store(s) with errors):
  * deployment store (binding: 'objectstore'): write failed [permission denied]
    Cause: 403 Forbidden ...
    Hint:  The credentials are valid but lack the required read/write/delete
           permissions on this bucket. Grant the IAM/ACL permissions needed
           for get, put, and delete operations.
```

**Error classification:**

| Classifier | Triggering signals | Meaning |
|---|---|---|
| `permission denied` | HTTP 403, `AccessDenied`, `Forbidden`, `not authorized` | Valid credentials, missing IAM/ACL permissions |
| `invalid credentials` | HTTP 401, `InvalidAccessKeyId`, `SignatureDoesNotMatch`, `unauthenticated` | Wrong/expired access key or secret |
| `connectivity / unknown` | Timeout, network error, bucket not found | Endpoint URL, bucket name, or network unreachable from this pod/host |

**Timeout override:**

```bash
ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS=60  # increase for slow networks
```

**Programmatic access:**

```python
from application_sdk.storage import verify_object_store_access, ObjectStorePreflightError
```

Both symbols are exported from `application_sdk.storage`. The function is normally called by
the SDK boot path — connectors do not need to call it manually.

### Preflight Gate Posture

Distinct from the SDR object-store preflight above, a connector can run a `preflight_check`
handler as the first activity of every extraction workflow. Enforcement is a **gate** property,
not a handler property: the handler always returns the honest verdict, and the gate decides what
to do with a `NOT_READY` verdict. The posture is set per app via the `preflight_gate_mode`
`ClassVar`:

- **soft** (default): never blocks — a `NOT_READY` verdict lets the run proceed and is emitted as
  `outcome="would_block"` (with `gate_mode="soft"` and the per-check `check_matrix`) on the gate
  outcome event. The verdict is always reported, so connector-pulse can rank apps by how often they
  *would* have blocked real runs — that list is the "checks are ready to enforce" queue.
- **hard**: blocks the run when the verdict is `NOT_READY` (raises `PreflightFailed`). This is the
  opt-in for apps whose checks are trusted to gate real runs.

```python
class MyConnector(App):
    preflight_gate_mode = "hard"   # checks are trusted to block runs
```

Ops can override the posture without an app release via `ATLAN_PREFLIGHT_GATE_MODE=hard` on the
worker deployment. The env var wins over the attribute; any set value other than the literal `hard`
resolves to soft, so malformed config never blocks a run by accident. An empty or unset value is
not an override — resolution falls through to the declared `preflight_gate_mode` attribute. The
worker logs an INFO line
per hard app at boot. Start soft, then flip to `hard` once connector-pulse `would_block` rows show
the checks track real workflow failures. See the `adopt-preflight-gate` skill for the full adoption
flow.

### Asset-Validation Outcome

`App.upload()` runs a **warn-only** validation of transformed asset NDJSON against the pyatlan_v9
`.validate()` backbone (plus a referential/orphan pass) before the SDR→Atlan handoff. It never blocks
and never fails the upload — invalid or orphaned assets are reported, not rejected.

The results are surfaced as a structured outcome event (the sibling of the preflight gate's outcome
event above) so they are queryable in ClickHouse, not just greppable in log bodies. Because the event
is emitted from inside the `upload` activity, the Temporal context (`workflow_run_id`, `app_name`) is
auto-stamped and each row joins to the workflow outcome by run id:

- It fires on **every validated upload** — `outcome="clean"` as well as `outcome="flagged"` — so
  there is a denominator to rank flag-rate against (mirrors the gate's `would_block` reporting).
- Five scalar counts land as their own `LogAttributes`: `assets_total`, `assets_passed`,
  `assets_invalid`, `assets_orphaned`, `assets_undeserializable`.
- Per-failure detail rides in one compact JSON attribute, `asset_validation_matrix` (bounded to a
  fixed number of rows per axis so it can't grow unbounded); the full human-readable report is also
  logged as a WARNING body, but only for flagged runs.

Uploads with nothing to validate emit nothing at all: when `ATLAN_VALIDATE_ASSETS_ON_UPLOAD=false`
or when the path is not a `transformed/` subtree (e.g. a raw upload), no outcome event is produced.
See [Monitoring](monitoring.md#asset-validation-outcome-event) for the attribute list as it reaches
OTLP.

> **Temporarily disabled by default (CNCT-85).** The check is currently off by default while the
> process-isolation fix ([#2769](https://github.com/atlanhq/application-sdk/pull/2769)) awaits
> downstream changes. Set `ATLAN_VALIDATE_ASSETS_ON_UPLOAD=true` to opt back in.

## Passthrough Modules

If your app imports third-party libraries that must be available inside the Temporal sandbox, declare them as a class-level attribute:

```python
class MyConnector(App):
    passthrough_modules = {"my_connector", "third_party_lib"}
    ...
```

The type is `ClassVar[set[str] | None]` — use a set literal, not a list. In v2, passthrough modules were passed to the `Worker` constructor. In v3, they live on the `App` subclass as a `ClassVar`. Do **not** pass `passthrough_modules` as a class-kwarg — it is not accepted by `App.__init_subclass__`.

## Customizing SQL Queries

For SQL template apps, override SQL query class attributes or load from files:

```python
from application_sdk.common.sql_filters import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/queries")

class MyExtractor(SqlMetadataExtractor):
    fetch_database_sql = SQL_QUERIES.get("FETCH_DATABASES")
    fetch_table_sql = SQL_QUERIES.get("FETCH_TABLES")
```

## Testing Apps

Test `@task` methods directly without Temporal or Dapr:

```python
import pytest
from application_sdk.testing import MockSecretStore, MockStateStore
from application_sdk.infrastructure import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.testing import clean_app_registry  # noqa: F401

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"api-key": "test-secret"}),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    yield ctx
    clear_infrastructure()

async def test_fetch(infra):
    connector = MyConnector()
    output = await connector.fetch_data(
        ExtractionInput(connection_id="test")
    )
    assert output.rows_extracted > 0
```

Use the `clean_app_registry` fixture to prevent `App` subclass registrations from leaking between tests:

```python
# conftest.py
from application_sdk.testing import clean_app_registry  # noqa: F401
```

For testing credential resolution, use `MockCredentialStore`:

```python
from application_sdk.testing import MockCredentialStore

store = MockCredentialStore()
ref = store.add_api_key("my-service", api_key="secret123")
# Or: store.add_basic("db", username="user", password="pass")
# Or: store.add_bearer_token("svc", token="tok")

ctx = InfrastructureContext(secret_store=store.secret_store)
set_infrastructure(ctx)
```

For testing tasks that emit heartbeats, use `MockHeartbeatController`:

```python
from application_sdk.testing import MockHeartbeatController

controller = MockHeartbeatController()
# Pass to AppContext or inject via fixture; inspect calls after the task runs:
calls = controller.get_heartbeat_calls()
```

---

## App State

`app_state` is in-memory state scoped to the current workflow execution. Use it to pass values between tasks without encoding them in task contracts.

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        await self.fetch_databases(FetchDbInput(connection_id=input.connection_id))
        return await self.transform_data(TransformInput(...))

    @task
    async def fetch_databases(self, input: FetchDbInput) -> FetchDbOutput:
        out = await self._do_fetch(input)
        # Store inside a @task — app_state requires an active activity context:
        self.app_state.set("db_list", out.databases)
        return out

    @task
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        dbs = self.app_state.get("db_list")
        ...
```

---

## Continuing with New Input

`continue_with()` restarts the current App with new input while preserving correlation context. It truncates the Temporal workflow history and starts a new run — useful for long-running Apps that accumulate too much history.

```python
class IncrementalExtractor(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        out = await self.fetch_batch(FetchInput(cursor=input.cursor))
        if out.has_more:
            # Restart with the next cursor — never accumulates unbounded history
            self.continue_with(ExtractionInput(cursor=out.next_cursor))
        return ExtractionOutput(total=out.count)
```

`continue_with()` does not return — it raises a framework signal internally.

---

## Worker Pools

By default every task runs on the app's primary Temporal task queue. Use `pool=` on `@task` to route a task to a dedicated worker pool — useful for activities that need different resource profiles (CPU-heavy crawls, memory-intensive exports, etc.).

```python
from application_sdk.app import App, task

class MyConnector(App):

    @task(pool="heavy")
    async def bulk_export(self, input: ExportInput) -> ExportOutput: ...

    @task  # runs on the default queue
    async def fetch_schema(self, input: SchemaInput) -> SchemaOutput: ...
```

**Pool name rules:** pool names must be lowercase kebab-case (e.g. `"heavy"`, `"cold-tier"`). The `@task` decorator enforces this at decoration time.

**Queue resolution** (evaluated at workflow-run time):

1. `ATLAN_POOL_<POOL>_QUEUE` — explicit override. Hyphens in the pool name are normalised to underscores: `pool="cold-tier"` looks up `ATLAN_POOL_COLD_TIER_QUEUE`.
2. `${ATLAN_TASK_QUEUE}-<pool>` — derived from the app's base queue when the explicit env var is absent.
3. If neither is set, a warning is emitted at startup and the activity falls back to the default queue.

**Pkl contract:** every pool used in `@task` must be declared in the app contract so the contract-toolkit can generate the correct deployment manifest:

```pkl
pools {
  ["heavy"] = new Pool {
    keda { minReplicaCount = 2 }
  }
  ["cold-tier"] = new Pool {
    keda { minReplicaCount = 0; cooldownPeriod = 600 }
  }
}
```

See [ADR-0016](../adr/0016-multi-pool-worker-routing.md) for the full design including rollout-drain requirements.

---

## Retry Policies

Pass a `RetryPolicy` to `@task` via `retry_policy` to override the default (3 attempts, exponential backoff: initial 1s, coefficient 2.0, capped at 5 minutes):

```python
from application_sdk.app import App, RetryPolicy, task

class MyConnector(App):
    @task(retry_policy=RetryPolicy(max_attempts=1))
    async def send_webhook(self, input: WebhookInput) -> WebhookOutput: ...

    @task(retry_policy=RetryPolicy(max_attempts=10, backoff_coefficient=1.5))
    async def fetch_flaky_api(self, input: FetchInput) -> FetchOutput: ...
```

`RetryPolicy` is a frozen dataclass with fluent builder methods:

```python
policy = RetryPolicy().with_max_attempts(5).with_non_retryable(ValueError)
```

---

## Catching Client-Side Workflow Failures

Code that calls `TemporalClient.execute_workflow(...)` or waits on a workflow handle
(test harnesses, admin tooling, anything outside `run()`/`@task`) can catch the
Temporal failure/cause exception family directly from `application_sdk.execution` —
no need to import `temporalio` yourself:

```python
from application_sdk.execution import (
    TemporalActivityError,
    TemporalCancelledError,
    TemporalWorkflowFailureError,
)

try:
    await temporal_client.execute_workflow(MyConnector.run, input, id=run_id, task_queue=queue)
except TemporalWorkflowFailureError as e:
    match e.cause:
        case TemporalActivityError():
            ...  # an @task raised
        case TemporalCancelledError():
            ...  # the workflow was cancelled
        case _:
            ...  # e.g. temporalio.exceptions.ApplicationError when run()/@entrypoint
            # itself raised directly — still log/handle it, don't ignore silently
```

`TemporalWorkflowFailureError` wraps the terminal-state cause on `.cause` — commonly
one of `TemporalActivityError`, `TemporalCancelledError`, `TemporalChildWorkflowError`,
`TemporalTerminatedError`, or `TemporalTimeoutError`, but not limited to those (e.g. a
direct raise from `run()`/`@entrypoint` surfaces as the unexported
`temporalio.exceptions.ApplicationError` — always include a catch-all case). These
five are re-exported (not wrapped) with a `Temporal` prefix so they don't collide with
unrelated SDK types of the same short name, e.g.
`application_sdk.common.error_codes.ActivityError` and
`application_sdk.errors.leaves.CancelledError`.

This is distinct from error handling *inside* `run()`/`@task` code: there, raise
and catch `application_sdk.errors.AppError` leaves (`CancelledError`,
`AppTimeoutError`, etc.) instead — those carry the SDK's classified failure
metadata (category, code, retryable). The `Temporal*Error` types above are raw
client-side signals for code observing a workflow from the outside.

---

## Atlan Client Mixin

Mix in `AtlanClientMixin` when your App needs to call the Atlan API. It provides `get_or_create_async_atlan_client()`, which caches the `AsyncAtlanClient` per execution.

```python
from application_sdk.credentials import AtlanClientMixin

class MyConnector(AtlanClientMixin, App):
    @task
    async def update_lineage(self, input: LineageInput) -> LineageOutput:
        client = await self.get_or_create_async_atlan_client(input.credential)
        await client.asset.upsert(...)
        return LineageOutput(updated=True)
```

Import path: `application_sdk.credentials.AtlanClientMixin`.
