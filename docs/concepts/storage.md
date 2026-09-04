# Storage

The SDK uses `obstore` for all object storage operations, bypassing the Dapr sidecar entirely. The same API works with S3, GCS, Azure Blob, and local filesystem backends.

---

## Two-Store Architecture

The SDK maintains two distinct object-store references, each serving a different
purpose:

| Dapr component | SDK reference | Owner | Purpose |
|----------------|--------------|-------|---------|
| `objectstore` | `infra.storage` | Customer / deployment | Task-to-task `FileReference` durability within a run |
| `atlan-objectstore` | `infra.upstream_storage` | Atlan | Final artifact hand-off to Atlan system apps (publish, QI, lineage) |

**Task-to-task transfers** use `infra.storage`. The activity interceptor
automatically uploads every `FileReference` returned from a `@task` to this
store, keeping all intermediate data inside the customer's deployment perimeter.
`objectstore` can be any backend the customer controls (S3, Azure Blob, GCS,
local disk) — Atlan's infrastructure never reads from it.

**App-to-app hand-off** uses `infra.upstream_storage`. When a connector's
extract activity produces artifacts that Atlan's publish or lineage apps must
consume, the connector calls `App.upload()` explicitly. In SDR deployments
`upstream_storage` points to `atlan-objectstore` (Atlan's S3-compatible
blobstorage proxy); in local dev it is `None` and `App.upload()` falls back to
the deployment store.

```
Connector run (customer's cluster)
  @task extract  ──► FileReference ──► objectstore (infra.storage, customer-owned)
  @task transform ──► FileReference ──► objectstore
  run()          ──► App.upload()  ──► atlan-objectstore (infra.upstream_storage, Atlan-owned)
                                         │
                                         ▼
                                  Atlan publish app reads → Atlas
```

**The key rule:** rely on the interceptor for intra-run durability; call
`App.upload()` explicitly for any data that must cross into Atlan's
infrastructure. Relying on the interceptor alone for the app-to-app
hand-off produces a silent failure — all DAG nodes succeed but the publish
app finds nothing to publish.

See [ADR-0014](../adr/0014-two-store-storage-architecture.md) for the full
rationale, fallback behaviour, and consequences.

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

`delete_prefix(prefix)` removes every object under a prefix and returns the
number of objects this call actually removed. A key that vanishes between the
listing and the delete — a concurrent writer removed it first — is benign (the
desired end state is already true for it): it is logged as a warning naming the
prefix and excluded from the returned count rather than failing the caller.
Every other deletion error still raises `StorageError`.

### Prefix downloads

`download_prefix` writes one of two layouts, and picking the wrong one is
silent — the bytes arrive, just not where the reader looks:

```python
from application_sdk.storage import download_prefix

# Default: the full store path is preserved under local_dir.
# key "artifacts/run/transformed/table/a.json" → "<out>/artifacts/run/transformed/table/a.json"
await download_prefix("artifacts/run/transformed", "<out>")

# strip_prefix=True: only the tree *under* the prefix lands in local_dir.
# key "artifacts/run/transformed/table/a.json" → "<out>/table/a.json"
await download_prefix("artifacts/run/transformed", "<out>", strip_prefix=True)
```

Use `strip_prefix=True` whenever `local_dir` already names the same directory as
the prefix (the usual shape when recovering a run's `transformed/` or
`current-state/` tree), otherwise the prefix appears twice and a reader keyed on
a fixed subpath such as `<out>/table` finds nothing.

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
| `quarantined` | `bool` | Store under the quarantine root (see below); default `False` |

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

## Quarantined storage

Tier describes *lifecycle*. How sensitive the data is, is a separate axis.

Set `quarantined=True` when you are storing **raw content pulled straight from a source
system** — a downloaded workbook, report definition, or any file the customer authored.
Such files routinely carry warehouse hostnames, database and schema references, usernames,
authoring filesystem paths, and literal filter values, so they are treated as sensitive by
default rather than inspected to decide.

The flag is orthogonal to `tier`: quarantined data keeps whichever lifecycle it needs, and
the tier's ordinary prefix becomes a sub-prefix beneath the quarantine root.

| Tier | Default | `quarantined=True` |
|------|---------|--------------------|
| `TRANSIENT` | `file_refs/…` | `quarantine/file_refs/…` |
| `RETAINED` | `{run_prefix}/file_refs/…` | `quarantine/{run_prefix}/file_refs/…` |
| `PERSISTENT` | `persistent-artifacts/apps/{app_name}/…` | `quarantine/persistent-artifacts/apps/{app_name}/…` |

