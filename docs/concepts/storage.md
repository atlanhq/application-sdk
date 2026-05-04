# Storage

The SDK uses `obstore` for all object storage operations, bypassing the Dapr sidecar entirely. The same API works with S3, GCS, Azure Blob, and local filesystem backends.

---

## Basic Operations

```python
from application_sdk.storage import upload_file, download_file

# Upload a local file to object storage (returns the SHA-256 hex digest of the uploaded file)
digest = await upload_file(
    "artifacts/my-app/output.json",  # key: destination object-store path
    "/tmp/output.json",              # local_path: source local file
)

# Download from object storage to a local file
await download_file(
    "artifacts/my-app/output.json",  # key: source object-store path
    "/tmp/output.json",              # local_path: destination local file
)
```

`key` is the object-store path. Leading slashes and `./local/tmp/...` workflow prefixes are stripped automatically by `normalize_key`. `local_path` is the local filesystem path.

### Additional operations

```python
from application_sdk.storage import delete, exists, list_keys

await delete("artifacts/my-app/output.json")
found = await exists("artifacts/my-app/output.json")
keys = await list_keys("artifacts/my-app/")  # returns list[str]
```

---

## FileReference

`FileReference` is a serialisable pointer to a file in object storage. Use it in task contracts to pass large data between tasks without embedding it in the Temporal payload (which has a 2 MB limit).

```python
from application_sdk.contracts import FileReference, StorageTier

ref = FileReference(
    storage_path="artifacts/my-app/batch-001.parquet",
    tier=StorageTier.TRANSIENT,
)
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `storage_path` | `str \| None` | Object-store key (single file) or prefix (directory) |
| `local_path` | `str \| None` | Local filesystem path, set after download |
| `tier` | `StorageTier` | Cleanup tier (see below); default `TRANSIENT` |
| `is_durable` | `bool` | `True` once the file has been uploaded to the object store; default `False` |
| `file_count` | `int` | Number of files this reference covers (default `1`) |

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
FileReference(storage_path="…", tier=StorageTier.TRANSIENT)

# Output artifact — kept for downstream consumers, removed on opt-in cleanup
FileReference(storage_path="…", tier=StorageTier.RETAINED)

# Connection config or incremental marker — kept forever
FileReference(storage_path="…", tier=StorageTier.PERSISTENT)
```

---

## App-Level Upload / Download

`App` provides two built-in methods for directory-level transfers with automatic `FileReference` tracking. Both accept `UploadInput` / `DownloadInput` objects:

```python
from application_sdk.contracts import UploadInput, DownloadInput, StorageTier

class MyConnector(App):
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        # Upload a local directory; returns UploadOutput with a single FileReference
        up = await self.upload(
            UploadInput(
                local_path="/tmp/output/",
                tier=StorageTier.TRANSIENT,
            )
        )
        # up.ref is a single FileReference (not a list)

        # Later, download back to a local path using a FileReference
        dl = await self.download(
            DownloadInput(
                ref=up.ref,
                local_path="/tmp/downloaded/",
            )
        )
        ...
```

Tracked `FileReference` objects are automatically registered for cleanup by `cleanup_storage()` at the end of the run.

---

## Cleanup

`App` runs two cleanup tasks in `on_complete()`:

- **`cleanup_files()`** — removes local temp paths from tracked `FileReference` objects and convention-based directories listed in `ATLAN_CLEANUP_BASE_PATHS`.
- **`cleanup_storage()`** — deletes remote files according to their `StorageTier`.

Both are called automatically by the default `on_complete()` implementation. Do not call them directly from `run()` — cleanup is tied to workflow completion, not mid-run state.

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
