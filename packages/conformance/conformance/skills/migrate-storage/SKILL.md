---
name: migrate-storage
description: >
  Move a connector app onto the SDK storage seam without changing what it
  delivers: task-to-task hand-offs travel as FileReference fields on typed
  contracts, the interceptor persists and materialises them, and only run()
  (or an @entrypoint) delivers files outward with App.upload / upload_refs.
  Clears P008, P009, P010, P011, P012 and P044, and the storage readers,
  writers and helpers other skills route here (ParquetFileReader,
  ParquetFileWriter, JsonFileReader, JsonFileWriter, upload_to_atlan,
  _download_files, _resolve_store). The done-bar is delivery parity: the keys
  other apps read and the transformed entities are the same before and
  after. A change to a key read outside the run, the delivered file set, file
  formats, an entrypoint field or state a later run reads is held for its
  owner.
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
last_updated: "2026-11-25"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - typed task contracts that carry FileReference fields instead of path strings or bytes
  - tasks that write a per-task scratch directory and return a FileReference; no transfer calls inside a @task
  - run() / @entrypoint delivering outbound files with App.upload or App.upload_refs at the same keys as before
  - RollingFileWriter or local writes in place of the deprecated storage-format readers and writers
  - pyproject.toml and uv.lock (SDK raised to 3.33.2 or later only when App.upload_refs is adopted and the lock is below it)
  - contract_schema.lock.json entries marked deprecated for entrypoint fields being retired; updated tests
---

# Migrate to the storage seam (P008, P009, P010, P011, P012, P044)

## What this protects

1. **Delivery.** Other apps (publish, lineage, query intelligence) read what
   this app delivers by key. The keys, the file set and the bytes under each
   prefix another app reads stay the same. A FileReference that never goes
   through `App.upload` / `upload_refs` from `run()` never reaches the
   upstream store: the run stays green and publish reads an empty prefix,
   which downstream can read as "the source has no assets" and delete
   published assets. This silent failure is what the proof guards.
2. **Hand-offs travel as FileReferences.** Within a run, a task writes into
   its own scratch directory and returns a FileReference on its typed
   Output; the next task declares it on its typed Input. The interceptor
   persists and materialises the ref with per-file checksums, so a retry or
   another worker gets the same files. A task never transfers files itself.
3. **What is read outside the run is a public contract.** An `@entrypoint`
   Input / Output field, a key the generated manifest reads by JSONPath, a
   prefix another app reads: its name and value stay. Retiring a field goes
   through the contract lifecycle; a rename is the owner's call.
4. **Behaviour at the edges stays.** A run that goes on without an input
   today keeps going on; one that fails keeps failing. Empty output, retries,
   value types and file names a reader matches stay as they are.
5. **State a later run reads belongs to its owner.** A prefix a later run or
   a retry reads back is a design choice, not a storage fix; only the
   incremental marker at the SDK's own key has a decided answer.
6. **When 1 to 5 cannot be proven, hold the site and say what is missing.**
   Your plan text is what the person reads. A site whose shape this skill
   does not describe gets the same treatment.

The rule definitions own what "correct" is: each finding carries its rule's
`full_description` and `terminal_state`. This skill owns how to reach it
without breaking 1 to 5, and how to prove it.

## Facts you need

