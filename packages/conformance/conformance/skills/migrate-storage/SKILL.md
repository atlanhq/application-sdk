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
  are the same multiset. Decided defaults cover scratch directories, tiers,
  internal key layout, delivery and entrypoint-field retirement; a change to
  a key read outside the run, the delivered file set, file formats,
  cross-run state or an entrypoint key name stops for an owner decision.
runs_before: [migrate-orchestration, migrate-deprecated-symbols]
routes_to: [migrate-deprecated-symbols]
also_clears: [ParquetFileReader, ParquetFileWriter, JsonFileReader, JsonFileWriter, upload_to_atlan, _download_files, _resolve_store]
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
last_updated: "2026-10-26"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - typed task contracts that carry FileReference fields instead of path strings or bytes
  - tasks that write a per-task scratch directory and return a FileReference; no transfer calls inside a @task
  - run() / @entrypoint delivering outbound files with App.upload or App.upload_refs at the same keys as before
  - RollingFileWriter or local writes in place of the deprecated storage-format readers and writers
  - pyproject.toml and uv.lock (SDK raised to >= 3.33.2 only when App.upload_refs is adopted and the lock is below it)
  - contract_schema.lock.json entries marked deprecated for entrypoint fields being retired; updated tests
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
This example is raw extract output, kept `RETAINED`; for a ref that
`upload_refs` re-delivers, see owner decision 2.

A task consumes a ref (the interceptor has already materialised it):

```python
class TransformInput(Input):
    data: FileReference

@task(timeout_seconds=600)
async def transform(self, input: TransformInput) -> TransformOutput:
    frame = await self.run_in_thread(pd.read_parquet, input.data.local_path)
```

A blocking read runs through `run_in_thread`; a bare `pd.read_parquet` in an
async task is a P023 finding.

Chunked output replaces `ParquetFileWriter` / `JsonFileWriter` with the SDK's
`RollingFileWriter` (none of the reference apps writes chunks; this is the
SDK API itself):

```python
from application_sdk.storage.rolling import RollingFileWriter

def flush_jsonl(batches: list[list[bytes]], path: str) -> None:
    with open(path, "wb") as handle:
        for batch in batches:
            for record in batch:
                handle.write(record + b"\n")

writer: RollingFileWriter[list[bytes]] = RollingFileWriter(
    base_path=scratch, extension=".jsonl", flush_fn=flush_jsonl, scoped_subdir_name=typename
)
for batch in batches:
    await writer.append(batch)
await writer.close()
return ExtractOutput(data=writer.file_reference)
```

The SDK's guide "Replacing ParquetFileWriter / JsonFileWriter" in
[`docs/agents/coding-standards.md`](https://github.com/atlanhq/application-sdk/blob/main/docs/agents/coding-standards.md)
(in the `atlanhq/application-sdk` repo, not shipped with this package) covers
the options in full.

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

Reading data another app or another workflow node wrote (a prefix handed in
on the entrypoint Input, the connection cache): download it in the
`@entrypoint` and hand the ref to the task (`atlan-metabase-app`
`app/connector.py`, `extract_lineage`):

```python
qi_records = None
if input.view_lineage_input_prefix:
    dl = await self.download(
        DownloadInput(storage_path=input.view_lineage_input_prefix)
    )
    qi_records = dl.ref
build = await self.build_lineage_records(BuildLineageInput(qi_records=qi_records, ...))
```

`App.download` reads `self.context.upstream_storage or self.context.storage`.
A helper that needs a store handle for the same data uses that same
expression; it never builds a store binding. A customer bucket:
`CloudStore.from_credentials(...)` (`atlan-openapi-app` `app/connector.py`);
the app's own tenant store: `CloudStore(self.context.storage, provider="tenant")`
(same file).

## Step 0 — Preconditions

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first. To
   check, run `detect --rule` with each earlier skill's rule ids (its
   "Conformance rules this skill clears" section); if findings remain, ask the
   developer whether to finish that skill first, because it can change the
   entities this skill compares. An app below SDK 3.20.0 must cross the daft
   cliff first: the legacy readers and writers were daft-backed.
2. Read the SDK version from `uv.lock`. `upload_refs` needs 3.33.2. Only if
   the lock is below it and the app will use `upload_refs`, raise the SDK to
   the newest release that is at least 7 days old and at or above that floor
   (dependency cooldown; never a release younger than 7 days unless it fixes
   a known vulnerability). List releases with dates:
   `curl -s https://pypi.org/pypi/atlan-application-sdk/json | jq -r '.releases | to_entries[] | select((.value | length) > 0 and (.value[0].yanked | not)) | "\(.key) \(.value[0].upload_time)"'`
   (yanked releases are skipped).
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
The sites routed here are B001, B008 and P005 findings that name another
skill; do not wait for them to be handed over. Search the app and `tests/`
for every symbol in this skill's `also_clears` frontmatter list
(`grep -rnwE 'ParquetFileReader|ParquetFileWriter|...' app tests`) and add
each use to the inventory.

