# FileReference & App.upload() — Complete Developer Reference

This document is the single source of truth for large-payload data transfer in
application-sdk v3. Read it before writing any code that passes files between
tasks, writes run artifacts, or pushes data upstream.

---

## Why This Exists

Temporal serialises every Input and Output through its payload codec. The hard
limit is **2 MB per payload**. A single SQLite database, Parquet file, or JSON
manifest from a real connector run is almost always larger than that. Embedding
raw bytes in your model will fail at runtime with a cryptic Temporal serialisation
error.

`FileReference` solves this by storing the actual bytes in object storage (S3,
GCS, Azure Blob) and putting only a tiny pointer in the Temporal payload. The SDK
uploads and downloads automatically — task authors never call storage APIs
directly.

`App.upload()` solves a different problem: pushing a file to a **stable, addressable
path** that survives beyond the current run and is visible to external
systems (the Atlan platform, downstream pipelines, SDR).

These are two distinct APIs with distinct purposes. Using the wrong one causes subtle
failures.

---

## Decision Matrix — Which API to Use

| Scenario | API | Why |
|---|---|---|
| Pass a SQLite / Parquet / JSON file from task A to task B in the same run | `FileReference` field on Input/Output | Auto persisted on output, auto materialised on input. Zero storage code needed. |
| Task B always needs what task A produced, and the file is always present | `FileReference` field (eager, default) | SDK downloads it before task B runs. |
| Task B only sometimes needs the file (e.g. only when `mode == "full"`) | `Annotated[FileReference \| None, Lazy()]` | SDK skips the download; task B fetches on demand with `fetch()`. |
| Multiple tasks all share the same large manifest file | Single `FileReference` passed through the chain | SDK downloads it exactly once per task via dedup. |
| Push a completed artifact so it appears in the Atlan UI after the run | `await self.upload(UploadInput(..., tier=StorageTier.RETAINED))` | Writes to a stable run-scoped prefix that the platform indexes. |
| Write a file that must persist across multiple runs (e.g. incremental bookmark) | `await self.upload(UploadInput(..., tier=StorageTier.PERSISTENT))` | Writes to a fixed path not cleaned up between runs. |
| Directory of output files (e.g. partitioned Parquet) from task A to task B | `FileReference(local_path=str(dir_path))` — same API, directory-aware | SDK uploads all files in the directory and re-creates the structure on download. |

**Key rule:** `FileReference` is for *within-run* transfer. `App.upload()` is
for *durable outbound* delivery.

### Which store does each API write to?

These two APIs route to **different object stores**:

| API | Store | Who can read it |
|-----|-------|-----------------|
| `FileReference` (via interceptor) | `objectstore` — `infra.storage`, customer-owned | Other tasks in the same deployment |
| `App.upload()` | `atlan-objectstore` — `infra.upstream_storage`, Atlan-owned (in SDR); falls back to `infra.storage` in local dev | Atlan system apps (publish, QI, lineage) and the Atlan UI |

The activity interceptor's persist step always writes `FileReference` objects to
`infra.storage` (`objectstore`). This is intentional: intermediate task outputs
stay inside the customer's deployment perimeter and never cross into Atlan's
infrastructure unless the connector explicitly decides to hand them off.

`App.upload()` uses `upstream_storage or storage`. In SDR deployments
`upstream_storage` is the `atlan-objectstore` Dapr binding (pointing to
`{tenant}/api/blobstorage`), so `App.upload()` routes to Atlan's S3.

**Consequence for connector authors:** if your connector produces artifacts that
Atlan's publish app must consume, you must call `App.upload()` explicitly from
`run()` — the interceptor alone is not sufficient. Omitting the explicit call
produces a silent failure: the DAG runs to completion but the publish app finds
nothing in Atlan's S3 and publishes zero assets. See
[ADR-0014](../adr/0014-two-store-storage-architecture.md) for the full
rationale.

---

## Part 1 — FileReference

### What Is a FileReference?

A frozen Pydantic model that holds a pointer to a file (or directory) in object
storage plus an optional local path on the current worker:

