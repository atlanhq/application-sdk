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

Every entry point's contracts are a **public boundary**: a `FileReference` on one must declare its shape. See [Declaring artifact schemas](#declaring-artifact-schemas).

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

When the app defines it, a manifest request hands the static manifest plus the decoded `fe_inputs` to the hook and serves its return value; apps without the hook get the static manifest unchanged. Two transports carry `fe_inputs`, and the hook cannot tell them apart:

```
POST /workflows/v1/manifest          {"entrypoint": "<name>", "fe_inputs": {...}}   # preferred
GET  /workflows/v1/manifest?entrypoint=<name>&fe_inputs=<url-encoded-json>          # size-capped
```

The hook must be **`async def`** and return a `dict` — a sync `def` is not discovered and the route serves the static manifest unchanged. If the hook does CPU/IO-bound work (SQL generation, full DAG rewrite) it owns offloading that off the event loop via the SDK's `run_in_thread()` (never the shared default executor — see conformance rule P031). Exceptions are logged internally and surface as a generic `500` (no internals leaked). See [Entry Points — Per-entry-point handler & core modules](entry-points.md#per-entry-point-handler--core-modules) for the module-naming convention.

> **Use `POST` when you send `fe_inputs`.** On `GET`, `fe_inputs` rides in the query string and is bounded by the request line. The SDK rejects a decoded `fe_inputs` larger than **8 KB** with `413 Payload Too Large`, *before* the hook runs, so oversize surfaces as a clear error instead of an opaque truncation. That cap is not conservative padding: past **~64 KB of request line** the URL is silently mangled by the HTTP parser — the app receives a fragment and answers `200` on a corrupt value — which is strictly worse than a rejection. A near-"select-all" asset-export-advanced submission decodes to ~11 KB and hit exactly this (CSA-539). `POST` puts the payload in the body, where neither limit applies. `GET` remains supported and uncapped for requests that send no `fe_inputs` at all — static manifest fetches, the app playground, and AE/marketplace registration probes.

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

## Offloading Blocking Work (`run_in_thread` and auto-holds)

Connector code runs on Temporal's asyncio event loop, so a blocking call (a sync database driver, a filesystem read, a vendor SDK without async support) must not run on the loop itself. Offload it with `run_in_thread` — reachable as `self.run_in_thread(...)`, `self.task_context.run_in_thread(...)`, or the module-level `application_sdk.execution.heartbeat.run_in_thread`:

```python
rows = await self.run_in_thread(cursor.execute, sql)
```

Every offload is automatically wrapped in a **progress hold** (ADR-0018), so the stall watchdog never reads a legitimately long blocking call as a stall and false-kills it. There is nothing to do at the call site — a long blocking call behaves exactly as it did before the watchdog existed:

- `run_in_thread` holds are **unbounded**. The SDK does not invent a duration for somebody else's blocking call, so the watchdog is inactive for the call's whole duration and the activity's duration bound is the only thing still holding it.
- `run_fault_isolated` holds are **bounded by that call's own `timeout`** (the wall-clock kill it already enforces), and unbounded when `timeout=None`, matching its "wait forever" semantics.

To give a blocking call a real bound instead of the unbounded default, wrap the offload in `holding_progress(label, timeout=...)` — declaring how long you would let this one call run before you would rather it failed. A wedged call is then caught at `timeout` plus the no-progress budget rather than at the duration backstop:

```python
async with self.holding_progress("full table scan", timeout=7200):
    rows = await self.run_in_thread(cursor.execute, sql)
```

`timeout` is **not** a prediction of how long the call takes. Err generous: too generous only delays detection toward the backstop, while too tight kills a healthy run — and because stall kills retry, a too-tight allowance burns the same wasted work up to three times.

**Inside a `holding_progress` block the automatic holds stand down**, so the allowance you declared is what governs. The example above lapses at 7200s rather than inheriting an unbounded auto-hold that would outlive it — the SDK never adds a vouch that outlives the one you asked for. (An offload running concurrently in a task that never entered the block is still auto-held.)