- **hand-off** — a path string, `bytes` field, shared `output_path`, prefix
  read/write or `_download_files` that moves data from one task to another
  within the run. Becomes a FileReference field on the producer's Output and
  the consumer's Input. A shared `output_path` on an `@entrypoint` contract
  is a public-contract change: see **owner decisions**. A value formatted as
  an object-store key or prefix is still a hand-off when a task in this run
  reads it: one task uploads under it and a later task downloads it, or the
  tasks use it as a directory. Classify by who reads the value, not by how
  it is spelled or what a comment calls it. When the key comes from another
  workflow node (an `@entrypoint` Input field), the `@entrypoint` downloads
  it and the task gets the ref (see **inbound**).
- **scratch** — an `output_path` / `output_dir` on a `@task` Input that only
  says where the task writes; the data moves on as FileReferences or not at
  all. Drop the field; the task makes its own directory.
- **dead** — a path field nothing reads. Search `app/`, `tests/`, the
  generated manifests (`app/generated/**/manifest.json`, which read Output
  fields by JSONPath) and `contract/app.pkl` for the field name, and check
  that no SDK base class or mixin the contract extends reads it by name.
  On a `@task` contract, delete it (or the whole unused class); on an
  `@entrypoint` contract, owner decision 5.
- **writer / reader** — `ParquetFileWriter` / `JsonFileWriter` →
  `RollingFileWriter` (or a local write plus `FileReference.from_local` for
  the directory); `ParquetFileReader` / `JsonFileReader` → a FileReference
  input read with `pandas.read_parquet` or an `orjson` line loop, through
  `run_in_thread`. `JsonFileReader` coerced values through pandas (an int
  column with a missing key → float, the missing key → `NaN`); the `orjson`
  loop returns the values in the file. Do not rebuild a DataFrame to keep
  the coercion; fix the callers that relied on it: `row.get(key)` for an
  optional key (`row[key]` raises `KeyError`), `is None` / `key not in row`
  for `pd.isna` / `math.isnan` checks. A value formatted into a published
  key or qualified name changes (`"3.0"` → `"3"`): record it under
  **owner decisions**.