- After-shape APIs:

  | API | Import | SDK floor |
  |---|---|---|
  | `FileReference`, `FileReference.from_local(path, *, tier=StorageTier.TRANSIENT)`, `StorageTier` | `application_sdk.contracts.types` | 3.0.0 |
  | `Lazy()` (`Annotated[FileReference \| None, Lazy()]`) for an input read only on some paths | `application_sdk.contracts.types` | 3.6.0 |
  | `RollingFileWriter(base_path, extension, flush_fn, *, scoped_subdir_name=None, ...)`; `.append`, `.close`, `.file_reference`, `.chunk_count` | `application_sdk.storage.rolling` | 3.10.0 |
  | `UploadInput(local_path=..., ref=..., storage_path=..., storage_subdir=..., tier=...)`, `DownloadInput` | `application_sdk.contracts.storage` (not the legacy `UploadInput` in `application_sdk.templates.contracts`) | 3.0.0; `ref` 3.15.0 |
  | `UploadRefsInput(files=[DeclaredFile(ref=..., label=...)], prefix=..., source_prefix=..., tier=...)`, `self.upload_refs`, `self.verify_refs` | `application_sdk.contracts.storage` | 3.33.2 |
  | `self.context.storage`, `self.context.upstream_storage` (`self.context` is the `AppContext`) | inside an App | 3.0.0 |
  | `get_infrastructure()` (returns `None` when no context is set) | `application_sdk.infrastructure.context` | 3.0.0 |
  | `CloudStore(store, provider=...)`, `CloudStore.from_credentials(...)` for an external customer bucket | `application_sdk.storage` | — |

- **Key layout.** An interceptor-persisted ref lands under a
  `file_refs/{uuid}` key, never at the key mirrored from its local path:
  `TRANSIENT` under `file_refs/` (within the run prefix in a workflow),
  `RETAINED` under the run prefix, `PERSISTENT` under
  `persistent-artifacts/apps/{app}/file_refs/`. `upload_refs` writes each
  declared ref at `{prefix}/{label}` when its `DeclaredFile` has a `label`,
  otherwise at `{prefix}/{leaf}` (the ref's key with `source_prefix`
  removed); a ref that yields neither is an error. It writes to
  `upstream_storage or storage`: Atlan's bucket in SDR, and in-cluster the
  one store both names point at, which is where publish reads. It adds a
  `.sha256` sidecar next to each file.
- **Tiers.** Cleanup runs after `run()` returns, on success and on failure
  (when enabled, the default), and deletes `TRANSIENT` refs; `RETAINED`
  refs survive the run; `PERSISTENT` refs are kept. `upload_refs` writes its
  own copies, `RETAINED` by default. The default `on_complete` cleanup
  removes the files of the refs the worker tracked (an override must call
  `super()`); it does not reach files written on another worker.
- **Empty output.** `upload_refs` returns `prefix=""` for an empty
  declaration and raises on a declared file with no objects.
- **Downloads.** `App.download` reads `self.context.upstream_storage or
  self.context.storage`. A prefix needs one trailing `/`: without it an
  empty prefix reads as a missing single object and raises, where
  `download_prefix` returned nothing. A download error crosses the activity
  boundary as its class name string (`error.cause.type`), never a parent.
- **Legacy writers.** `JsonFileWriter` writes orjson over `DataFrame.to_dict`
  records, names chunks `chunk-<n>-part<m>.json`, splits a chunk near the
  message-size limit, and adds a `statistics/` sidecar; every legacy writer
  uploaded each chunk as it wrote it, and with `replace_prefix=True` wiped a
  prior attempt's prefix. `RollingFileWriter` names chunks `chunk-<n>` and
  writes no sidecar unless the flush function does.
- **Legacy readers.** `JsonFileReader` coerced values through pandas (a
  missing key → `NaN`, an int column with gaps → float); an `orjson` line
  loop returns the values in the file. `ParquetFileReader.read_batches`
  equals `pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same
  size>)` with each batch converted on its own; one `pandas.read_parquet` of
  a whole file can type a column differently.
- **Entrypoint fields.** A field is retired in three changes: stop reading
  it and mark it `deprecated` (`Field(..., deprecated=True)`, or the Pkl
  widget, then regenerate the ledger); later mark it `sunset`
  (`json_schema_extra={"x-lifecycle": "sunset"}`, `deprecated=True` kept);
  then delete it. `ledger-guard` blocks `active` → `sunset` in one change, and
  P012 keeps reporting the field while it is on the contract. A FileReference
  on an entrypoint contract needs an `artifactSchemas` entry in the Pkl
  contract. A field inherited from an SDK contract
  (`ExtractionInput.output_path`) is only no longer read, never retired.