```python
class FileReference(BaseModel, frozen=True):
    local_path: str | None = None      # absolute path on the worker's disk
    storage_path: str | None = None    # object-store key (set after persist)
    is_durable: bool = False           # True once uploaded to the store
    tier: StorageTier = StorageTier.TRANSIENT
    file_count: int | None = None      # set for directories; number of files
    auto_materialize: bool = True      # False = SDK never touches this ref
```

You never set `storage_path`, `is_durable`, or `file_count` yourself — the SDK
fills them in during persist and materialize.

### Constructing a FileReference

Constructing a `FileReference` is just a Pydantic model instantiation — it works
anywhere (a `@task`, `run()`, a helper function). What requires a `@task`-decorated
method is **persisting** the ref so the SDK can auto-upload it on activity return:

```python
# Single file
ref = FileReference(local_path="/tmp/output/tables.parquet")

# Single file, non-default tier
ref = FileReference(
    local_path="/tmp/output/tables.parquet",
    tier=StorageTier.RETAINED,
)

# Directory (SDK walks all files recursively)
ref = FileReference(local_path="/tmp/output/")

# Opt-out of automatic lifecycle (advanced — you own download/upload)
ref = FileReference(local_path="/tmp/special.bin", auto_materialize=False)
```

The SDK only auto-persists a `FileReference` when it is **returned from a `@task`
method** as part of an `Output` model. A ref constructed in `run()` and never
returned from a task will not be uploaded — `run()` is a Temporal workflow
function with sandbox restrictions, and the upload happens activity-side. As a
rule of thumb: build refs inside `@task` methods and return them; treat refs
received in `run()` as already-durable handles to pass between tasks.

### Tiers

The tier controls **where** the file is stored and **when it is cleaned up**.

#### `StorageTier.TRANSIENT` (default)

```
Prefix:  file_refs/{uuid}
Cleanup: deleted by App.cleanup_storage() at the end of every run
Use for: intermediate files between tasks that are no longer needed after
         the run completes
```

Example — extraction output that the transform task needs but nothing else:

```python
class ExtractOutput(Output):
    raw_db: FileReference | None = None

# In the extract task:
return ExtractOutput(
    raw_db=FileReference(local_path=str(sqlite_path))
    # tier=StorageTier.TRANSIENT is the default
)
```

#### `StorageTier.RETAINED`

```
Prefix:  artifacts/apps/{app_name}/workflows/{workflow_id}/{run_id}/file_refs/
Cleanup: NOT deleted by standard cleanup — must be removed manually or via
         a retention policy
Use for: run artifacts you want available after the run completes
         (downloadable from the Atlan UI, inspectable for debugging)
```

Example — a processed Parquet file that needs to be accessible after the run:

```python
class TransformOutput(Output):
    result_parquet: FileReference | None = None

return TransformOutput(
    result_parquet=FileReference(
        local_path=str(output_parquet),
        tier=StorageTier.RETAINED,
    )
)
```

#### `StorageTier.PERSISTENT`

```
Prefix:  persistent-artifacts/apps/{app_name}/file_refs/
Cleanup: never deleted by any standard SDK operation
Use for: files that must survive across multiple runs — incremental
         watermarks, shared lookup tables, cross-run caches
```

Example — an incremental bookmark that the next run will read:

```python
class BookmarkOutput(Output):
    watermark_file: FileReference | None = None

return BookmarkOutput(
    watermark_file=FileReference(
        local_path=str(watermark_path),
        tier=StorageTier.PERSISTENT,
    )
)
```

### Lifecycle in Detail

#### Persist (output side)

After your `@task` returns an Output containing an ephemeral `FileReference`
(one with `local_path` set but `is_durable=False`), the SDK automatically
uploads it to **`infra.storage`** (`objectstore` — the deployment store):

1. Computes the storage path from the tier prefix.
2. Uploads the local file (or all files in a local directory) to the store.
3. Writes a `{storage_path}.sha256` sidecar to the store — used for integrity
   verification on download.
4. Writes a `{local_path}.sha256` sidecar locally — allows the same worker to
   skip re-download if the task is retried locally.
5. Returns a **durable** ref (`is_durable=True`, `storage_path` set) in the
   serialised Output that Temporal stores.

