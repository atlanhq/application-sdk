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
| `ATLAN_OBSTORE_READ_TIMEOUT` | `90s` | Progress-based liveness bound — fail only if no bytes arrive for this long |
| `ATLAN_OBSTORE_TIMEOUT` | `30m` | Overall per-request wall-clock backstop |
| `ATLAN_STORAGE_RESUME_DOWNLOADS` | `true` | Resume interrupted chunked downloads from their checkpoint sidecar |
| `ATLAN_STORAGE_PROGRESS_LOG_INTERVAL_SECONDS` | `30` | Heartbeat log interval during long transfers (`0` disables) |

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