- **outbound** — an `upload_to_atlan` call, a hand-rolled copy between the
  deployment and upstream stores (`create_store_from_binding*`), an
  `application_sdk.storage` upload (`upload_file`, `upload_file_from_bytes`),
  or an upload of a whole local directory to a prefix. Becomes `App.upload` /
  `upload_refs` in `run()`, delivering **the same keys** as today. An
  existing `self.upload(UploadInput(local_path=<dir>))` in `run()` that only
  works because the legacy writers already put the files at mirrored keys
  must become `upload_refs` in the same change that retires those writers.
  A delete of that local folder in `run()` after the upload (P021) goes in
  the same change: the default `on_complete` cleanup removes the files of the
  refs on the worker that tracked them (an app that overrides `on_complete`
  must call `super()`). It does not reach files written on another worker:
  that case is owner decision 8 in `migrate-orchestration`.
  A copy that runs only when `ENABLE_ATLAN_UPLOAD` is set worked because the
  files already sat at their canonical keys (a pinned `storage_path`, or a
  legacy writer's mirrored key). A ref from `FileReference.from_local` lands
  under `file_refs/{uuid}` instead, so its
  `upload_refs` call runs in every deployment. It writes to
  `upstream_storage or storage`: Atlan's bucket in SDR, and in-cluster the
  one store that both names point at, which is where publish reads.
- **inbound** — reading another app's output by prefix (for example the
  query-intelligence app's results), the connection cache, or a
  `FileReference(storage_path=..., is_durable=True)` built by hand to name
  data already in the store. Becomes `self.download(DownloadInput(...))` in
  `run()` / the `@entrypoint`, with `dl.ref` handed to the tasks that read it
  (`None` when `dl.ref.file_count` is 0). A helper that needs the store
  handle itself uses `self.context.upstream_storage or self.context.storage`.
  An input that is optional today (the run goes on without it) stays
  optional: see the decided defaults.
- **wrapper** — an app helper around `upload_prefix` / `download_prefix`
  (`upload_if_dapr`, `download_if_dapr`, `_sync_input_dir`, …). Classify each
  caller (hand-off, outbound, inbound), migrate the callers, then delete the
  helper. A helper with many callers across tasks is one design change;
  plan it as such or stop with that reason.
- **cross-run state** — a prefix synced so the next run can read it
  (current-state snapshots, incremental markers, resume checkpoints). No
  contract boundary carries it, so neither a FileReference nor `upload_refs`
  applies as is. Owner decision 7.
- **move** — a transfer call inside a `@task`: `self.upload` /
  `self.download` / `self.upload_refs` (P008) and the `application_sdk.storage`
  helpers `upload_file` / `upload_file_from_bytes`, which P008 does not see.
  The task returns a FileReference; the transfer moves to `run()`. A
  helper write that lands in the **deployment** store today lands in the
  **upstream** store once it goes through `App.upload` / `upload_refs`: that
  adds keys downstream apps can see, so it is an owner decision.
- **store** — a hand-built store (P009). Inside the app:
  `self.context.storage` / `upstream_storage` or `App.download`. An external
  customer bucket: `CloudStore.from_credentials`; keep the old failure on an
  empty prefix (list first when the old code failed on it). A `boto3` import
  used only for authentication (STS, IAM, an RDS token) is not storage:
  ignore it with that reason. A `boto3` client for an AWS service that is the
  metadata source (Glue, Athena, SageMaker, …) has no documented SDK seam
  (`application_sdk.common.aws_utils` builds plain clients but is not in the
  SDK capability manifest and takes no client `Config`): stop and record it.
- **field** — a hand-set `storage_path` / `is_durable` / `file_count` (P010).
  `FileReference.from_local(path, tier=...)` computes them. A ref that pins
  `storage_path` to a key mirrored from its local path, often after the task
  uploaded the file itself, becomes `from_local` and the task's own upload
  goes; the interceptor persists it under `file_refs/`. A reader that loads
  that key by name reads the ref instead. A pinned key that something
  outside this run reads (publish, another app, the DAG) is an **outbound**
  site: deliver it with `upload_refs` at the same key.
- **key** — a `str` field on an `@entrypoint` Output (or Input) that holds an
  object-store key or prefix that is read outside this run: by the generated
  manifest's JSONPath for the next workflow node, or by another app (P012
  false positive by name). Never convert it to a FileReference. Renaming it
  changes the DAG wire contract: owner decision 8. A key only tasks of this
  run read is a **hand-off**, not a key.

Private storage helpers routed here by `migrate-deprecated-symbols` (B008):
`_download_files` (a **hand-off** or **inbound** site),
and `storage.ops._resolve_store` (a **store** site). The private
`execution._temporal.activity_utils.get_object_store_prefix` /
`build_output_path` imports are a `migrate-deprecated-symbols` swap; this
skill only removes their calls when the shared path they build goes. Do not
replace `build_output_path` with a path that drops the run id when the path
feeds a published key: that changes the key; keep it and record the call. A
run-scoped path built in `run()` takes its ids from the App context:
`self.context.workflow_id` and `self.run_id`, always set there. Not
`input.workflow_id`: only the SDK's own dispatch (`handler.service` and the
Temporal executor backend) fills it, so a run started any other way gets an
empty id. Private
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
2. **Tier per artefact.** Cleanup runs after `run()` returns, on success and
   on failure (when cleanup is enabled, the default), and deletes `TRANSIENT`
   refs; `RETAINED` refs survive the run; `PERSISTENT` are kept indefinitely.
   Legacy writer output survived the run. `upload_refs` writes its own copies,
   `RETAINED` by default, and downstream reads those: a ref it re-delivers
   keeps the default tier, `TRANSIENT`, because a `RETAINED` one only keeps a
   second copy of the same bytes. Keep a ref `RETAINED` when its own key is
   read after the run, or when the file must be reviewable after the run.
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
   an `@entrypoint` Input / Output: stop reading it and mark it `deprecated`
   (in Python source `Field(..., deprecated=True)`; for a contract generated
   from `app.pkl`, in the Pkl widget definition, then regenerate), and
   regenerate the ledger (status `deprecated`; the field stays on the
   contract). Once that has merged, mark it `sunset` the same way in a later
   change (in Python source `Field(..., deprecated=True,
   json_schema_extra={"x-lifecycle": "sunset"})`; keep `deprecated=True`).
   Once the ledger records `sunset`, delete the field from the source; the
   ledger keeps its entry. `ledger-guard` blocks `active` → `sunset` in one
   change. P012 keeps reporting a path field while it is on the contract. A
   FileReference on an entrypoint contract needs an `artifactSchemas` entry in
   the pkl contract. A field inherited from an SDK contract (for example
   `ExtractionInput.output_path`): stop reading it; do not retire it. `@task`
   contracts are free to change.