The original local file is **not deleted** unless you pass
`retain_local_copy=False` to `App.upload()` (that is a different code path).

#### Materialize (input side)

Before your `@task` runs, the SDK inspects every `FileReference` field on the
Input. For each durable ref that needs downloading (file absent, sidecar missing,
or sidecar hash mismatch), the SDK:

1. Downloads the file from the store to a temp path under `TEMPORARY_PATH`.
2. Verifies the downloaded content SHA-256 against the stored sidecar.
3. Writes a fresh local sidecar.
4. Sets `local_path` on the ref so your task sees a ready-to-use local file.

**Retry safety:** if the task retries on a different worker pod, `local_path`
will be a stale path from another machine. The sidecar check catches this: the
local sidecar is absent (different pod, different disk), so the SDK re-downloads.

**Cache hit:** if the same worker retries and the local file + sidecar are intact,
the SDK skips the download entirely (`file_ref.materialize.skipped` event emitted
at DEBUG).

#### Directory FileReferences

Passing a directory works exactly like a single file from the task author's
perspective:

```python
# Produce: task returns a directory ref
output_dir = Path("/tmp/output/partitioned/")
return ExtractOutput(result_dir=FileReference(local_path=str(output_dir)))

# Consume: task receives a materialized directory ref
async def transform(self, input: TransformInput) -> TransformOutput:
    dir_path = Path(input.result_dir.local_path)
    for parquet_file in dir_path.glob("*.parquet"):
        process(parquet_file)
```

The SDK uploads every file in the directory recursively (bounded concurrency),
preserves the directory structure under the storage prefix, and recreates it on
download. `file_count` on the durable ref tells you how many files were uploaded.

The per-file sidecar fast-path applies inside directories too: on retry, files
that already have a matching local sidecar are skipped; only changed or missing
files are re-downloaded.

### Lazy Materialization

By default every `FileReference` field is downloaded before the task runs.
Mark a field `Lazy` to skip the automatic download:

```python
from typing import Annotated
from application_sdk.contracts.types import FileReference, Lazy

class MyInput(Input):
    # This is always downloaded before the task runs:
    manifest: FileReference | None = None

    # This is LEFT AS A DURABLE REF — not downloaded automatically:
    full_export: Annotated[FileReference | None, Lazy()] = None
```

Inside the task, call `fetch()` to download on demand:

```python
from application_sdk.storage.reference import fetch

async def my_task(self, input: MyInput) -> MyOutput:
    if input.full_export and self.needs_full_export():
        ref = await fetch(input.full_export)  # downloads now, cached for this task
        with open(ref.local_path, "rb") as f:
            data = f.read()
```

**When to use `Lazy`:**

- Your task only sometimes needs the file (conditional logic, mode flags).
- Lightweight tasks (send-metrics, notify, query-metastore) that happen to
  receive an Input model with large refs they don't use — forcing a 500 MB
  download on a 50 ms task will cause timeouts.
- Fan-out tasks that receive a shared manifest but only need their slice.

**When NOT to use `Lazy`:**

- The task always reads the file — eager download is simpler and you get the
  download logged and retried before your code runs.

### Dedup — Same File, Multiple Fields

If multiple Input fields point at the same `storage_path` (e.g. a shared spec
file passed through a pipeline), the SDK downloads it exactly once:

```python
class PipelineInput(Input):
    spec: FileReference | None = None          # storage_path = "file_refs/abc"
    spec_copy: FileReference | None = None     # storage_path = "file_refs/abc"  (same)
```

Both fields end up with the same `local_path` after materialization. A
`file_ref.materialize.dedup_hit` DEBUG event is emitted for the second ref. You
do not need to deduplicate manually.

---

## Part 2 — App.upload()

### What It Does

`App.upload()` uploads a local file to a **stable, run-scoped path** in the
deployment object store and returns a `FileReference` pointing at it. Use it when:

- You want the artifact to outlive the current run.
- You need to push a file to Atlan's artifact layer (visible in the UI,
  downloadable by support, indexed by the platform).
- You need a `FileReference` with a known prefix (not a random `file_refs/{uuid}`).

