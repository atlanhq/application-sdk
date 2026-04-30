# Storage

The SDK uses `obstore` for all object storage operations, bypassing the Dapr sidecar entirely. The same API works with S3, GCS, Azure Blob, and local filesystem backends.

---

## Basic Operations

```python
from application_sdk.storage import upload_file, download_file

# Upload a local file to object storage
await upload_file(
    source="/tmp/output.json",
    key="artifacts/my-app/output.json",
)

# Download from object storage to a local file
await download_file(
    key="artifacts/my-app/output.json",
    destination="/tmp/output.json",
)
```

`key` is the object-store path (no leading slash). `source`/`destination` are local filesystem paths.

### Additional operations

```python
from application_sdk.storage import delete_file, file_exists, list_keys

await delete_file("artifacts/my-app/output.json")
exists = await file_exists("artifacts/my-app/output.json")
keys = await list_keys("artifacts/my-app/")  # returns list[str]
```

---

## FileReference

`FileReference` is a serialisable pointer to a file in object storage. Use it in task contracts to pass large data between tasks without embedding it in the Temporal payload (which has a 2 MB limit).

```python
from application_sdk.contracts import FileReference, StorageTier

ref = FileReference(
    key="artifacts/my-app/batch-001.parquet",
    tier=StorageTier.TRANSIENT,
)
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `key` | `str` | Object-store path |
| `tier` | `StorageTier` | Cleanup tier (see below) |
| `local_path` | `str \| None` | Optional cached local path after download |

`FileReference` is safe to include in `Output` models because it is small (a path string + an enum). The actual file stays in object storage.

---

## StorageTier

`StorageTier` controls when `cleanup_storage()` deletes a file:

| Tier | Path prefix | Cleanup |
|------|------------|---------|
| `TRANSIENT` | `file_refs/` | Always removed at end of run |
| `RETAINED` | `{run_prefix}/file_refs/` | Removed only when `include_prefix_cleanup=True` (opt-in) |
| `PERSISTENT` | `persistent-artifacts/apps/{app_name}/…` | Never deleted by cleanup |

```python
from application_sdk.contracts import StorageTier

# Intermediate working file — deleted at end of every run
FileReference(key="…", tier=StorageTier.TRANSIENT)

# Output artifact — kept for downstream consumers, removed on opt-in cleanup
FileReference(key="…", tier=StorageTier.RETAINED)

# Connection config or incremental marker — kept forever
FileReference(key="…", tier=StorageTier.PERSISTENT)
```

---

## App-Level Upload / Download

`App` provides two built-in `@task` methods for directory-level transfers with automatic `FileReference` tracking:

```python
class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        # Upload a local directory; returns a list of FileReference objects
        refs = await self.upload(
            local_dir="/tmp/output/",
            prefix="artifacts/my-app/run-001/",
            tier=StorageTier.TRANSIENT,
        )

        # Later, download back to a local path
        await self.download(refs, local_dir="/tmp/downloaded/")
        ...
```

Tracked `FileReference` objects are automatically registered for cleanup by `cleanup_storage()` at the end of the run.

---

## Cleanup

`App` runs two cleanup tasks in `on_complete()`:

- **`cleanup_files()`** — removes local temp paths from tracked `FileReference` objects and convention-based directories listed in `ATLAN_CLEANUP_BASE_PATHS`.
- **`cleanup_storage()`** — deletes remote files according to their `StorageTier`.

You can also call them mid-run to reclaim space after large intermediate steps:

```python
async def run(self, input):
    out = await self.fetch_large_batch(...)
    # Reclaim storage before the next big step
    await self.cleanup_storage()
    return await self.transform(...)
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS` | `4` | Max concurrent upload/download operations |
| `ATLAN_TEMPORARY_PATH` | `./local/tmp/` | Base path for local temporary files |
| `ATLAN_CLEANUP_BASE_PATHS` | _(empty)_ | Extra prefixes to clean up (comma-separated) |

---

## Backend Selection

The object-store backend is configured via Dapr component YAML at deploy time (see `components/objectstore.yaml` in the repo). No code changes are needed to switch between S3, GCS, Azure Blob, or local filesystem. For local development, the default components target a local filesystem path.