6. **Retries.** Legacy writers with `replace_prefix=True` wiped a prior
   attempt's prefix; per-task scratch directories do not. Confirm no consumer
   depends on that overwrite.
7. **Cross-run state.** A prefix the next run reads back (a cross-run
   **state** site) either stays a whole-prefix sync with a reasoned
   `# conformance: ignore[P044] state synced across runs` (P044's own text
   allows this for a state directory synced wholesale), or moves to
   `App.upload` / `App.download` in `run()` at the same stable key. Either
   way the key must not move: the next run, on the old or new version,
   reads it.
8. **Entrypoint key.** A **key** field that the generated manifest or
   another app reads either keeps its name with a reasoned
   `# conformance: ignore[P012] object-store key read by <reader>`, or gets
   a `*_prefix` / `*_key` twin that the manifest switches to, with the old
   name retired as in decision 5.

Decisions 1, 2, 3, 5 and 6 have answers that apply to every app; they are in
**Decided defaults** below. A site that needs a different answer (a key
outside the run would move, the delivered file set would change) still
stops here.

**Stop 1** (see Agent protocol).

## Step 2 — Apply, in this order

1. Contracts: add the FileReference fields; keep the old fields until their
   producers and consumers are migrated, then remove them (on an entrypoint
   contract, mark them `deprecated` and remove them in a later change).
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

## Decided defaults

Owner answers for storage sites, read by remediation tooling. Each row names
a Step 1 class as its `fix_shape`. A default keeps what the app does today
(a run that goes on without an input keeps going on; a file delivered today
is delivered at the same key).

