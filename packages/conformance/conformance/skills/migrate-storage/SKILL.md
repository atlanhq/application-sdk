---
name: migrate-storage
description: >
  Move a connector app onto the SDK storage seam: every task-to-task hand-off
  travels as a FileReference field on a typed contract, the interceptor
  persists and materialises it, and only run() (or an @entrypoint) delivers
  files outward with App.upload / App.upload_refs. Clears the storage-seam
  rules P008 (transfer inside a @task), P009 (hand-built object stores),
  P010 (SDK-managed FileReference fields set by hand), P011 (bytes on a
  contract), P012 (a path string on a contract) and P044 (upload_prefix /
  download_prefix), and performs the structural storage migrations other
  skills route here: ParquetFileReader, ParquetFileWriter, JsonFileWriter,
  upload_to_atlan and private storage helpers such as _download_files. The
  done-bar is delivery parity: the object-store keys the app publishes for
  downstream apps are the same before and after, and the transformed entities
  are the same multiset. Key layout, tiers, empty-output semantics, file
  formats and entrypoint-contract changes stop for an owner decision.
runs_before: [migrate-deprecated-symbols]
mandatory_triggers:
  - "/migrate-storage"
  - "migrate storage seam"
  - "FileReference migration"
  - "replace ParquetFileWriter"
optional_triggers:
  - "output_path contract field"
  - "upload_prefix replacement"
  - "RollingFileWriter migration"
owner: connector-platform-team
last_updated: "2026-10-06"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - typed task contracts that carry FileReference fields instead of path strings or bytes
  - tasks that write a per-task scratch directory and return a FileReference; no transfer calls inside a @task
  - run() / @entrypoint delivering outbound files with App.upload or App.upload_refs at the same keys as before
  - RollingFileWriter or local writes in place of the deprecated storage-format readers and writers
  - pyproject.toml and uv.lock (SDK raised to >= 3.33.2 only when App.upload_refs is adopted and the lock is below it)
  - contract_schema.lock.json entries marked sunset for retired entrypoint fields; updated tests
---

# Migrate to the storage seam (P008, P009, P010, P011, P012, P044)

## Conformance rules this skill clears

P008, P009, P010, P011, P012, P044. These rules name this skill as their
`remediation_reference`, and `/remediate` hands their findings here. When the
skill is done, run
`atlan-application-sdk-conformance detect --rule P008,P009,P010,P011,P012,P044`
and confirm none of these rule ids is still reported. Storage sites routed
here by `migrate-deprecated-symbols` are B001 / B008 findings; re-check those
with `atlan-application-sdk-conformance detect --rule B001,B008`.

## The model

- **Within a run, task to task:** the producing `@task` writes into its own
  scratch directory and returns a `FileReference` in its typed Output. The
  interceptor persists it to the deployment store; the consuming task
  declares a `FileReference` field on its typed Input and the interceptor
  materialises it locally. No task calls a transfer itself.
- **Outbound, to other apps (publish, lineage, query intelligence):** `run()`
  or an `@entrypoint` calls `self.upload(UploadInput(...))` for one file or
  directory, or `self.upload_refs(UploadRefsInput(...))` to deliver many refs
  under one prefix. These write to the upstream store.
- A FileReference that never goes through `App.upload` / `upload_refs` from
  `run()` never reaches the upstream store: the run stays green and publish
  reads an empty prefix. This is the silent failure the proof step guards.

## What each rule fires on

| Rule | Fires on | Stops when |
|---|---|---|
| P008 | `self.upload`, `self.download`, `self.upload_refs` inside an SDK `@task` | the transfer moves to `run()` / `@entrypoint`, and the task returns a FileReference |
| P009 | any `boto3` import; obstore `S3Store` / `GCSStore` / `AzureStore` / `HTTPStore` construction; `create_store_from_binding*` calls | the app uses `self.context.storage` / `self.context.upstream_storage`, `CloudStore(...)`, or `App.upload` / `App.download` |
| P010 | `FileReference(...)` with `storage_path=`, `is_durable=` or `file_count=` | `FileReference.from_local(path, tier=...)` or `FileReference(local_path=..., tier=...)` |
| P011 | a `bytes` / `bytearray` / `memoryview` field on a direct `Input` / `Output` subclass | the field is a FileReference |
| P012 | a `str` field on a direct `Input` / `Output` subclass whose name or description reads as a path (`output_path`, `*_dir`, `*_file`, "path to …") | the field is a FileReference, or it is a store key renamed to `*_prefix` / `*_key` |
| P044 | `upload_prefix` / `download_prefix` from `application_sdk.storage` | FileReference fields between tasks, or `App.upload` / `upload_refs` hoisted to `run()` |