```python
from application_sdk.contracts import StorageTier, UploadInput

# Raw definition files downloaded from the source, kept for the run's consumers.
await self.upload(
    UploadInput(
        local_path="/tmp/definitions",
        tier=StorageTier.RETAINED,
        quarantined=True,
        storage_subdir="definitions",   # placed inside the resolved quarantine prefix
    )
)
```

The root defaults to `quarantine` and is settable with `ATLAN_QUARANTINE_PREFIX`. Access
control, encryption, and the retention policy are applied to that prefix by the deployment,
which is what makes the guarantee enforceable rather than per-connector convention.

Three things worth knowing:

- **It is opt-in.** Nothing moves unless you set the flag, so existing storage keys are
  unchanged. The corollary is that a raw-source write which forgets the flag lands in an
  ordinary prefix.
- **`quarantined=True` cannot be combined with an explicit `storage_path`.** An explicit key
  is used verbatim and bypasses tier resolution, so the pair would quietly write a
  *non*-quarantined key. Use `storage_subdir` to place files within the resolved prefix.
- **Quarantine is a location, not redaction.** The bytes still exist at rest. If a raw
  payload contains live credentials, quarantining it is not sufficient — it should not be
  stored.

See [ADR-0021](../adr/0021-quarantined-storage.md) for the full rationale.

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
| `ATLAN_OBSTORE_READ_TIMEOUT` | `90s` | Progress-based liveness bound — fail only if no bytes arrive for this long |
| `ATLAN_OBSTORE_TIMEOUT` | `30m` | Overall per-request wall-clock backstop |
| `ATLAN_STORAGE_RESUME_DOWNLOADS` | `true` | Resume interrupted chunked downloads from their checkpoint sidecar |
| `ATLAN_STORAGE_PROGRESS_LOG_INTERVAL_SECONDS` | `30` | Heartbeat log interval during long transfers (`0` disables) |
| `ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES` | `8388608` (8 MiB) | Multipart part size for uploads. Raise it when the destination makes part *count* expensive (e.g. an S3 proxy fronting GCS, which emulates multipart with 32-source-capped `compose` round trips) |
| `ATLAN_STORAGE_UPLOAD_MAX_CONCURRENCY` | `12` | Parts uploaded concurrently. Peak upload memory is roughly part size times this — lower it alongside a larger part size to hold memory steady |
| `ATLAN_STORAGE_VERIFY_TRANSFERS` | `true` | Validate every transfer's bytes (see [Transfer integrity](#transfer-integrity)) |
| `ATLAN_STORAGE_WRITE_SIDECARS` | `true` | Emit the `{key}.sha256` sidecar that downstream verification reads |

---

## Transfer integrity

A producing app that dies part-way through a write — the motivating case was
`ENOSPC` during a state carry-forward — leaves a *truncated* artifact in the
object store and still reports success. The consuming app then downloads that
file on every retry and fails inside its parser (`Malformed JSON … unexpected
end of data`), burning the whole retry budget on a deterministically corrupt
input and attributing the failure to the consumer instead of the producer.

The SDK closes that loop at the byte layer both apps share, so every transfer
path is covered by the same checks rather than each caller rolling its own:

**On upload** (`upload_file`, and therefore `App.upload`, `FileReference`
persist, `upload_prefix`, and the writer chunk uploads):

1. the local file must not shrink between the opening `stat` and the read
   reaching EOF — a file truncated under the reader would put a prefix of the
   intended artifact in the store (`StorageIntegrityError`);
2. a HEAD after the writer closes must report the byte count that was sent —
   this is what catches a backend silently dropping the object (`StorageError`,
   retryable);
3. the SHA-256 computed during the upload pass is written to `{key}.sha256`.

**On download** (`download_file` / `download_file_chunked`, and therefore
`App.download`, `FileReference` materialize, `download_prefix`, and the
incremental-state fetches):

1. the bytes written to disk must match the size the store declared for the
   object — a shortfall is a truncated transfer (`StorageError`, retryable);
2. when `{key}.sha256` exists, the content must hash to it. A mismatch means
   the stored object is corrupt at source and raises `StorageIntegrityError` —
   **non-retryable**, naming the key and both digests, because a byte-stable
   corrupt file fails identically on every attempt.

Sidecars are SDK bookkeeping, never data: every listing helper
(`list_data_keys`, `list_data_objects`, …) excludes them, and `download_prefix`
consumes rather than mirrors them, so a directory handed to a reader that globs
it never contains files the producer did not write.

Objects written before this protocol existed, or by a non-SDK producer, simply
have no sidecar; those downloads keep the size check and skip the digest check.

### What this does not prove

The sidecar attests to **the bytes the SDK read at upload time**, not to the
artifact being semantically complete. A producer that wrote a truncated file to
disk and *then* uploaded it gets a sidecar recording the truncated content as
the expected digest, and every downstream check passes — exactly the intended
bytes moved, as far as the transfer layer can tell.