### Signature

```python
await self.upload(input: UploadInput) -> UploadOutput
```

### UploadInput Fields

```python
class UploadInput(BaseModel):
    local_path: str                            # required: path to the file on disk
    tier: StorageTier = StorageTier.RETAINED   # where to store it
    storage_path: str | None = None            # override the full destination key
    storage_subdir: str | None = None          # append a subdir under the run prefix
    skip_if_exists: bool = False               # skip upload when remote SHA-256 matches
```

| Field | Required | Description |
|---|---|---|
| `local_path` | Yes | Absolute path to the file to upload. |
| `tier` | No (default `RETAINED`) | Tier that controls the destination prefix and cleanup policy. |
| `storage_path` | No | Fully-qualified destination key. Overrides the auto-generated path. Use this when you need an exact fixed path (e.g. `argo-artifacts/spec.json`). |
| `storage_subdir` | No | Subdirectory appended under the run prefix. Useful for grouping related uploads without spelling out the full path. |
| `skip_if_exists` | No (default `False`) | When `True`, skip uploading files whose SHA-256 already matches the stored `{key}.sha256` sidecar. Useful for retried tasks and idempotent re-uploads. |

### Path Computation

When `storage_path` is **not** set, the destination key is computed as:

```
{run_prefix}/{tier_subdir}/{filename}
```

Where `run_prefix` is:

```
artifacts/apps/{app_name}/workflows/{workflow_id}/{run_id}
```

And `tier_subdir` depends on the tier:

| Tier | tier_subdir |
|---|---|
| `RETAINED` | `file_refs/` |
| `PERSISTENT` | (uses `persistent-artifacts/` as the base, ignores run_prefix) |

When `storage_subdir` is set:

```
{run_prefix}/{storage_subdir}/{filename}
```

### UploadOutput

```python
class UploadOutput(BaseModel):
    ref: FileReference    # durable ref pointing at the uploaded file
```

The returned `ref` is already durable (`is_durable=True`, `storage_path` set).
You can pass it as a `FileReference` field on a subsequent task's Input.

### Usage Patterns

#### Pattern 1 — Upload a run artifact (most common)

```python
from application_sdk.contracts.storage import UploadInput
from application_sdk.contracts.types import StorageTier

# Inside run() or a @task method:
upload_result = await self.upload(
    UploadInput(
        local_path="/tmp/output/tables.parquet",
        tier=StorageTier.RETAINED,
    )
)
# upload_result.ref is now a durable FileReference
# storage_path will be something like:
# artifacts/apps/myapp/workflows/wf-123/run-456/file_refs/tables.parquet
```

#### Pattern 2 — Upload to a known subdir (e.g. argo-artifacts pattern)

```python
upload_result = await self.upload(
    UploadInput(
        local_path="/tmp/spec.json",
        storage_subdir="argo-artifacts",
    )
)
# storage_path: artifacts/apps/myapp/workflows/.../run-.../argo-artifacts/spec.json
```

#### Pattern 3 — Upload to a fully-specified path

```python
upload_result = await self.upload(
    UploadInput(
        local_path="/tmp/spec.json",
        storage_path="argo-artifacts/my-connector/latest-spec.json",
    )
)
# storage_path: argo-artifacts/my-connector/latest-spec.json (exactly)
```

#### Pattern 4 — Upload then pass the ref to another task

```python
# In the extract task:
upload_result = await self.upload(
    UploadInput(local_path=extract_output.db_path, tier=StorageTier.RETAINED)
)

# Pass the durable ref to the transform task:
transform_input = TransformInput(source_db=upload_result.ref)
transform_output = await self.transform(transform_input)
```

#### Pattern 5 — PERSISTENT for cross-run state

```python
# Write and upload a watermark after each successful run:
watermark_path = Path(TEMPORARY_PATH) / "watermark.json"
watermark_path.write_text(json.dumps({"last_run": arrow.utcnow().isoformat()}))

await self.upload(
    UploadInput(
        local_path=str(watermark_path),
        tier=StorageTier.PERSISTENT,
        storage_path=f"persistent-artifacts/apps/{self._app_name}/watermark.json",
    )
)
```