`detect` does not scan `tests/`; test code that builds contracts or calls
these helpers changes with the app and is part of the inventory. Suppression
form, on the line or a comment-only line directly above:
`# conformance: ignore[<ID>] <reason>`.

## After-shape APIs

| API | Import | SDK floor |
|---|---|---|
| `FileReference`, `FileReference.from_local(path, *, tier=StorageTier.TRANSIENT)`, `StorageTier` | `application_sdk.contracts.types` | 3.0.0 |
| `Lazy()` (`Annotated[FileReference \| None, Lazy()]`) for an input read only on some paths | `application_sdk.contracts.types` | 3.6.0 |
| `RollingFileWriter(base_path, extension, flush_fn, *, scoped_subdir_name=None, ...)`; `.append`, `.close`, `.file_reference`, `.chunk_count` | `application_sdk.storage.rolling` | 3.10.0 |
| `UploadInput(local_path=..., ref=..., storage_path=..., storage_subdir=..., tier=...)`, `DownloadInput` | `application_sdk.contracts.storage` (not the legacy `UploadInput` in `application_sdk.templates.contracts`) | 3.0.0; `ref` 3.15.0 |
| `UploadRefsInput(files=[DeclaredFile(ref=..., label=...)], prefix=..., source_prefix=..., tier=...)`, `self.upload_refs`, `self.verify_refs` | `application_sdk.contracts.storage` | 3.33.2 |
| `self.context.storage`, `self.context.upstream_storage` (`self.context` is the `AppContext`, `application_sdk.app.context`) | inside an App | 3.0.0 |
| `get_infrastructure()` (returns `None` when no context is set) | `application_sdk.infrastructure.context` | 3.0.0 |
| `CloudStore(store, provider=...)`, `CloudStore.from_credentials(...)` for an external customer bucket | `application_sdk.storage` | — |

## Reference shapes

A task produces a ref (`atlan-metabase-app` `app/connector.py`):

```python
def _ref(local_path: str) -> FileReference:
    return FileReference(local_path=local_path, tier=StorageTier.RETAINED)

@task(timeout_seconds=600)
async def extract_collections(self, input: FetchInput) -> FetchOutput:
    scratch = task_scratch_dir("extract-collections")
    records = await fetch_collections_summaries(client, scratch)
    out = raw_file(scratch, "collections")
    await self.run_in_thread(write_jsonl, out, records)
    return FetchOutput(typename="collections", record_count=len(records), output_file=_ref(out))
```

`task_scratch_dir` is the app's own helper (a `tempfile.mkdtemp` per task);
every task makes its own directory, so no path is shared through a contract.

A task consumes a ref (the interceptor has already materialised it):

```python
class TransformInput(Input):
    data: FileReference

async def transform(self, input: TransformInput) -> TransformOutput:
    frame = pd.read_parquet(input.data.local_path)
```

Chunked output replaces `ParquetFileWriter` / `JsonFileWriter`
(`atlan-trino-app` `app/artifacts.py`): a `RollingFileWriter` with a
`flush_fn` that writes one chunk file, `await writer.append(batch)` per batch,
`await writer.close()`, then return `writer.file_reference` in the Output.

`run()` delivers outbound refs at a fixed prefix (`atlan-mysql-app`
`app/mysql.py`):

```python
delivered = await self.upload_refs(
    UploadRefsInput(
        files=[DeclaredFile(ref=ref) for ref in result.transformed_files],
        source_prefix=result.transformed_data_prefix,
        prefix=result.transformed_data_prefix,
    )
)
return ExtractionOutput(transformed_data_prefix=delivered.prefix)
```

With `label=` on each `DeclaredFile` instead of `source_prefix`, each file
lands at `<prefix>/<label>` (`atlan-metabase-app`).

Reading another app's cache or an external bucket: `CloudStore(self.context.upstream_storage or self.context.storage, provider="tenant")`
(`atlan-openapi-app` `app/connector.py`), or `CloudStore.from_credentials(...)`
for a customer bucket.

## Step 0 — Preconditions

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first. To
   check, run `detect --rule` with each earlier skill's rule ids (its
   "Conformance rules this skill clears" section); if findings remain, ask the
   developer whether to finish that skill first, because it can change the
   entities this skill compares. An app below SDK 3.20.0 must cross the daft
   cliff first: the legacy readers and writers were daft-backed.
2. Read the SDK version from `uv.lock`. `upload_refs` needs 3.33.2; raise the
   SDK to the latest release only if the lock is below it and the app will
   use `upload_refs`.