Closing that half is [atomic artifact writes](#atomic-artifact-writes) below.
What the transfer layer buys on top of it is narrower, and still worth having:
a file truncated *while the upload reads it* is caught; any corruption after a
good upload is caught on the next download rather than surfacing as a parser
error; and the failure is attributed to the artifact and its producing key
instead of to whichever consumer opened it.

---

## Atomic artifact writes

Every SDK writer that produces an app artifact writes it **atomically**: the
bytes go to a staging file, are flushed to the filesystem, and are renamed onto
the artifact's path in one step. The artifact's final path therefore either does
not exist or holds a complete file. A partial artifact is *unnameable*.

That matters because a truncated file at an artifact's real name is
indistinguishable from a correct one. It gets carried forward, uploaded, and
integrity-checked against its own truncated bytes, and the failure surfaces much
later inside a consuming app's parser — at a byte offset that is identical on
every retry, which is what makes it look like a consumer bug.

Covered writers: the incremental carry-forward state copy, the incremental
marker, incremental diff metadata, writer chunk output and its statistics
sidecar, and the local `.sha256` sidecar. Downloads get the same treatment:
`download_file` and `download_file_chunked` stage in `.sdk-partial/` and
publish with `os.replace`, so a shared `local_path` never exposes a partial
file to a concurrent reader (CONNECT-1126). Two consequences of the rename
publish: the destination is a fresh inode with mode `0600` on every download
(previously a pre-existing file kept its inode and mode), and a `local_path`
that is a symlink is replaced by a regular file rather than written through. `JsonFileWriter` chunks are the one
exception — successive calls append to the same file, and an append cannot be
staged and renamed without rewriting it — so those get the typed error below
without the atomicity.

Staging lives in a `.sdk-partial/` directory beside the artifact rather than as
a `.tmp` suffix next to it, so it is never picked up by a directory listing, a
directory `FileReference`, or a prefix upload. `safe_list_directory` and
`upload_prefix` read one shared definition of what to skip.

The chunked staging file (`.sdk-partial/{name}.part`) and its resume checkpoint
are deterministic functions of the destination, so `download_file_chunked`
itself holds a per-destination lock for the whole transfer — two concurrent
downloads to one `local_path` serialise no matter which entry point they came
through (`materialize_file_reference`, `download_prefix`, batch, or a direct
call). A queued waiter marks activity progress on a short interval
(`ATLAN_STORAGE_LOCK_WAIT_PROGRESS_SECONDS`) so it is never killed as stalled
behind another activity's multi-GB download.

An app that opens its own file handle bypasses all of this. The guarantee covers
the writers the SDK owns, which is where app artifacts actually come from.

### Running out of disk

A write that fails for lack of space raises `DiskFullError` — a
`ResourceExhaustedError` leaf naming the path, the step, the bytes needed, and
the bytes free. A bare `OSError` is what this replaces: it carries no category,
so it lands in whatever broad `except` is in the call stack and the run reports
some unrelated downstream symptom instead.

**This error is the signal that a deployment needs more ephemeral storage.**
Requests and limits are deployment configuration and are deliberately not
requested from the SDK or from app code — neither can know the number. The error
tells the operator which deployment to raise and roughly by how much.

Before a large write whose size is known up front, the SDK checks free space
first, so a plainly undersized volume fails in seconds with `needs ~N GiB, has
M` rather than corrupting output forty minutes in. The check is strict — free
space is not padded with an invented margin — so it catches the impossible write,
not the marginal one; a marginal write that still runs out is caught during the
write and reported identically.

---

## Backend Selection

The object-store backend is configured via Dapr component YAML at deploy time (see `components/objectstore.yaml` in the repo). No code changes are needed to switch between S3, GCS, Azure Blob, or local filesystem. For local development, the default components target a local filesystem path.

---

## Supported Auth Modes

`create_store_from_binding` translates the Dapr component `spec.metadata` fields into the correct obstore configuration. Supported modes per provider:

### S3 (`bindings.aws.s3` / `bindings.s3`)

| Mode | Required fields |
|------|----------------|
| Static access key | `accessKey` + `secretKey` (+ optional `sessionToken` for temporary/STS-derived base creds) |
| AssumeRole via STS | `assumeRoleArn` (+ optional `sessionName`, `accessKey`/`secretKey`/`sessionToken` for base identity) |
| Instance profile / IRSA / env vars | Omit all credential fields |

`boto3` and `azure-identity` are core SDK dependencies — no extra install is required for these auth modes. The `[iam_auth]` and `[azure]` extras are backwards-compatibility shims kept so connector `pyproject.toml` files that listed them continue to install without error.

### Azure Blob (`bindings.azure.blobstorage`)

Priority order when multiple modes are present: account key > SAS token > certificate > service principal > workload identity > managed identity.

| Mode | Required fields |
|------|----------------|
| Account key | `accountKey` |
| SAS token | `sasToken` or `sasKey` |
| Certificate-based service principal | `azureTenantId` + `azureClientId` + (`azureCertificateFile` or `azureCertificate`) |
| Service principal (client secret) | `azureTenantId` + `azureClientId` + `azureClientSecret` |
| AKS Workload Identity | `azureTenantId` + `azureClientId` (no secret; AAD webhook injects `AZURE_FEDERATED_TOKEN_FILE`) |
| User-assigned managed identity | `azureClientId` only |
| System-assigned MI / DefaultAzureCredential | Omit all credential fields |

Use `azureEnvironment` to target sovereign clouds (`AzurePublicCloud`, `AzureChinaCloud`, `AzureUSGovernmentCloud`, `AzureGermanCloud`).

### GCS (`bindings.gcp.bucket` / `bindings.gcs`)

| Mode | Required fields |
|------|----------------|
| Inline service-account key | Any SA JSON field that includes `private_key` or `private_key_id` |
| ADC / Workload Identity / metadata server | `bucket` + `project_id` only (no `private_key`) |

---

## Required Cloud Permissions

The SDK uses a fixed set of object-level operations — no bucket creation, versioning, lifecycle, or presigned-URL generation. The tables below list the minimum permissions the access identity must hold on the target bucket or container.

### S3

Apply object-level actions to `arn:aws:s3:::BUCKET/*` and the list action to `arn:aws:s3:::BUCKET` in separate IAM statement entries.

| IAM action | Operations covered |
|---|---|
| `s3:GetObject` | GetObject (full and byte-range), HeadObject |
| `s3:PutObject` | PutObject, CreateMultipartUpload, UploadPart, CompleteMultipartUpload |
| `s3:AbortMultipartUpload` | Abort in-flight multipart upload on error (required for streaming write error paths) |
| `s3:DeleteObject` | DeleteObject and DeleteObjects (bulk batch — same IAM action) |
| `s3:ListBucket` | ListObjectsV2 — bucket-level permission, not object-level |

Minimal policy skeleton:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::YOUR-BUCKET"
    }
  ]
}
```

**AssumeRole**: when using `assumeRoleArn`, the caller identity additionally needs `sts:AssumeRole` on the target role ARN. The role itself holds the bucket policy above.

### GCS

| IAM permission | Operations covered |
|---|---|
| `storage.objects.get` | GetObject (full and byte-range), object metadata / HeadObject equivalent |
| `storage.objects.create` | PutObject, resumable / streaming write |
| `storage.objects.delete` | DeleteObject (GCS has no native bulk-delete API — `delete_prefix` issues parallel single-object deletes) |
| `storage.objects.list` | ListObjects |

No `storage.buckets.*` permissions are needed. The smallest predefined role that covers all four is **`roles/storage.objectAdmin`** scoped to the bucket. Alternatively, create a custom role with exactly these four permissions.

### Azure Blob Storage / ADLS Gen2

**RBAC** — assign at container or storage-account scope:

| RBAC action | Operations covered |
|---|---|
| `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read` | GetBlob, GetBlobProperties (HEAD), ListBlobs, byte-range GET |
| `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write` | PutBlob, PutBlock + PutBlockList (streaming / block write) |
| `Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete` | DeleteBlob, BlobBatch delete (bulk — up to 256 keys per request) |

The smallest predefined role covering all three is **`Storage Blob Data Contributor`**. `Storage Blob Data Owner` is a superset and is only needed if you additionally require POSIX ACL management on ADLS Gen2.

**SAS token** — minimum permissions at container scope: `r` (read) + `w` (write) + `d` (delete) + `l` (list), i.e. a container-scoped SAS with **`rwdl`**.

**ADLS Gen2 with POSIX ACLs**: if the storage account has hierarchical namespace enabled and you use ACL-based access control instead of RBAC, the principal needs Execute (`X`) on every parent directory and Read / Write / Delete on the objects in scope. RBAC (`Storage Blob Data Contributor`) is simpler and is recommended unless you have a specific ACL requirement.

### What you do not need

These are commonly over-provisioned by accident:

- S3: any `s3:*Bucket*` action beyond `s3:ListBucket` (no lifecycle, versioning, ACL, tagging, or CORS operations)
- GCS: any `storage.buckets.*` permission
- Azure: `Microsoft.Storage/storageAccounts/blobServices/containers/write` (container creation)