### upload() vs FileReference — Side-by-Side

```python
# ── FileReference (inter-task, automatic) ─────────────────────────────────

class ExtractOutput(Output):
    raw_db: FileReference | None = None     # tier=TRANSIENT by default

# In the extract task — just set local_path:
return ExtractOutput(raw_db=FileReference(local_path=str(db_path)))

# In the transform task — local_path is already set by the SDK:
async def transform(self, input: TransformInput) -> TransformOutput:
    db = sqlite3.connect(input.raw_db.local_path)   # ready to use


# ── App.upload() (outbound, stable path) ──────────────────────────────────

# In the run() method — push result for the platform to index:
result = await self.upload(
    UploadInput(local_path=transform_output.result_parquet.local_path,
                tier=StorageTier.RETAINED)
)
# result.ref.storage_path is stable and survives workflow completion
```

---

## Part 3 — End-to-End Examples

### Example A — Extract → Transform → Upload (typical connector)

```python
# contracts.py
class ExtractOutput(Output):
    raw_db: FileReference | None = None          # TRANSIENT: temp inter-task file

class TransformInput(Input):
    raw_db: FileReference | None = None

class TransformOutput(Output):
    result_parquet: FileReference | None = None  # RETAINED: kept after run


# connector.py
class MyConnector(App):
    @task
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        db_path = Path(TEMPORARY_PATH) / "raw.db"
        run_extraction(db_path)
        return ExtractOutput(raw_db=FileReference(local_path=str(db_path)))
        # SDK uploads raw.db to file_refs/{uuid} automatically on return

    @task
    async def transform(self, input: TransformInput) -> TransformOutput:
        # SDK downloaded raw.db before this method was called
        df = read_sqlite(input.raw_db.local_path)
        out = Path(TEMPORARY_PATH) / "result.parquet"
        df.to_parquet(out)
        return TransformOutput(
            result_parquet=FileReference(local_path=str(out), tier=StorageTier.RETAINED)
        )
        # SDK uploads result.parquet to artifacts/.../run-.../file_refs/result.parquet

    async def run(self, input: RunInput) -> RunOutput:
        extract_out = await self.extract(ExtractInput(...))
        transform_out = await self.transform(
            TransformInput(raw_db=extract_out.raw_db)
        )
        # Push artifact so the platform can index it:
        upload = await self.upload(
            UploadInput(
                local_path=transform_out.result_parquet.local_path,
                tier=StorageTier.RETAINED,
                storage_subdir="argo-artifacts",
            )
        )
        return RunOutput(artifact_ref=upload.ref)
```

### Example B — Fan-out with shared manifest and lazy heavy ref

```python
class ParseInput(Input):
    manifest: FileReference | None = None                             # eager
    full_snapshot: Annotated[FileReference | None, Lazy()] = None    # lazy

class MyConnector(App):
    @task
    async def parse(self, input: ParseInput) -> ParseOutput:
        # manifest is already downloaded — use it directly
        items = json.loads(Path(input.manifest.local_path).read_text())

        if self.requires_full_snapshot(items):
            # Only download the large snapshot when actually needed
            snap_ref = await fetch(input.full_snapshot)
            snapshot_data = load_snapshot(snap_ref.local_path)

        return ParseOutput(results=process(items))
```

### Example C — Persistent watermark across runs

```python
async def run(self, input: RunInput) -> RunOutput:
    # Try to read the watermark from the previous run:
    watermark_key = f"persistent-artifacts/apps/{self._app_name}/watermark.json"
    try:
        local_wm = await download_file(watermark_key, "/tmp/watermark.json")
        last_run = json.loads(Path("/tmp/watermark.json").read_text())["last_run"]
    except StorageNotFoundError:
        last_run = None   # first run

    # ... do the work ...

    # Write updated watermark:
    Path("/tmp/watermark.json").write_text(json.dumps({"last_run": now_iso()}))
    await self.upload(UploadInput(
        local_path="/tmp/watermark.json",
        storage_path=watermark_key,
        tier=StorageTier.PERSISTENT,
    ))
```

---

## Part 4 — Common Pitfalls