3. Record the baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule P008,P009,P010,P011,P012,P044,B001,B008 --exit-zero --output "$TMPDIR/before.sarif"`,
   and the tests: `UV_FROZEN=1 uv sync --all-groups --all-extras` once (frozen,
   so the lock file is not rewritten), then
   `uv run --no-sync pytest tests/unit tests/integration` (a plain `uv run`
   re-syncs the environment and can replace the installed conformance build).
   Tests that already fail do not block this skill and must not get worse.
4. Capture delivery parity, outside the repo, for **every** `@entrypoint`
   that writes under a prefix another app reads:
   - **Run it hermetically.** Use the app's offline workflow test (fake
     clients, recorded or cassette data). Give the test infrastructure a
     **separate** `upstream_storage` from `storage`, so a file that never
     reaches the upstream store shows as a missing key; with one shared store
     the silent failure cannot show. The integration kit has no option for
     this: override its infrastructure fixture in a `conftest.py` outside the
     repo that you point pytest at (a `-p` plugin cannot override a conftest
     fixture), and set `ATLAN_APPLICATION_NAME` before any `application_sdk`
     import so the run paths match production. If the app has no such test for an
     entrypoint, write a scratch test outside the repo, or stop and ask the
     developer.
   - **Published keys.** From a fixture or plugin outside the repo
     (`pytest -p <plugin>`), copy the upstream store's tree after the run, and
     dump the entrypoint Output. Record every key under the prefixes the
     Output hands to other apps (`transformed_data_prefix`, lineage and
     query-intelligence prefixes), relative to each prefix. Compare data keys;
     list sidecar keys (`.sha256`, `statistics/`) separately and ask the
     developer whether they count.
   - **Entities.** Collect the transformed entity files (one JSONL line per
     entity).
   - **An empty upstream baseline is a finding, not a pass.** If an
     entrypoint hands a prefix to other apps but nothing reaches the upstream
     store today, the silent failure already exists: report it at Stop 1.
     "Same key set" after the change proves nothing for that prefix.

## Step 1 — Inventory and classify every site

One row per site (B001 flags an import: list every use of a routed class).

- **hand-off** — a path string, `bytes` field, shared `output_path`, prefix
  read/write or `_download_files` that moves data from one task to another
  within the run. Becomes a FileReference field on the producer's Output and
  the consumer's Input. A shared `output_path` on an `@entrypoint` contract
  is a public-contract change: see **owner decisions**.
- **writer / reader** — `ParquetFileWriter` / `JsonFileWriter` →
  `RollingFileWriter` (or a local write plus `FileReference.from_local` for
  the directory); `ParquetFileReader` / `JsonFileReader` → a FileReference
  input read with `pandas.read_parquet` or an `orjson` line loop.
- **outbound** — an `upload_to_atlan` call, a hand-rolled copy between the
  deployment and upstream stores (`create_store_from_binding*`), an
  `application_sdk.storage` upload (`upload_file`, `upload_file_from_bytes`),
  or an upload of a whole local directory to a prefix. Becomes `App.upload` /
  `upload_refs` in `run()`, delivering **the same keys** as today. An
  existing `self.upload(UploadInput(local_path=<dir>))` in `run()` that only
  works because the legacy writers already put the files at mirrored keys
  must become `upload_refs` in the same change that retires those writers.
- **inbound** — reading another app's output by prefix (for example the
  query-intelligence app's results). Becomes `self.download(DownloadInput(...))`
  in `run()` (it reads `upstream_storage or storage`) or
  `CloudStore(self.context.upstream_storage or self.context.storage, ...)`.
- **move** — a transfer call inside a `@task`: `self.upload` /
  `self.download` / `self.upload_refs` (P008) and the `application_sdk.storage`
  helpers `upload_file` / `upload_file_from_bytes`, which P008 does not see.
  The task returns a FileReference; the transfer moves to `run()`. A
  helper write that lands in the **deployment** store today lands in the
  **upstream** store once it goes through `App.upload` / `upload_refs`: that
  adds keys downstream apps can see, so it is an owner decision.
- **store** — a hand-built store (P009). Inside the app:
  `self.context.storage` / `upstream_storage` or `App.download`. An external
  customer bucket: `CloudStore.from_credentials`. A `boto3` import used only
  for authentication (STS, IAM) is not storage: ignore it with that reason.
- **field** — a hand-set `storage_path` / `is_durable` / `file_count` (P010).
  `FileReference.from_local(path, tier=...)` computes them. A `storage_path`
  that pins a key another app reads is an **outbound** site, not a field fix.
- **key** — a `str` field that holds an object-store key or prefix, not a
  local path (P012 false positive by name). Rename to `*_prefix` / `*_key`, or
  ignore with that reason; never convert a store key to a FileReference.

Private storage helpers routed here by `migrate-deprecated-symbols` (B008):
`_download_files` (a **hand-off** or **inbound** site),
`storage.ops._resolve_store` (a **store** site), and the private
`execution._temporal.activity_utils.get_object_store_prefix` /
`build_output_path` (a **hand-off** site once the shared path goes). Until
then, import `get_object_store_prefix` from the public
`application_sdk.execution`. Do not replace `build_output_path` with a path
composed from `input.workflow_id` alone when the path feeds a published key:
that drops the run id from the key; keep it and record the call. Private
non-storage names such as `_HTTP_POOL_LIMITS` stay with that skill.

**Owner decisions** — record each; do not apply without an answer:

1. **Key layout.** Interceptor-persisted refs land under a
   `file_refs/{uuid}` key, never at the key mirrored from the local path:
   `TRANSIENT` under `file_refs/` (within the run prefix in a workflow),
   `RETAINED` under the run prefix, `PERSISTENT` under
   `persistent-artifacts/apps/{app}/file_refs/`. Everything read by prefix
   outside this run (system apps, runbooks) must keep today's layout: deliver
   it with `upload_refs` so the destination keys equal the Step 0 list: each
   declared ref (a file or a whole directory) lands at `{prefix}/{label}`
   when its `DeclaredFile` has a `label`, otherwise at `{prefix}/{leaf}`,
   where *leaf* is the ref's own key with `source_prefix` removed. A ref that
   yields neither is an error.
2. **Tier per artefact.** `TRANSIENT` refs are deleted at run end;
   `RETAINED` survive the run; `PERSISTENT` are kept indefinitely. Legacy
   writer output survived the run.
3. **Empty output.** `upload_refs` returns `prefix=""` for an empty
   declaration and raises on a declared file with no objects; some apps hand
   publish a prefix naming an empty tree today. An empty or missing prefix
   can be read as "the source has no assets" downstream, which can delete
   published assets. Confirm with each downstream consumer, per entrypoint.
4. **Bytes and format.** The legacy `JsonFileWriter` writes orjson over
   `DataFrame.to_dict` records, names chunks `chunk-<n>-part<m>.json`, splits
   a chunk into more parts near the message-size limit, and adds a
   `statistics/` sidecar. A `RollingFileWriter` replacement changes the chunk
   names and drops the sidecar unless the flush function reproduces them;
   write the same bytes and names, or get the developer's yes. Parquet to
   JSONL changes nullable integer typing.
5. **Entrypoint contract.** Retiring or retyping a field the app declares on
   an `@entrypoint` Input / Output: mark it `sunset` in
   `contract_schema.lock.json`; a FileReference on an entrypoint contract needs
   an `artifactSchemas` entry in the pkl contract. A field inherited from an
   SDK contract (for example `ExtractionInput.output_path`): stop reading it;
   do not sunset it. `@task` contracts are free to change.
6. **Retries.** Legacy writers with `replace_prefix=True` wiped a prior
   attempt's prefix; per-task scratch directories do not. Confirm no consumer
   depends on that overwrite.

**Stop 1** (see Agent protocol).

## Step 2 — Apply, in this order

1. Contracts: add the FileReference fields; keep the old fields until their
   producers and consumers are migrated, then remove them (or mark them
   `sunset` on an entrypoint contract).
2. Producers: per-task scratch directories; `RollingFileWriter` or local
   writes; return the FileReference.
3. Consumers: read `input.<field>.local_path`; remove prefix reads,
   `_download_files` and the legacy readers.
4. `run()` / `@entrypoint`: deliver outbound files with `App.upload` /
   `upload_refs` at the agreed keys; remove hand-rolled store copies and
   `upload_to_atlan`. If `upload_refs` verification counts more objects than
   were declared for a **directory** ref (the interceptor adds `.sha256`
   sidecars to it), declare a copy of the ref with `local_path=None` and
   `auto_materialize=False`, and record it: this is an SDK defect, not an app
   one.
5. Stores and fields: `self.context.*`, `CloudStore`,
   `FileReference.from_local`; the agreed ignores and renames.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule P008,P009,P010,P011,P012,P044 --exit-zero --output <file>`
   and `--rule B001,B008` — nothing left from this skill's inventory except
   the agreed ignores and the owner-decision sites.
2. The tests pass, with the Step 0.3 command, and no failure that was
   not in the baseline.
3. Delivery parity: re-run the Step 0.4 capture with the same two stores. The published keys relative
   to each prefix are the same set (missing keys are the silent-failure
   case), and the `(typeName, qualifiedName)` pairs of the transformed
   entities are the same multiset. Any other difference is a defect unless
   the developer accepted it at Stop 1.

**Stop 2** (see Agent protocol).

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site and its class, the Step 0
   key list, and the owner decisions with a recommendation for each. No
   edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   and the key and entity parity diff. Then hand off for PR review.