For opaque *async* calls (the connector's own async client) there is no SDK-owned seam to auto-hold, so wrap those in `holding_progress` directly. Expect to need it: interleaved streaming reads (fetch a page, write a batch, repeat) are already covered by the SDK's own writer and transfer loops, but almost every connector makes at least one genuinely opaque single call — one large metadata query, one slow list/export that returns everything at once.

See [Progress and Stalls](progress-and-stalls.md) for the whole picture — what counts as
progress, how to size the allowance from your own data, and how to read the report the
watchdog produces for your app — and [ADR-0018](../adr/0018-progress-aware-heartbeat.md)
for the design.

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

**`upload()` does not require the files on the calling pod.** When `local_path`
is absent — a cross-pod hand-off where the tasks that produced the tree ran on
other workers, or a caller that only has a `FileReference` — pass
`UploadInput(ref=...)` and the upload streams from the deployment store at
`ref.storage_path` instead of the local filesystem. Pass `storage_path` too to
pin the destination and the copy is key-preserving; leave it off and the
artifacts land under the canonical run prefix for the tier. A key-preserving
copy whose source and destination are the same object is recognised as already
satisfied (`reason="skipped:same_object"`) rather than moving bytes — but a
`ref` pointing at a key that was never written still fails loudly with
`StorageNotFoundError`. A partially-present local directory gets both — the
local files plus anything present only in the store — but only when the
destination is a *different* store (an SDR hand-off to the upstream store);
within a single store the local tree stays authoritative. See
[file-reference.md](file-reference.md) and
[ADR-0014](../adr/0014-two-store-storage-architecture.md).

Both transfer tasks validate the bytes they move. An upload confirms the local file did not shrink while it was read and that the store recorded what was sent, and records a `{key}.sha256` sidecar; a download confirms it wrote as many bytes as the store declared and, when a sidecar exists, that the content hashes to it. An artifact whose producer died mid-write therefore fails at the transfer boundary with a non-retryable `StorageIntegrityError` naming the file and both digests, rather than reaching a parser as an unattributable `Malformed JSON`. The checks live in the transfer primitives, so every path — these tasks, `FileReference` persist/materialize, prefix transfers, the writer chunk uploads — is covered by the same code. See [storage.md](storage.md#transfer-integrity).

**Writer output is staged, then published at `close()`.** A `Writer` (parquet, JSON) never writes into its output directory directly — it writes into a private staging tree (a sibling directory, not inside the output) and publishes into the output directory in one step when `close()` returns. Files produced by that writer are therefore absent from the output directory until `close()` completes; a writer that is cancelled or fails before `close()` publishes nothing. This is what stops a cancelled attempt's orphaned writer from colliding with, or being adopted into, a retry's output. The published filenames and object-store keys are unchanged — only the timing of when files appear in the output directory moves to `close()`. Note that publishing only *adds*: content already sitting in a reused output directory is left in place, and a deferred writer's `FileReference` walks the whole directory, so it adopts that content too.

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

Interactive SDR preflight (`sdr:preflight_check`) maps those buckets onto existing taxonomy leaves so routing and retry match the cause, not a single retryable outage:

| Role + bucket | Leaf | Audience | Retryable |
|---|---|---|---|
| Either role, `permission denied` | `AppPermissionDeniedError` | USER | no |
| Either role, `invalid credentials` | `AuthError` | USER | no |
| Deployment store, `connectivity / unknown` | `SourceUnavailableError` | USER | yes |
| Upstream Atlan upload proxy, `connectivity / unknown` | `DependencyUnavailableError` | PLATFORM | yes |

Customer-facing `suggested_action` is kept on every leaf. When a READY handler is downgraded because an object-store probe failed, the aggregate `PreflightOutput.error` / `message` banner is pinned to that object-store row — not the first failed check overall, which can be a non-fatal secret-store row.

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

### SDR: Binding Secret Resolution (`auth.secretStore` + `secretKeyRef`)

Dapr component YAMLs for the deployment and upstream object stores can carry credentials
either inline (``value:``) or via a ``secretKeyRef:`` that names a secret in the component's
``auth.secretStore``.  On the secure k8s SDR path the chart deliberately omits the matching
environment variables and resolves credentials only through the secret store.

At startup ``_create_infrastructure`` (in ``application_sdk.main``) reads each store's
``secretKeyRef`` entries via ``read_binding_secret_refs``, fetches the referenced secrets from
the Dapr sidecar (``_fetch_binding_secrets``), and hands them to the synchronous store
factories (``create_store_from_binding*``) as the ``secrets=`` keyword.  The same secrets are
also published to a process-wide registry (``set_fetched_binding_secrets``) so sync consumers
constructed later — ``DaprCredentialVault.__init__`` is the canonical case — resolve the
binding against the same values instead of falling back to env-only resolution and reaching
a different verdict.

Resolution order per field: the fetched secret map wins; environment variables are the
fallback (Docker Compose / ``secretstores.local.env`` shape); a ``secretKeyRef`` that neither
source holds marks the binding broken (``StorageBindingBrokenError``).

The ``secrets=`` parameter is keyword-only and optional, so existing call sites and the
public sync API are unchanged.  Secret values are never logged — the startup resolution log
emits ``endpoint_configured=<bool>`` rather than the resolved endpoint.

### SDR: Interactive Activity Timeouts

The three interactive SDR operations (`sdr:test_auth`, `sdr:preflight_check`, `sdr:fetch_metadata`)
run as Temporal activities. Each is bounded by a `schedule_to_close` cap (ticks even when no worker
is polling, so a request to an offline worker fails instead of hanging) and a `start_to_close` cap
(bounds in-flight execution once a worker picks it up). The defaults are deliberately generous —
SDR handlers resolve credentials from a customer-side secret store and probe the customer's own
network — and each cap is env-tunable so a deployment fronting an especially slow store can raise it
without an SDK release. All follow the `ATLAN_` prefix convention (ADR-0009), matching
`ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS`.

| Env var | Default (s) | Who sets it | Bounds |
|---|---|---|---|
| `ATLAN_SDR_AUTH_SCHEDULE_TO_CLOSE_SECONDS` | 60 | Deployment / operator | `test_auth` total wall-clock |
| `ATLAN_SDR_AUTH_START_TO_CLOSE_SECONDS` | 55 | Deployment / operator | `test_auth` in-flight run |
| `ATLAN_SDR_PREFLIGHT_SCHEDULE_TO_CLOSE_SECONDS` | 120 | Deployment / operator | `preflight_check` total wall-clock |
| `ATLAN_SDR_PREFLIGHT_START_TO_CLOSE_SECONDS` | 110 | Deployment / operator | `preflight_check` in-flight run |
| `ATLAN_SDR_METADATA_SCHEDULE_TO_CLOSE_SECONDS` | 150 | Deployment / operator | `fetch_metadata` total wall-clock |
| `ATLAN_SDR_METADATA_START_TO_CLOSE_SECONDS` | 140 | Deployment / operator | `fetch_metadata` in-flight run |

Invariant: `start_to_close < schedule_to_close` in each pair, so at least one retry attempt fits
inside the schedule cap. An inverted override is logged as a WARNING at worker start so the misconfig
is visible before the worker accepts work. These activities set **no** `heartbeat_timeout` — they run
a single handler call and never call `activity.heartbeat()`, so a heartbeat cap would hard-limit
runtime; `start_to_close` is the correct in-flight bound.

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

#### What hard mode covers

Hard mode applies to every outcome the gate can attribute to the **source**, not only a
`NOT_READY` verdict. Failures of the gate's own **plumbing** always fail open, in both postures —
a platform blip must not fail a healthy run. The gate stamps which of the two happened as
`gate_classification` on the outcome event, so the two are separable downstream:

| Gate outcome | `gate_classification` | soft | hard |
| -- | -- | -- | -- |
| Verdict `READY` / `PARTIAL` | — | proceed | proceed |
| Verdict `NOT_READY` | — | report `would_block` | **block** |
| Probe overran the budget | `source_unverifiable` | report `would_block` | **block** |
| Handler crashed | `source_unverifiable` | report `would_block` | **block** |
| Credential provably absent | `source_unverifiable` | report `would_block` | **block** |
| Credential lookup failed for another reason | `gate_broken` | fail open | fail open |
| Rate limited (429) | `gate_broken` | fail open | fail open |
| Secret-store / dependency outage | `gate_broken` | fail open | fail open |
| Worker unavailable | `gate_broken` | fail open | fail open |
| Gate skipped (replay, source-less app) | — | `skipped` | `skipped` |

A handler signals "I could not determine readiness" — as opposed to "the source is not ready" — by
raising a typed error whose category is plumbing-side (`RateLimitedError`,
`DependencyUnavailableError`, `ResourceExhaustedError`). Returning `NOT_READY` for a transient
makes hard mode fail *closed* on a blip, which is the mirror-image bug.

Two queryable events come out of the gate. The per-run **outcome** event carries `outcome`,
`gate_mode`, `gate_classification` and the per-check `check_matrix`. On a `gate_broken` fail-open
its `reason` names the *underlying* fault — the SDK unwraps Temporal's `ActivityError`/`ApplicationError`
to the real error type (e.g. `DaprSidecarUnreachableError`), not the wrapper — so a persistent
platform fault is separable from a transient blip on the dashboard. A deadline overrun carries no
error type to unwrap, so it reports which deadline fired instead: `Timeout:START_TO_CLOSE` (one
attempt outran its own budget — what a dependency wait wider than the gate's `start_to_close` looks
like), `Timeout:SCHEDULE_TO_CLOSE` (the retry window closed), or `Timeout:HEARTBEAT`. A boot-time **posture** event
(`Preflight gate posture`) is emitted once per gate-registered app — soft ones included — carrying
`app_name`, `gate_mode` and `gate_timeout_seconds`. The posture event is the denominator the outcome
events cannot supply: an app that never reaches a verdict emits no outcome row at all, so "which
apps believe they are gated" is only answerable from posture rows.

**Upgrading an app that is already on hard mode:** the three `source_unverifiable` rows above
previously fell through to fail-open, so hard mode enforced only the `NOT_READY` verdict. They now
block. Before taking this SDK version, confirm the handler finishes inside
`preflight_gate_timeout_seconds` — an app whose preflight has been quietly overrunning the budget
was proceeding on every run and will now abort on every run. The worker logs the budget alongside
the hard-mode line at boot.

#### Sizing the check budget

The handler gets `preflight_gate_timeout_seconds` (default 150, clamped 5-300) to run all its
checks, and the SDK **enforces** it — the gate cancels `preflight_check` when it elapses.
`preflight_gate_max_attempts` (default 2, clamped 1-3) sets the retries, and both Temporal
timeouts derive from the pair:

```python
class MyConnector(App):
    preflight_gate_mode = "hard"
    preflight_gate_timeout_seconds = 250   # this source's catalog probe is genuinely slow
    preflight_gate_max_attempts = 1        # a retry cannot rescue a slow check
```

The budget bounds the **whole handler call**, not each check, and it is a **deadline, not a
reservation** — a handler returning in 3s holds its worker slot for 3s whatever the budget says.
A generous budget therefore costs nothing on a healthy run; it only changes the run that would
otherwise have been cut short.

`PreflightInput.timeout_seconds` carries what is *left* after credential resolution, so a handler
sizing probes to that field is sizing to the real deadline. Three rules follow:

- **In hard mode an overrun blocks the run**, so the declared budget and the handler's actual cost
  must agree. Raise the budget for a demonstrably slow source rather than letting checks overrun.
- **Size from the p99 of successful runs**, read off the SDK-measured `gate_duration_ms` on the
  outcome event. Sizing to the worst observed run makes the timeout decorative; sizing to p95
  blocks 5% of runs. Per-check `duration_ms` inside `check_matrix` is handler-authored and is not
  a substitute — and a handler that never sets it publishes `-1.0`, the "not measured" sentinel,
  never a plausible elapsed time.
- **Pair a large budget with one attempt.** A retry rescues a transient by trying *again*, not by
  trying *longer*; at the 300s ceiling two attempts reserve a ~10 minute `schedule_to_close`.
- **Keep probes awaitable.** Cancellation lands at an `await`; blocking synchronous I/O on the
  event loop cannot be interrupted, so it escapes the budget and also stalls the worker's other
  activities. Run blocking drivers in a thread.

Note the ops override below now carries more weight than it used to: setting it to `hard`
fleet-wide makes every app block on a handler crash or an absent credential, including apps that
never opted in and whose checks have not been validated against real runs. Prefer the per-app
attribute.

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

> **One check, reached through the wrapper.** This check *is* the generic artifact wrapper's
> NDJSON × `ModelSource` cell ([ADR-0020](../adr/0020-artifact-validation.md)) — the hook calls
> `validate_artifact(target, ModelSource(model=Asset))`, and the model the declaration delegates to is
> pyatlan_v9's `Asset`. Nothing about the check, its process isolation or its event changed in that
> move: the event name and every attribute key above are a shipped contract that dashboards and alert
> rules match verbatim. One hand-off emits one row, so this hook does **not** also emit the generic
> `"Artifact validation outcome"`.

> **On by default (CNCT-85).** The scan runs in an isolated child process
> (process-isolation fix [#2769](https://github.com/atlanhq/application-sdk/pull/2769)), so a native
> fault in the decode path is contained and downgraded to a best-effort skip rather than killing the
> worker. Set `ATLAN_VALIDATE_ASSETS_ON_UPLOAD=false` to disable per-deployment.

### Declaring artifact schemas

Data crosses app boundaries as **files**, and at every hand-off the producer's idea of the
artifact's shape and the consumer's idea of it are independent beliefs that nothing checks. A
production RCA traced 73 days of frozen lineage to one column that had become a string where the
consumer expected a timestamp — every workflow in the chain reported success throughout. Checksums
do not help: storage integrity attests that the bytes read are the bytes written, and is explicit
that this proves nothing about the artifact being semantically what the reader expects.

`artifactSchemas` in your pkl contract is where that shape gets written down.

**Required on an entry point's boundary. Optional on a `@task`.**

| Surface | Declaration | Why |
|---|---|---|
| An entry point's `input` / return contract | **Required** | Public by definition — another app or the platform DAG reads it |
| An internal `@task` contract | Optional | App-internal processing; the app decides whether it wants the check |

There is no special case for `run()`. The default `run()` method is registered as an *implicit*
entry point carrying the same metadata as an explicit `@entrypoint`, so "every entry point's
contracts" already means "every public boundary". `@task` contracts never become entry points, so
they are exempt by construction — not by a list that could drift.

Declare it keyed by the **contract field name**, never by a storage path (a path-shaped key fails
generation, by design — path-shape inference is what let an earlier upload-time hook match nothing
and silently validate zero records):

```pkl
artifactSchemas {
  ["raw_queries"] = new ArtifactSchema {
    format = "parquet"   // or "ndjson" — the content format, not the file suffix
    fields {
      new ArtifactField {
        name = "QUERY_ID"
        type = "string"
        description = "Warehouse-assigned query id; the parser's join key."
      }
      new ArtifactField {
        name = "START_TIME"
        type = "timestamp"
        description = "When the query began executing. A stringified timestamp here is the defect this declaration exists to catch."
      }
    }
  }
}
```

`description` is required on every field and is never asserted at runtime — it is read by whoever is
debugging the hand-off that just failed, so `name` + `type` alone do not satisfy it.

**Inputs are declarable too.** For a cross-app hand-off the **consumer** declares what it requires of
its input, and the producer references the consumer's published declaration rather than re-authoring
the field list. Ownership stays with the consumer.

**Where the generated file lands** is decided by your *contract*, not by how many `@entrypoint`
methods you wrote. The warning reads the committed `app/generated/` tree and names the file your
toolkit output actually writes:

| Generated tree | Artifact-schema path |
|---|---|
| One `manifest.json` at the root | `app/generated/artifact_schemas.json` |
| A `manifest.json` per entry-point subdirectory (a bundle) | `app/generated/{entry-point}/artifact_schemas.json`, one per entry point |

A **route/card-split** app — one marketplace card plus extra `@entrypoint`s the DAG invokes by
`workflow_type` — has a flat tree, so all of its entry points share the one flat file. That is why
the entry-point count is the wrong signal: counting it as a bundle would send you to a nested path
the toolkit never writes for that app.

`artifactSchemas` is a **per-entry-point** property, like `pipeline` and `uiConfig`. Declaring it on
a **bundle root** is a generation error: the root has no contract model, so a key there could not
name a real field. Two entry points that genuinely share an artifact assign one shared
`ArtifactSchema` value into both contracts.

Regenerate with `pkl eval -m . contract/app.pkl` (or `uv run poe generate`). Never hand-edit the
generated `artifact_schemas.json` — it is a pkl eval output and the next toolkit run reverts the
edit.

#### What you will see if you skip it

**A warning today, an error in v4.0.** An undeclared boundary `FileReference` is reported at worker
build, naming the field, the entry point and the file the declaration belongs in:

```
App 'my-connector' entry point 'run': output contract 'ExtractOutput' declares a
FileReference field 'transformed_entities' with no artifact schema. […] Declare it in
the app's pkl contract as artifactSchemas { ["transformed_entities"] = new
ArtifactSchema { ... } } and regenerate, so it lands in
app/generated/artifact_schemas.json. […] This is a warning today and will be an error
in v4.0.
```

It arrives as both a `DeprecationWarning` and a `warning` log line, and it never blocks
registration. Conformance rule **K016** (`EntrypointArtifactSchemaMissing`, WARN-tier) reports the
same gap in review, before a worker is ever built.

A *malformed* `artifact_schemas.json` is not treated as "declares nothing" — the entry point is
skipped with a log line saying why, so one bad JSON blob cannot produce a warning on every boundary
field.

#### What a declaration buys you at runtime

The activity interceptor validates every `FileReference` artifact against its declaration on both
sides of every task — at ingest (right after the file is materialised, before your code reads it)
and at hand-off (right after your task returns, while the bytes are still local so a flag blames the
producer). By default it is report-only: a mismatch is logged and counted, never blocked.

Every artifact emits one `"Artifact validation outcome"` row either way, including the negatives —
`not_declared`, `unsupported`, `absent` — because a check that reports nothing is indistinguishable
from a check that passed. So a boundary field you never declared is visible in ClickHouse as
`outcome=not_declared, boundary=true` rather than as an app that quietly validates zero records. See
[Monitoring — Artifact-validation outcome event](monitoring.md#artifact-validation-outcome-event).

#### Blocking on a bad artifact

Once an app's outcome rows show a false-positive rate it trusts, it can opt into blocking:

```python
class MyApp(App):
    artifact_validation_mode = "hard"   # default: "soft"
```

In `hard` mode a bad artifact fails the activity — blast radius one workflow, and at hand-off the
producing task is still on the stack, so the failure names whoever wrote the file rather than
whoever reads it three hops later. In `soft` mode the identical outcome emits
`artifact_enforcement="would_block"` and the run continues, which is how you measure what
graduating would cost before paying it.

Two things never block, whatever the mode: a failure of the SDK's own validator (it always fails
open — a defect in the check may not fail a healthy run), and an *undeclared* artifact on an
internal `@task` contract, since declaration is optional there by design. `ATLAN_ARTIFACT_VALIDATION_MODE`
overrides the attribute at deploy time, and only the literal `hard` enforces. See
[Monitoring — Artifact-validation posture](monitoring.md#artifact-validation-posture).

See [FileReference & App.upload()](file-reference.md) for what a `FileReference` is and how it moves,
and the contract toolkit's `examples/artifact-schemas/` for a full worked contract.

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

### Choosing a pool from measurements

Picking a pool means guessing a resource profile unless you have measured one. Activity sizing telemetry records peak container memory, CPU throttling and input bytes per execution, which is what turns "this feels memory-intensive" into a number you can size against. It is off by default and opt-in per activity — see [Monitoring → Activity Sizing Telemetry](monitoring.md#activity-sizing-telemetry).

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