- **Directory refs.** When `upload_refs` verification counts more objects
  than were declared for a directory ref (the interceptor added `.sha256`
  sidecars), declare a copy of the ref with `local_path=None` and
  `auto_materialize=False`, and record it as an SDK defect.
- **Run ids.** In `run()`, `self.context.workflow_id` and `self.run_id` are
  always set; `input.workflow_id` is filled only by the SDK's own dispatch.
- `ENABLE_ATLAN_UPLOAD`-only copies worked because files sat at canonical
  keys already (a pinned `storage_path` or a legacy writer's mirrored key);
  a `from_local` ref does not, so its delivery runs in every deployment.
- `storage.ops._resolve_store(None)` used only to ask whether an object
  store is configured equals `infra = get_infrastructure()`, then
  `infra is not None and infra.storage is not None`.
- A blocking read in an async task runs through `run_in_thread`; a bare
  `pd.read_parquet` there is a P023 finding.
- Find SDK versions at the release tag, never from memory; raise the SDK
  through the app's normal dependency update.

## The rules

| Rule | Fires on | End state |
|---|---|---|
| P008 | `self.upload` / `self.download` / `self.upload_refs` inside a `@task` | the task returns a FileReference; the transfer is in `run()` / `@entrypoint` (principle 2) |
| P009 | `boto3` imports, obstore store construction, `create_store_from_binding*` | `self.context.storage` / `upstream_storage`, `App.download`, or `CloudStore` for a customer bucket; `boto3` used only for authentication is not storage |
| P010 | `FileReference(...)` with `storage_path=`, `is_durable=` or `file_count=` | `FileReference.from_local`; a pinned key read outside the run is delivered with `upload_refs` at that key (principle 1) |
| P011 | a `bytes` field on a direct `Input` / `Output` | a FileReference |
| P012 | a `str` field that reads as a path | a FileReference hand-off; a field only a task writes into dropped; a key read outside the run keeps its name with a reasoned ignore (principle 3) |
| P044 | `upload_prefix` / `download_prefix` | FileReference hand-offs, or `App.upload` / `upload_refs` / `App.download` in `run()` |
| B001 / B008 routed here | `ParquetFileReader`, `ParquetFileWriter`, `JsonFileReader`, `JsonFileWriter`, `upload_to_atlan`, `_download_files`, `_resolve_store`, in any form | the same end states: writers return refs, readers read refs, delivery in `run()` |

Classify a value by who reads it, never by its name: a key a later task of
this run reads is a hand-off; a key the manifest or another app reads is
public (principle 3); a prefix a later run reads is state (principle 5). A
helper that wraps the prefix calls is migrated through its callers, then
deleted. `detect` does not scan `tests/`; search `app` and `tests` for every
symbol in `also_clears` too. Suppression form, on the line or a comment-only
line directly above: `# conformance: ignore[<ID>] <reason>`.

The private `get_object_store_prefix` / `build_output_path` imports and
private non-storage names such as `_HTTP_POOL_LIMITS` stay with
`migrate-deprecated-symbols`: this skill removes such a call only when the
shared path it builds goes. A `build_output_path` path that feeds a published
key keeps its run id; dropping it changes the key (principle 3).

## Before you change anything

1. Run the skills before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first. An app
   below SDK 3.20.0 must cross the daft cliff first (`migrate-off-daft`).
2. Baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule P008,P009,P010,P011,P012,P044 --exit-zero --output "$TMPDIR/before.sarif"`
   (with B001 / B008 sites routed here, also
   `atlan-application-sdk-conformance detect --rule B001,B008 --exit-zero --output "$TMPDIR/before-routed.sarif"`),
   and the tests (`UV_FROZEN=1 uv sync --all-groups --all-extras`, then
   `uv run --no-sync pytest tests/unit tests/integration`). Record the tests
   that already fail.
3. Capture delivery for every `@entrypoint` that writes under a prefix
   another app reads, from the app's offline workflow test, with a
   **separate** `upstream_storage` and `storage` (with one shared store the
   silent failure cannot show; override the kit's infrastructure fixture in a
   `conftest.py` outside the repo, and set `ATLAN_APPLICATION_NAME` before any
   `application_sdk` import). Record every key under each prefix the Output
   hands on, relative to that prefix (sidecars listed apart), and the
   transformed entities. An empty upstream baseline for a prefix that is
   handed on is a finding, not a pass. No such test: write one outside the
   repo, or hold.

## Prove it

1. Re-detect with the baseline command: only held sites and reasoned
   ignores remain.
2. Tests: no failure that was not in the baseline.
3. Delivery: re-run the capture. The keys relative to each prefix are the
   same set (a missing key is the silent failure), and the
   `(typeName, qualifiedName)` pairs of the transformed entities are the same
   multiset. Any other difference is a defect unless a decided default allows
   it.

## PR description

- the sites changed and the end state applied;
- every key, file name, tier or behaviour difference and why it is allowed;
- every site held: what is missing, and who must add or decide it;
- whether delivery parity is proven or not.

## Working with a developer, and in the remediation lane

With a developer: stop after the inventory (before any edit), and after the
proof. In the remediation lane the decided defaults below are the
developer's answers; a held site exits with its plan text as the brief.

## Reference shapes

A task produces a ref (`atlan-metabase-app` `app/connector.py`):

```python
@task(timeout_seconds=600)
async def extract_collections(self, input: FetchInput) -> FetchOutput:
    scratch = task_scratch_dir("extract-collections")
    records = await fetch_collections_summaries(client, scratch)
    out = raw_file(scratch, "collections")
    await self.run_in_thread(write_jsonl, out, records)
    return FetchOutput(typename="collections", record_count=len(records), output_file=_ref(out))
```

A task consumes a ref the interceptor already materialised:

```python
class TransformInput(Input):
    data: FileReference

@task(timeout_seconds=600)
async def transform(self, input: TransformInput) -> TransformOutput:
    frame = await self.run_in_thread(pd.read_parquet, input.data.local_path)
```

Chunked output with the SDK's `RollingFileWriter` (the SDK API itself; the
SDK guide "Replacing ParquetFileWriter / JsonFileWriter" in
`docs/agents/coding-standards.md` of the `atlanhq/application-sdk` repo
covers the options):

```python
writer: RollingFileWriter[list[bytes]] = RollingFileWriter(
    base_path=scratch, extension=".jsonl", flush_fn=flush_jsonl, scoped_subdir_name=typename
)
for batch in batches:
    await writer.append(batch)
await writer.close()
return ExtractOutput(data=writer.file_reference)
```

`run()` delivers outbound refs at a fixed prefix (`atlan-mysql-app`
`app/mysql.py`; with `label=` per `DeclaredFile` instead, as in
`atlan-metabase-app`):

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

Data another node wrote is downloaded in the `@entrypoint` and its ref handed
on (`atlan-metabase-app` `app/connector.py` `extract_lineage`, with this
skill's trailing-slash and `None`-when-empty rules):

```python
qi_records = None
if input.view_lineage_input_prefix:
    dl = await self.download(
        DownloadInput(storage_path=input.view_lineage_input_prefix.rstrip("/") + "/")
    )
    qi_records = dl.ref if dl.ref.file_count else None
```

A customer bucket: `CloudStore.from_credentials(...)`; the tenant store:
`CloudStore(self.context.storage, provider="tenant")` (`atlan-openapi-app`
`app/connector.py`).

## Decided defaults

Owner answers read by remediation tooling. A default keeps what the app does
today: a run that goes on without an input keeps going on, and a file
delivered today is delivered at the same key. A row with `block` holds a
site for a person: `match` is a pattern on the finding message or source
line, `fix_shape` a pattern on the planned shape. Where a row's `why` names
an owner decision by number, it is the principle above: 1 key layout and
delivery (principle 1), 2 tiers, 3 empty output, 5 entrypoint contract and 8
entrypoint key (principle 3), 6 retries (principle 4), 7 cross-run state
(principle 5).

```decided-defaults
{"rule": "P012", "fix_shape": "task-scratch", "decision": "Drop the path field from the @task contract and stop passing it; the task writes into its own tempfile.mkdtemp() directory and returns what it wrote as FileReferences. A retry gets a new directory; the consumer only sees the ref the successful attempt returned.", "why": "Owner decision 6: nothing reads the shared path, so its overwrite on retry is not needed. Reference shape: atlan-metabase-app app/paths.py task_scratch_dir."}
{"rule": "P012", "fix_shape": "task-hand-off", "decision": "The producer writes into its own scratch directory and returns FileReference.from_local(<file or dir>, tier=StorageTier.RETAINED) on a typed Output field, or None when it wrote nothing; when upload_refs also re-delivers that ref, keep the from_local default TRANSIENT instead. The consumer declares FileReference | None on its Input and reads .local_path. On a @task contract delete the path or key field and the transfer calls that moved the data; on an @entrypoint contract retire the old field with entrypoint-field-retire. A key handed in from another workflow node is downloaded in the @entrypoint (inbound-download).", "why": "Owner decision 2: hand-off output was legacy writer output that survived the run, so RETAINED keeps that. Reference shape: atlan-openapi-app app/contracts.py, atlan-metabase-app app/connector.py."}
{"rule": "P044", "fix_shape": "task-hand-off", "decision": "Same as P012 task-hand-off, tier rule included: the producing task returns FileReference.from_local(<dir>, tier=StorageTier.RETAINED) (TRANSIENT when upload_refs re-delivers it; None when empty), the consuming task declares it on its Input; a reader of a prefix several tasks wrote declares a bounded collection of refs, Annotated[dict[str, FileReference], MaxItems(n)]. Delete the upload_prefix / download_prefix calls and any wrapper left with no caller.", "why": "The interceptor persists and materialises the ref with per-file checksums, which the prefix copy did not have. Reference shape: atlan-metabase-app app/contracts.py."}
{"rule": "P012", "fix_shape": "dead-field", "decision": "Delete the field (or the class when nothing uses it) and every place that sets it, once the Step 1 search (app, tests, generated manifests, contract/app.pkl, SDK base classes) finds no reader. On an @entrypoint contract use entrypoint-field-retire instead.", "why": "A field nothing reads carries no behaviour; @task contracts are free to change."}
{"rule": "P012", "fix_shape": "entrypoint-field-retire", "decision": "In this change stop reading the field and mark it deprecated (Field(..., deprecated=True), or the Pkl widget then regenerate) and regenerate the ledger. A later change marks it sunset; a third deletes it. The finding stays open until the delete; say which step this is in the PR description. Never for a field the generated manifest or another app reads: that field is a key and keeps its value.", "why": "Owner decision 5. An old payload that still sends the key is accepted: Input drops an unknown key with a warning."}
{"rule": "P010", "fix_shape": "from-local", "decision": "FileReference.from_local(<local_path>, tier=<the tier the call passed>) in place of FileReference(local_path=..., file_count=..., tier=...); keep the None guards around it and change nothing else. Only for a call that sets local_path and, besides tier, only file_count (and maybe is_durable=False / storage_path=None): no auto_materialize, manifest_path or store.", "why": "The interceptor counts the files again when it persists a local ref, so the hand-set file_count never reaches a consumer."}
{"rule": "P010", "fix_shape": "unpin-key", "decision": "FileReference.from_local(<local path>, tier=<the tier it had>) and delete the task's own upload of that file; a reader that loaded the pinned key by name gets the ref on its Input instead. A pinned key that something outside the run reads stays: deliver that file from run() with outbound-upload-refs at the same key.", "why": "Owner decision 1: refs read only inside the run may live under file_refs/ keys; keys read outside the run do not move."}
{"rule": "P010", "fix_shape": "inbound-download", "decision": "self.download(DownloadInput(storage_path=<the prefix with one trailing slash: prefix.rstrip('/') + '/'>)) in run() / the @entrypoint, and pass dl.ref (None when dl.ref.file_count is 0) to each task that reads it; a single object keeps its exact key. Without the trailing slash an empty prefix is read as a missing single object and raises. If the run goes on without this data today, keep that: catch TemporalActivityError around the download and continue with None only when isinstance(error.cause, TemporalApplicationError) (import it from application_sdk.execution; its ApplicationError is the subclass for raising and never matches a failure read back) and error.cause.type is in a fixed set of leaf class names. error.cause.type is the class name of the exception the download raised, never a parent, so build the set from the old except clause by replacing each caught class with its own name plus the names of all its subclasses (for a storage error, the subclasses defined in application_sdk.storage.errors; for OSError, the built-in OSError subclasses); StorageNotFoundError must be in it. Write the set as a literal in the app. Re-raise anything else. If the run fails without it today, let the error raise.", "why": "App.download reads upstream_storage or storage and retries with the SDK transfer policy; the task gets a ref that survives a move to another pod. The download in the @entrypoint and the ref handed to the task are the atlan-metabase-app app/connector.py extract_lineage shape; the None-on-empty rule and the fail-soft catch are this skill's rules for keeping today's behaviour (the reference download has neither). The error type crosses the activity boundary as its class name string."}
{"rule": "P044", "fix_shape": "inbound-download", "decision": "Same as P010 inbound-download: hoist the download_prefix of another app's or another node's output to self.download(...) in run() / the @entrypoint and hand dl.ref to the task; keep today's fail-soft or fail-hard behaviour as described there.", "why": "A download inside a @task trades P044 for P008. Reference shape: atlan-metabase-app app/connector.py extract_lineage."}
{"rule": "P009", "fix_shape": "inbound-download", "decision": "Same as P010 inbound-download when the hand-built store only reads a prefix for a task: remove the store and the task that used it, download in run() / the @entrypoint, hand dl.ref on.", "why": "App.download already routes to upstream_storage or storage."}
{"rule": "P009", "fix_shape": "store-fallback", "decision": "For a hand-built store that only reads, and that was bound to the upstream store: self.context.upstream_storage or self.context.storage. For one bound to the deployment store: self.context.storage. Drop test settings that only fed the hand-built store. A store the app writes through is a move site: owner decision.", "why": "The same store App.download uses for upstream data, and the app's own store for its own data, so keys and bucket do not change in either deployment mode."}
{"rule": "P009", "fix_shape": "outbound-upload-refs", "decision": "Replace the hand-written copy between the deployment and upstream stores with one self.upload_refs(UploadRefsInput(...)) call in run() after the transforms, over every transformed ref the tasks returned: either files=[DeclaredFile(ref=ref)] with source_prefix=prefix=<the prefix delivered today> when the ref keys already end in today's relative names, or files=[DeclaredFile(ref=ref, label=<today's relative name>)] with prefix=<the prefix delivered today>. Hand delivered.prefix downstream. Declare exactly the files today's code delivers; if any declaration can be empty, stop: owner decision 3. Run it in every deployment, not only when ENABLE_ATLAN_UPLOAD is set; the only new keys are the .sha256 sidecars upload_refs writes next to each file, as in the reference apps. The task refs keep the from_local default TRANSIENT; leave UploadRefsInput.tier at its default RETAINED.", "why": "Owner decisions 1-3: destination keys equal today's keys; upload_refs writes to upstream_storage or storage, which in-cluster is the same store, and verifies the delivery against the declaration. Reference shape: atlan-mysql-app app/mysql.py run() (source_prefix), atlan-metabase-app app/connector.py (label)."}
{"rule": "P044", "fix_shape": "outbound-upload-refs", "decision": "Same as P009 outbound-upload-refs for an upload_prefix of transformed output that publish or another app reads: return the refs from the task, deliver them with one upload_refs call in run() at today's prefix and relative names, hand delivered.prefix downstream, and stop on a declaration that can be empty (owner decision 3).", "why": "An upload inside a @task trades P044 for P008. Reference shape: atlan-mysql-app app/mysql.py run()."}
{"rule": "P009", "fix_shape": "customer-bucket", "decision": "CloudStore.from_credentials(<the same bucket, region and auth the client used>); call store.list(prefix) first when the old code failed on an empty prefix; then store.download(prefix=..., output_dir=<the same local layout>). Needs SDK 3.49.1 or later; with a lower lock, plan the site as SDK.", "why": "CloudStore is the SDK API for a customer bucket; it skips S3 folder-marker keys, and from 3.49.1 it resolves a blank region as boto3 did. Reference shape: atlan-openapi-app app/connector.py."}
{"rule": "P009", "fix_shape": "auth-only-ignore", "decision": "# conformance: ignore[P009] boto3 for authentication only, not object storage (<the one in use: STS, IAM or RDS token>), on the import line, after checking that every boto3 client in the file is STS, IAM or an RDS token; any other service client is the Step 1 store-class stop.", "why": "Step 1 store class: authentication is not storage.", "suppress": true, "reason": "authentication only, not object storage"}
{"rule": "P012", "fix_shape": "entry-key-ignore", "decision": "# conformance: ignore[P012] object-store key, not a local path, read by <the manifest node or app that reads it>, on the field line, for a str field on an @entrypoint Output whose value is an object-store key or prefix read outside this run, or an @entrypoint Input field that receives one (its read becomes inbound-download). Only when no code in the app uses the value as a local path (os.path.join with a local root, open, makedirs). A field used both ways: move the in-run use to a FileReference hand-off between tasks, keep the field and its value for the manifest, then add this ignore. Never retire a field the manifest or another app reads.", "why": "Owner decision 8: P012 matches the field name or its description; the value already survives the move to another node, and a rename changes only the name.", "suppress": true, "reason": "object-store key, not a local path"}
{"rule": "P012", "fix_shape": "not-path-ignore", "decision": "# conformance: ignore[P012] <what the value is>, not a path, on the field line, for a str field whose value is not a path at all (a format flag, a list of names, a display name).", "why": "P012 is a name heuristic; its rule text names a reasoned ignore for a field that is not a transferable path.", "suppress": true, "reason": "not a path"}
{"rule": "P044", "fix_shape": "incremental-marker", "decision": "Replace the prefix transfer of the incremental marker with fetch_marker(connection_qualified_name, application_name) where the app reads the marker today and persist_marker(connection_qualified_name, marker_value, application_name) where it saves it today, from application_sdk.common.incremental; read MarkerPair.marker where the code read the old value. Save the value the app saves today, at the point it saves it today; fetch_marker takes next_marker at the moment it is called, so use MarkerPair.next_marker only where the app took the current time at that same point (run start, before any work the scan covers); where the app captured its value earlier and read the marker later, keep the app's own captured value. Only when today's key is get_persistent_s3_prefix(connection_qualified_name, application_name)/marker.txt, the value is a timestamp in YYYY-MM-DDTHH:MM:SSZ, and the prefix call moves that file and nothing else; otherwise the site is cross-run-state. Keep today's failure behaviour: fetch_marker raises StorageError when the read fails for any reason other than a missing marker, and persist_marker raises MarkerUploadError (application_sdk.common.incremental.incremental_errors) on a failed save; where the app caught either failure today and went on (for example with a full run), catch that error and do the same.", "why": "Owner decision 7: fetch_marker and persist_marker are the SDK seam for the incremental marker and read and write the same key, so the next run finds the marker this run saved. The get_persistent_s3_prefix folder also holds current-state/, so replacing a call that moved more than the marker would stop moving those files; a different key would make the first run after the change start from zero; fetch_marker only strips fractional seconds before the Z and converts no other format, so a value in another format would reach the extraction unconverted."}
{"rule": "P044", "fix_shape": "^cross-run-state$", "block": "The site syncs a prefix so that a later run, or a retry, can read it back (a current-state snapshot, another state folder, a resume checkpoint, a marker at a key or in a format the SDK marker functions do not use, or a prefix call that moves the marker with other files). Each SDK seam changes or moves what is stored: self.upload / self.download with StorageTier.PERSISTENT moves the transfer out of the task, and the current-state store stamps file names, writes a manifest and prunes. Owner decision 7.", "brief": "Keep this state where it is with self.upload / self.download (StorageTier.PERSISTENT) at the same key, move it to the current-state store (new file names), or keep the prefix call with # conformance: ignore[P044] state synced across runs?"}
{"rule": "P009", "fix_shape": "^boto3-source-api$", "block": "boto3 builds a client for an AWS service that is the app's metadata source, not object storage. The SDK has no documented seam for such a client: application_sdk.common.aws_utils is not in the SDK capability manifest and takes no client Config.", "brief": "Should application_sdk.common.aws_utils become the public seam for source clients (with client Config), or should P009 stop firing on AWS clients that are not storage?"}
{"rule": "P009", "fix_shape": "^dev-script$", "block": "The boto3 import is in a developer script under scripts/ (fixture seeding, replay capture), not in app runtime code.", "brief": "Should conformance scan scripts/ for P009, or should these developer tools take a reasoned ignore?"}
{"rule": "P009", "fix_shape": "^customer-bucket$", "requires": {"package": "atlan-application-sdk", "min": "3.49.1"}, "block": "CloudStore.from_credentials resolves a blank region as boto3 did only from atlan-application-sdk 3.49.1, and the app's lock resolves an older SDK.", "brief": "Raise the app's atlan-application-sdk lock to 3.49.1 or later through its normal dependency update, then re-run this site."}
{"rule": "B001", "fix_shape": "writer-rolling", "decision": "Replace ParquetFileWriter / JsonFileWriter with RollingFileWriter and a flush_fn that writes one chunk per batch, or with a local write, in the task's own scratch directory; return FileReference.from_local(<dir>, tier=StorageTier.RETAINED) on the task Output. Keep the counts the task returned (count the rows appended; RollingFileWriter.chunk_count). RollingFileWriter chunk names and no statistics sidecar are kept only where no reader matches file names; where one does, write the old names with a local write. Files a reader outside the run reads are delivered from run() with upload_refs at the same prefix.", "why": "The deprecated writers wrote and uploaded each chunk; the interceptor now persists the returned ref, and the explicit delivery keeps the same keys for outside readers."}
{"rule": "B001", "fix_shape": "reader-file-reference", "decision": "The reading task declares the producer's FileReference on its typed Input and reads the chunk files from .local_path through run_in_thread the way the deprecated reader did: for parquet read_batches, pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same size>) with each batch converted to pandas on its own; for parquet read(), one pyarrow table of all files converted once; for JSON, an orjson line loop. Data already in the store before this run is downloaded in run() and its ref handed to the task. A reader that caught the missing-prefix error today keeps that behaviour for a missing ref.", "why": "The interceptor materialises the ref before the task runs; the old reader downloaded the prefix itself."}
{"rule": "B008", "fix_shape": "reader-file-reference", "decision": "For a _download_files use: The reading task declares the producer's FileReference on its typed Input and reads the chunk files from .local_path through run_in_thread the way the deprecated reader did: for parquet read_batches, pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same size>) with each batch converted to pandas on its own; for parquet read(), one pyarrow table of all files converted once; for JSON, an orjson line loop. Data already in the store before this run is downloaded in run() and its ref handed to the task. A reader that caught the missing-prefix error today keeps that behaviour for a missing ref.", "why": "The interceptor materialises the ref before the task runs; the old reader downloaded the prefix itself."}
{"rule": "P008", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P009", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P010", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P011", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P012", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P044", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "B001", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "B008", "fix_shape": "^hold$", "block": "Held: this site's delivery, a contract read outside the run, behaviour at the edges or state a later run reads cannot be proven unchanged. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
```