### Do not construct FileReference outside a @task

```python
# WRONG — module level or __init__:
MY_REF = FileReference(local_path="/tmp/file.bin")   # breaks Temporal sandbox

# CORRECT — inside a @task function body:
@task
async def my_task(self, input) -> Output:
    ref = FileReference(local_path="/tmp/file.bin")
    return Output(ref=ref)
```

### Do not embed large bytes in Input/Output directly

```python
# WRONG:
class BadOutput(Output):
    raw_bytes: bytes = b""         # will hit 2 MB Temporal limit

# CORRECT:
class GoodOutput(Output):
    data_file: FileReference | None = None
```

### Do not put FileReference in a dict key

```python
# WRONG — the SDK walks dict values, not keys:
data = {ref: "some_value"}         # ref will not be persisted

# CORRECT:
data = {"file": ref}
```

### Do not read local_path before the task runs

```python
# WRONG — in run(), local_path is a stale hint, not guaranteed to exist:
path = input.ref.local_path
data = open(path).read()           # may not exist on this machine

# CORRECT — read local_path only inside @task, after the SDK has materialised it:
@task
async def my_task(self, input: MyInput) -> MyOutput:
    data = open(input.ref.local_path).read()   # safe: SDK set this before call
```

### Do not use FileReference for outbound delivery

```python
# WRONG — FileReference is internal to the run:
return Output(ref=FileReference(local_path=str(result), tier=StorageTier.RETAINED))
# The Atlan platform cannot index this on its own

# CORRECT — use App.upload() to push to a stable, platform-visible path:
await self.upload(UploadInput(local_path=str(result), tier=StorageTier.RETAINED))
```

---

## Part 5 — Observability

All events below are structured log records routed through OTLP. They appear in
Grafana under the `application_sdk.storage` logger with the fields listed.

Events for files ≥ 10 MiB are logged at `INFO`; smaller files at `DEBUG`.

### Persist events

| Event | Fields |
|---|---|
| `file_ref.persist.start` | `storage_path`, `local_path`, `file_size_bytes`, `tier` |
| `file_ref.persist.complete` | `storage_path`, `bytes_uploaded`, `duration_ms`, `sha256` |
| `file_ref.persist.failed` | `storage_path`, `local_path`, `error_type`, `bytes_uploaded` |

### Materialize events

| Event | Fields |
|---|---|
| `file_ref.materialize.start` | `storage_path`, `file_size_bytes`, `is_cache_hit=False` |
| `file_ref.materialize.complete` | `storage_path`, `bytes_downloaded`, `duration_ms`, `sha256` |
| `file_ref.materialize.skipped` | `storage_path`, `local_path`, `is_cache_hit=True` |
| `file_ref.materialize.failed` | `storage_path`, `bytes_transferred_before_failure`, `error_type` |
| `file_ref.materialize.dedup_hit` | `storage_path`, `dedup_key`, `reused_local_path`, `local_path` |
| `file_ref.materialize.lazy_skipped` | `storage_path` |

### Low-level transfer events

| Event | Fields |
|---|---|
| `storage.upload success` | `store_path`, `size_bytes`, `elapsed_ms`, `throughput_mibps` |
| `storage.download success` | `store_path`, `size_bytes`, `elapsed_ms`, `throughput_mibps` |
| `storage.upload failure` | `store_path`, `error_class`, `elapsed_ms` |
| `storage.download failure` | `store_path`, `error_class`, `elapsed_ms` |

---

## Part 6 — Migration from upload_to_atlan

`BaseMetadataExtractor.upload_to_atlan` is **deprecated**.  It still works (it
now just forwards to `App.upload` internally), but emits a `DeprecationWarning`
and will be removed in the next major SDK release.

**Deprecated:**

```python
await self.upload_to_atlan(UploadInput(output_path=input.output_path))
```

**Replacement:**

```python
up = await self.upload(
    UploadInput(
        local_path=input.output_path,
        tier=StorageTier.RETAINED,
    )
)
records_uploaded = up.ref.file_count or 0
```

`App.upload()` is concurrent, SHA-256 verified, and works identically in hosted
and SDR deployments.
