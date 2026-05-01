# FileReference — Large-Payload Data Transfer

`FileReference` is the application-sdk mechanism for passing data larger than
Temporal's 2 MB payload limit between activities. Instead of embedding raw bytes
in an Input or Output model, a task declares a `FileReference` field and the SDK
handles upload and download automatically.

---

## Decision Matrix

| What you need | API | Notes |
|---|---|---|
| Pass a large file from task A to task B inside the same workflow | Declare a `FileReference \| None` field on your Input/Output Pydantic models | Auto persist (output) and materialize (input). Always relative to the local-deployment store. |
| Push a file to a stable path (run artifact, upstream artifact) | `await self.upload(UploadInput(local_path=..., tier=...))` → returns `UploadOutput` with `.ref: FileReference` | Single outbound API. Works identically in hosted and SDR deployments. |

Never embed raw bytes or large strings in Input/Output models — they will fail
Temporal's 2 MB payload limit at runtime.

---

## FileReference Lifecycle

### Persist (output side)

After a `@task` returns, `create_activity_from_task` inspects every `FileReference`
field on the Output. If a ref is **ephemeral** (`is_durable=False`, `local_path` set),
the SDK:

1. Uploads the local file or directory to the object store under `file_refs/{uuid}`
   (or a tier-specific prefix for `RETAINED`/`PERSISTENT` refs).
2. Writes a SHA-256 sidecar (`{storage_path}.sha256`) to the store for integrity
   verification.
3. Writes a local sidecar (`{local_path}.sha256`) so the same worker can skip
   re-download on the next activity run.
4. Returns a **durable** ref (`is_durable=True`, `storage_path` set) in the
   serialized Output payload.

### Materialize (input side)

Before a `@task` runs, the SDK inspects every `FileReference` field on the Input.
If a ref is **durable** and the local file is absent or its SHA-256 sidecar is
stale/missing, the SDK:

1. Downloads the file from the object store to a local temp path.
2. Verifies the SHA-256 against the stored sidecar.
3. Writes a fresh local sidecar.
4. Sets `local_path` on the ref so the task sees a ready-to-use local file.

The task author never calls upload or download manually — the plumbing is invisible.

### Tier Choice

| Tier | Default prefix | Cleanup |
|---|---|---|
| `StorageTier.TRANSIENT` | `file_refs/` | Deleted by `App.cleanup_storage()` at end of run. Use for inter-task intermediaries. |
| `StorageTier.RETAINED` | `artifacts/apps/{app}/workflows/{wf_id}/{run_id}/file_refs/` | Not auto-deleted. Use for run artifacts you want available after the workflow completes. |
| `StorageTier.PERSISTENT` | `persistent-artifacts/apps/{app_name}/file_refs/` | Never deleted by standard cleanup. Use for files that must survive across multiple runs. |

---

## The `Lazy()` Marker

By default every `FileReference` field is downloaded before the activity runs
(**eager** materialization). For activities that don't always need a large ref —
or that should decide at runtime whether to fetch it — mark the field `Lazy`:

```python
from typing import Annotated
from application_sdk.contracts.types import FileReference, Lazy

class MyInput(Input):
    manifest: FileReference | None = None           # eager (always downloaded)
    heavy_artifact: Annotated[FileReference | None, Lazy()] = None  # lazy (skipped)
```

The `heavy_artifact` field is left as a durable `FileReference` in the activity
input. Call `fetch()` inside the activity to download on demand:

```python
from application_sdk.storage.reference import fetch

async def my_task(self, input: MyInput) -> MyOutput:
    if input.heavy_artifact and needs_artifact():
        ref = await fetch(input.heavy_artifact)
        data = open(ref.local_path, "rb").read()
    ...
```

**Rule of thumb:** if your activity declares the field but doesn't always read it,
mark it `Lazy`. Lightweight activities (send-metrics, query-metastore) on tenant
inputs carrying large refs will time out if forced to download every field.

---

## Dedup Is Automatic

When the same `storage_path` appears in multiple input fields (e.g. a manifest
shared by several sub-tasks in the same fan-out), the SDK downloads it exactly
once and propagates `local_path` to all refs sharing that key. You do not need to
deduplicate manually.

A `file_ref.materialize.dedup_hit` debug event is emitted for each subsequent ref
that reuses an already-downloaded file.

---

## Common Pitfalls

**Do not construct `FileReference` outside an activity.**
The path helpers (`StorageTier._make_file_ref_path`) use `uuid.uuid4()` internally
and are restricted to activity context (Temporal sandbox non-determinism rules).
Only call `FileReference(local_path=...)` from inside a `@task`-decorated function.

**Do not put `FileReference` in a dict key.**
The SDK walks dict *values*, not keys. A `FileReference` used as a dict key will
not be persisted or materialized.

**Do not expect `FileReference` to point at upstream storage.**
`FileReference` is always relative to the local-deployment object store. Use
`await self.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED))` when
you need to push a file to a stable path visible outside the current run.

---

## Migration from `upload_to_atlan`

`BaseMetadataExtractor.upload_to_atlan` is deprecated. The v3 replacement for
inter-task file passing is the `FileReference` field pattern described above:

**v2 (deprecated):**
```python
# In run():
await self.upload_to_atlan(UploadInput(output_path=input.output_path))
```

**v3 pattern:**
```python
# In a @task — return FileReference on the Output:
output_file = Path(output_dir) / "tables.parquet"
return ExtractOutput(tables_file=FileReference(local_path=str(output_file)))

# In run() — push to a stable artifact path:
up = await self.upload(UploadInput(local_path=extract.tables_file.local_path,
                                   tier=StorageTier.RETAINED))
```

Note: `upload_to_atlan` performs a cross-store copy (deployment store →
upstream/Atlan store). The v3 equivalent for that operation is under active
development; do not remove existing `upload_to_atlan` call sites until a
replacement is available.

---

## Observability

The SDK emits structured `file_ref.*` log events that are routed through OTLP.
All fields below appear in OTLP exports and can be queried in Grafana.

| Event | When |
|---|---|
| `file_ref.persist.start` | Upload begins |
| `file_ref.persist.complete` | Upload finished; includes `bytes_uploaded`, `sha256`, `duration_ms` |
| `file_ref.persist.failed` | Upload error; includes `error_type`, `bytes_uploaded` |
| `file_ref.materialize.start` | Download begins; includes `file_size_bytes` |
| `file_ref.materialize.complete` | Download finished; includes `bytes_downloaded`, `sha256`, `duration_ms` |
| `file_ref.materialize.skipped` | Local sidecar matched — no download needed; `is_cache_hit=True` |
| `file_ref.materialize.failed` | Download error; includes `bytes_transferred_before_failure`, `error_type` |
| `file_ref.materialize.dedup_hit` | Second-or-later ref sharing a `storage_path`; `reused_local_path` shows the winner |
| `file_ref.materialize.lazy_skipped` | `Lazy()`-marked field was skipped |

Events for files ≥ 10 MiB are emitted at `INFO`; smaller files at `DEBUG`.