```decided-defaults
{"rule": "P012", "fix_shape": "task-scratch", "decision": "Drop the path field from the @task contract and stop passing it; the task writes into its own tempfile.mkdtemp() directory and returns what it wrote as FileReferences. A retry gets a new directory; the consumer only sees the ref the successful attempt returned.", "why": "Owner decision 6: nothing reads the shared path, so its overwrite on retry is not needed. Reference shape: atlan-metabase-app app/paths.py task_scratch_dir."}
{"rule": "P012", "fix_shape": "task-hand-off", "decision": "The producer writes into its own scratch directory and returns FileReference.from_local(<file or dir>, tier=StorageTier.RETAINED) on a typed Output field, or None when it wrote nothing; the consumer declares FileReference | None on its Input and reads .local_path. Delete the path or key field and the transfer calls that moved the data. A key handed in from another workflow node is downloaded in the @entrypoint (inbound-download).", "why": "Owner decision 2: hand-off output was legacy writer output that survived the run, so RETAINED keeps that. Reference shape: atlan-openapi-app app/contracts.py, atlan-metabase-app app/connector.py."}
{"rule": "P044", "fix_shape": "task-hand-off", "decision": "Same as P012 task-hand-off: the producing task returns FileReference.from_local(<dir>, tier=StorageTier.RETAINED) (None when empty), the consuming task declares it on its Input; a reader of a prefix several tasks wrote declares a bounded collection of refs, Annotated[dict[str, FileReference], MaxItems(n)]. Delete the upload_prefix / download_prefix calls and any wrapper left with no caller.", "why": "The interceptor persists and materialises the ref with per-file checksums, which the prefix copy did not have. Reference shape: atlan-metabase-app app/contracts.py."}
{"rule": "P012", "fix_shape": "dead-field", "decision": "Delete the field (or the class when nothing uses it) and every place that sets it, once the Step 1 search (app, tests, generated manifests, contract/app.pkl, SDK base classes) finds no reader. On an @entrypoint contract use entrypoint-field-retire instead.", "why": "A field nothing reads carries no behaviour; @task contracts are free to change."}
{"rule": "P012", "fix_shape": "entrypoint-field-retire", "decision": "In this change stop reading the field and mark it deprecated (Field(..., deprecated=True), or the Pkl widget then regenerate) and regenerate the ledger. A later change marks it sunset; a third deletes it. The finding stays open until the delete; say which step this is in the PR description.", "why": "Owner decision 5. An old payload that still sends the key is accepted: Input drops an unknown key with a warning."}
{"rule": "P010", "fix_shape": "from-local", "decision": "FileReference.from_local(<local_path>, tier=<the tier the call passed>) in place of FileReference(local_path=..., file_count=..., tier=...); keep the None guards around it. Only for a call that sets no auto_materialize and no storage_path other than None.", "why": "The interceptor counts the files again when it persists a local ref, so the hand-set file_count never reaches a consumer."}
{"rule": "P010", "fix_shape": "unpin-key", "decision": "FileReference.from_local(<local path>, tier=<the tier it had>) and delete the task's own upload of that file; a reader that loaded the pinned key by name gets the ref on its Input instead. A pinned key that something outside the run reads stays: deliver that file from run() with outbound-upload-refs at the same key.", "why": "Owner decision 1: refs read only inside the run may live under file_refs/ keys; keys read outside the run do not move."}
{"rule": "P010", "fix_shape": "inbound-download", "decision": "self.download(DownloadInput(storage_path=<the key or prefix>)) in run() / the @entrypoint, and pass dl.ref (None when dl.ref.file_count is 0) to each task that reads it. If the run goes on without this data today, keep that: catch TemporalActivityError around the download and continue with None only when error.cause.type names a storage or OS-level error (StorageError and its subclasses, OSError and its subclasses, ConnectionError, TimeoutError); re-raise anything else. If the run fails without it today, let the error raise.", "why": "App.download reads upstream_storage or storage and retries with the SDK transfer policy; the task gets a ref that survives a move to another pod. Reference shape: atlan-metabase-app app/connector.py extract_lineage."}
{"rule": "P044", "fix_shape": "inbound-download", "decision": "Same as P010 inbound-download: hoist the download_prefix of another app's or another node's output to self.download(...) in run() / the @entrypoint and hand dl.ref to the task; keep today's fail-soft or fail-hard behaviour as described there.", "why": "A download inside a @task trades P044 for P008. Reference shape: atlan-metabase-app app/connector.py extract_lineage."}
{"rule": "P009", "fix_shape": "inbound-download", "decision": "Same as P010 inbound-download when the hand-built store only reads a prefix for a task: remove the store and the task that used it, download in run() / the @entrypoint, hand dl.ref on.", "why": "App.download already routes to upstream_storage or storage."}
{"rule": "P009", "fix_shape": "store-fallback", "decision": "self.context.upstream_storage or self.context.storage in place of create_store_from_binding* / a hand-built store in a helper that reads or writes another app's data; drop test settings that only fed the hand-built store.", "why": "The same store App.download uses: in-cluster both names are one store, in SDR it is the upstream store, so keys and bucket do not change."}
{"rule": "P009", "fix_shape": "outbound-upload-refs", "decision": "Replace the hand-written copy between the deployment and upstream stores with self.upload_refs(UploadRefsInput(files=[DeclaredFile(ref=ref, label=<the file name delivered today>)], prefix=<the prefix delivered today>)) from run(), called per entity type right after its transform so earlier results still land if a later step fails. Declare exactly the files today's code delivers. Run it in every deployment, not only when ENABLE_ATLAN_UPLOAD is set. Refs it re-delivers are TRANSIENT.", "why": "Owner decisions 1-3: destination keys equal today's keys; upload_refs writes to upstream_storage or storage, which in-cluster is the same store. Reference shape: atlan-mysql-app app/mysql.py run(), atlan-metabase-app app/connector.py."}
{"rule": "P044", "fix_shape": "outbound-upload-refs", "decision": "Same as P009 outbound-upload-refs for an upload_prefix of transformed output that publish or another app reads: return the refs from the task, deliver from run() with upload_refs at today's prefix and file names.", "why": "An upload inside a @task trades P044 for P008. Reference shape: atlan-mysql-app app/mysql.py run()."}
{"rule": "P009", "fix_shape": "customer-bucket", "decision": "CloudStore.from_credentials(<the same bucket, region and auth the client used>); call store.list(prefix) first when the old code failed on an empty prefix; then store.download(prefix=..., output_dir=<the same local layout>). Needs SDK 3.49.1 or later; with a lower lock, plan the site as SDK.", "why": "CloudStore is the SDK API for a customer bucket; 3.49.1 skips S3 folder-marker keys and resolves a blank region as boto3 did. Reference shape: atlan-openapi-app app/connector.py."}
{"rule": "P009", "fix_shape": "auth-only-ignore", "decision": "# conformance: ignore[P009] boto3 for authentication only (STS / IAM / RDS token), not object storage, on the import line, after checking that no client in the file is s3 or another storage service.", "why": "Step 1 store class: authentication is not storage."}
```

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site and its class, the Step 0
   key list, and the owner decisions with a recommendation for each. No
   edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   and the key and entity parity diff. Then hand off for PR review.
