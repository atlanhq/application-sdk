---
name: migrate-deprecated-symbols
description: >
  Move a connector app off deprecated SDK symbols (B001) and off private
  modules and names it does not own (B008). B001 reads the deprecation
  manifest the SDK ships (generated from the @deprecated markers in SDK
  source), so the list of what to migrate is never hard-coded here: the
  finding message carries the SDK's own migration notice. The skill
  inventories every site and sorts it into a class: a straight swap (same
  behaviour, new name or import path; some marked safe by the detector), a
  base or wrapper to drop, a storage reader or writer to move to
  FileReferences, an identity or error-class change, or an owner decision
  (a live transformer, a SQL extractor, PreflightStatus.PARTIAL, a private
  name with no public equivalent). It applies every class but the owner
  decisions, and proves the result with re-detection and the test suite. Run it
  after any skill that raises the SDK, because a newer SDK can deprecate more
  symbols.
routes_to: [migrate-storage]
also_clears: [get_object_store_prefix, build_output_path, _HTTP_POOL_LIMITS, _HTTP_POOL_TIMEOUT_SECONDS, _current_outputs, _app_error_to_http_status]
mandatory_triggers:
  - "/migrate-deprecated-symbols"
  - "deprecated SDK symbol"
  - "B001 deprecated"
  - "B008 private import"
optional_triggers:
  - "remove private SDK imports"
  - "fix deprecation warnings from application_sdk"
owner: connector-platform-team
last_updated: "2026-10-28"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code and tests importing public SDK and third-party names only
  - app code using the replacement each deprecation notice names, for the straight swaps
  - justified ignores for private names with no public equivalent, each naming a tracking id
  - a routing list of structural sites handed to migrate-storage, or to the developer with upgrade-v3
---

# Migrate deprecated and private symbols (B001, B008)

## Conformance rules this skill clears

B001, B008. These rules name this skill as their `remediation_reference`, and
`/remediate` hands their findings here. When the skill is done, run
`atlan-application-sdk-conformance detect --rule B001,B008` and confirm none
of these rule ids is still reported.

## Why this matters

A deprecated symbol is on a removal path: the next SDK major (or the version
in its notice) removes it, and the app breaks on that bump. A private name
(leading underscore) has no compatibility promise at all: SDK 3.36.0 reshaped
one private module, and every app that imported it stopped collecting tests.

## What each rule fires on

| Rule | Fires on | Stops when |
|---|---|---|
| B001 | a symbol in the SDK's deprecation manifest: `from application_sdk.x import Name`, a subclass of it, a call `x.<method>()`, or an enum member such as `PreflightStatus.PARTIAL` | the site uses the replacement the notice names, or carries a justified ignore |
| B008 | an import of a `_`-prefixed module or name from a package the app does not own (SDK or third-party), or a reach-through `alias._private.x` | the import names a public module and name, or carries a justified ignore |

B001 matches method calls by name only (`x.upload_to_atlan(...)`), so a
call of an unrelated object's method, or of the app's own method of the same
name, can fire it; that is a false positive, ignore it with that reason.
B008 never fires on relative imports,
on names that exist in the repo, or on dunders.

`detect` does not scan `tests/`, but test code breaks on the same SDK
changes. Find those sites yourself and add them to the inventory:
`grep -rnE 'application_sdk[^ ]*\._|from application_sdk[^ ]* import .*\b_[A-Za-z]|PreflightStatus\.PARTIAL|in PreflightStatus|list\(PreflightStatus' tests/`
plus every deprecated symbol the app-code findings named, and every symbol in
this skill's `also_clears` list (other skills route those here, and their
`tests/` sites produce no finding). Iterating the enum
(`for status in PreflightStatus`) still yields PARTIAL. List string patch
targets too (`patch("app.app.download_files")`): they move with the symbol
they name.

## Safe swaps marked by the detector

A finding whose fix is one fixed text change that keeps behaviour carries
`result.properties["atlan/safeSwap"]`:

```json
{"id": "public-reexport", "find": "application_sdk.execution._temporal.activity_utils",
 "replace": "application_sdk.execution",
 "requires": {"package": "atlan-application-sdk", "min": "3.0.0"}}
```

- `public-reexport` (B008): a module-level `from <private> import ...` whose
  names a public module re-exports as the same objects.
- `public-rename` (B001): a call of a deprecated SDK method whose public
  name takes the same arguments, in a repo where no file defines a function
  of the old or the new name and no file under `tests/` names the old one.

Apply it on the finding's line only: `find` occurs there exactly once;
replace it with `replace`. Apply it only when `uv.lock` pins `package` at or
above `requires.min`; otherwise the site is a **swap** that needs the SDK
raised first. Nothing else changes: a module-level import swap leaves every
patch target as it was, and a rename is marked only when no test names the
old method. A
finding without the property is never a safe swap, even when it looks like
one.

Suppression form, on the line or on a comment-only line directly above:
`# conformance: ignore[<ID>] <reason>`.

The deprecation manifest ships in the package:
`$(dirname "$(atlan-application-sdk-conformance programs-dir)")/data/deprecated_symbols.json`.
Each entry has `symbol`, `kind`, `module`, `message` (the migration notice)
and `removal_version`. The replacement exists only in `message`; read it, never
guess it. Two cautions: a notice can cite a doc that is only in the
`atlanhq/application-sdk` repo (for example `docs/agents/coding-standards.md`),
so read it on GitHub; and a notice that points at a private module
(`application_sdk._runtime...`) would create a B008 — treat that site as
**no public equivalent** instead of following it.

## Step 0 — Preconditions

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first, so the
   manifest matches the SDK the app will ship with. If you run this skill
   alone, record that the earlier migrations can still change what is
   deprecated.
2. Record the baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule B001,B008 --exit-zero --output "$TMPDIR/before.sarif"`.
   Run the tests with every group and extra installed
   (`UV_FROZEN=1 uv sync --all-groups --all-extras` once, so the lock file is not
   rewritten, then `uv run --no-sync pytest tests/unit tests/integration`; a plain
   `uv run` re-syncs and can replace the installed conformance build).
   Record the tests that already fail; they do not block this skill, and they
   must not get worse.

## Step 1 — Inventory and classify every site

One row per site: file:line, the symbol, the notice, and one class. B001
flags the **import**; list every use of the imported name too (one row per
use), because each use changes with the migration. Every class below is
fixed in this skill, except the **owner decisions**.

- **safe swap** — the finding carries `atlan/safeSwap` (see "Safe swaps
  marked by the detector"). Apply it as stated; nothing else on the line
  changes.
- **swap** — the replacement has the same behaviour and signature, or the
  public path exports the same object. Known examples:
  - A private `application_sdk.execution._temporal.activity_utils` import of
    `get_object_store_prefix` or `build_output_path` →
    `from application_sdk.execution import get_object_store_prefix` (the same
    function, exported publicly; `atlan-mysql-app` `app/mysql.py:19`). In a
    function-local import keep it local, and move any test
    `patch("application_sdk.execution._temporal.activity_utils.<name>")` to
    `patch("application_sdk.execution.<name>")`: the function now looks the
    name up on the public module when it runs. A module-level import needs no
    test change. Replacing the call with
    FileReference outputs is a storage migration, not part of this swap.
  - `SqlApp._resolve_credential_ref` → `SqlApp.resolve_credential_ref`, and
    `SqlApp._build_transform_input` → `SqlApp.build_transform_input` (same
    signature; the old names delegate; `atlan-mysql-app` `app/mysql.py:521`).
    When the app defines a method of either name, it is an **owner
    decision** (below), not a swap.
  - `DataframeType.daft` → `DataframeType.pandas`.
  - `fetch_marker_from_storage` → `fetch_marker`, `persist_marker_to_storage`
    → `persist_marker` (SDK ≥ 3.41.0): same arguments, but the results are
    dataclasses. Replace a tuple unpack with `pair.marker` /
    `pair.next_marker`, and a dict read of the persist result with
    `result.s3_key` / `result.marker_timestamp`.
  - An SDK private that has a public alias for the same object, for example
    `application_sdk.templates.sql_app._orjson_default` →
    `application_sdk.common.asset_serialization.orjson_default`.
  - Third-party privates with a public home, for example
    a library's private submodule → the public package that re-exports the
    name (check the name imports from it at the locked version), or
    `ipaddress._BaseAddress` / `_BaseNetwork` in an annotation →
    `ipaddress.IPv4Address | ipaddress.IPv6Address` /
    `ipaddress.IPv4Network | ipaddress.IPv6Network`.
  - `storage.ops._resolve_store(None)` used only to ask "is an object store
    configured?" → `infra = get_infrastructure()` (from
    `application_sdk.infrastructure.context`), then
    `configured = infra is not None and infra.storage is not None`. It
    returns `None` when no context is set, so check that first; it replaces
    the try/except around the old call.
  Confirm each one: open the replacement in the installed SDK and check the
  signature before you call it a swap.
- **base** — `class X(BaseMetadataExtractor)` on a source the app does not
  read with SQL through an SDK SQL client → `class X(App)` (`application_sdk.app.App`;
  `atlan-metabase-app` `app/connector.py:218`, `atlan-openapi-app`
  `app/connector.py:394`). The base adds only the deprecated
  `upload_to_atlan` task. First move every call of the SDK's
  `upload_to_atlan` to `self.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED))`
  (`atlan-metabase-app` `app/connector.py:929`); where the code read the old
  result, read the same count from the new `UploadOutput`. A method of that
  name the app defines itself stays: once the base is gone it is the app's
  own task. Update tests that assert the task is inherited. Under PINNED
  worker versioning (`ATLAN_APP_BUILD_ID` set) the change is safe; for
  unversioned deployments or AUTO_UPGRADE, let in-flight runs finish first.
- **transformer wrapper** — a class that subclasses `TransformerInterface`
  (or `QueryBasedTransformer`) but does its own mapping: no call reaches the
  base's `transform_metadata`, no YAML template is read, no `super()` call.
  Drop the base, and delete an override that exists only to satisfy it (one
  that raises "not supported" or returns its input), with the error class
  and the test that only covered that override. An annotation of
  `TransformerInterface` becomes the app's own class.
- **dead** — the flagged class or import has no reader in `app/`, `tests/`,
  generated manifests or `contract/app.pkl`. Delete it.
- **identity** — `get_workflow_id()` → `input.workflow_id` when the task's
  typed `Input` is in scope, else `self.context.workflow_id`;
  `get_workflow_run_id()` → `self.run_id` (`App.run_id`), or
  `self.context.run_id`. These are the same values the private helpers
  return, so keys built from them do not change. `build_output_path()`:
  moving the **import** to the public `application_sdk.execution.build_output_path`
  (the same object) is a **swap**; replacing the **call** changes published
  keys — leave the call.
- **error** — `CredentialVaultError` → `application_sdk.errors.DependencyUnavailableError`
  (its parent class: an `except` / `isinstance` now also matches the
  parent's other subclasses; narrow it if the code must not catch those).
  `HandlerError` → an `application_sdk.errors.AppError` subclass whose
  category keeps the HTTP status the handler returns today; update every
  `except` and `isinstance` that used it.
- **storage** — `ParquetFileReader`, `ParquetFileWriter`, `JsonFileReader`,
  `JsonFileWriter`, their subclasses and annotations, and `_download_files`.
  Fix them here with the **writer / reader** rules of the
  `migrate-storage` skill (`skills/migrate-storage/SKILL.md` in this package,
  Step 1): a writer becomes `RollingFileWriter` with a `flush_fn` (one chunk
  per batch) or a local write, and the task returns
  `FileReference.from_local(<dir>, tier=StorageTier.RETAINED)`; a reader of
  another task's output declares that FileReference on its typed Input and
  reads the chunk files through `run_in_thread` the way the deprecated reader
  did: where the app called `ParquetFileReader.read_batches`, loop over
  `pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same size>)` and
  convert each batch to pandas on its own (one `pandas.read_parquet` of the
  whole file can type a column differently and loses partial progress); where
  it called `read()`, read all files into one pyarrow table and convert once.
  `JsonFileReader` becomes an `orjson` line loop. Keep what the app does today:
  - the deprecated writers uploaded every chunk as they wrote it, so a
    reader in another task or another app found the files in the store;
    hand the ref on, or deliver it from `run()` with `upload_refs` at the
    same prefix;
  - counts the task returned from `close()` (`total_record_count`,
    `chunk_count`) are still returned: count the rows you append, and use
    `RollingFileWriter.chunk_count`;
  - file names a reader matches by name (`chunk-<n>-part<m>`, a
    `<start>_<end>` miner layout, a `chunk_start` offset) are written with
    those names (a local write with the old names), not `chunk-<n>`;
  - a reader that catches the missing-prefix error keeps doing so;
  - a reader that relied on pandas coercion (a missing key → `NaN`, an int
    column with gaps → float) is fixed at its callers, as `migrate-storage`
    says; a value formatted into a published key or qualified name is an
    owner decision.
  A subclass that overrides a private writer hook (`_write_chunk`,
  `_write_dataframe`) becomes a plain `flush_fn` that writes the same bytes.
- **owner decisions** — the replacement changes what the app publishes or
  the task names it runs; the developer decides. Record the site, do not
  edit it:
  - **live transformer** — `QueryBasedTransformer` or `AtlasTransformer`
    whose base `transform_metadata` (YAML templates, entity classes) builds
    the published assets → the asset-mapper pattern (typed records →
    `map_<entity>()` → pyatlan asset; `atlan-metabase-app`
    `app/asset_mapper.py`, `atlan-mysql-app` `app/mysql.py`). The published
    attributes change unless proven equal per asset type.
  - **SQL extractor** — `SqlMetadataExtractor`,
    `IncrementalSqlMetadataExtractor`, or `BaseMetadataExtractor` on a source
    read with SQL → `templates.SqlApp` (`atlan-mysql-app` `app/mysql.py:181`).
    `SqlApp` runs `extract_*` / `transform_*` tasks and writes raw JSONL where
    the old base ran `fetch_*` tasks and wrote parquet, so task names, raw
    layout and the transform change together. Hand it to the developer with
    [`upgrade-v3`](https://github.com/atlanhq/application-sdk/blob/main/.claude/skills/upgrade-v3/SKILL.md)
    (in the `atlanhq/application-sdk` repo, not shipped with this package).
  - **SqlApp override** — the app defines `_resolve_credential_ref` or
    `_build_transform_input` (or the public name) on an `SqlApp` subclass.
    The SDK's `run()` and preflight gate call only the public name, so an
    override on the old name is not reached today; renaming it to the
    public name makes the SDK start calling the app's body.
  - **preflight status** — `PreflightStatus.PARTIAL` → `READY` keeps the
    gate's behaviour (the gate already treats PARTIAL as READY);
    `NOT_READY` blocks the run. Either way the status label in the UI, Pulse
    and the Automation Engine event changes. Change the tests that assert
    PARTIAL and any message map keyed on it in the same step. Keep the
    "readiness is undetermined" message: choose it from the checks (READY
    with a failed advisory check), not from the status alone, or an
    undetermined run shows "all checks passed".
  - **no public equivalent** (below).
- **no public equivalent** — first look for a public surface that answers
  the same question the code asks. Example: a test of how an error maps to an
  HTTP status calls the real test-auth route (`POST /workflows/v1/auth`, no
  Temporal needed) on `create_app_handler_service(handler)` with a test
  client and a handler that raises that `AppError` (a `HandlerError` or a
  failed result skips the mapping), not
  `_app_error_to_http_status`. If none exists: a private name the SDK or
  library does not expose publicly (for example SDK constants such as `_HTTP_POOL_LIMITS`, or
  a patched private method of a third-party library). Two
  options for the developer: for a plain value (a constant), copy it into
  the app under its own name; otherwise ignore it:
  `# conformance: ignore[B008] no public equivalent — tracked in <ticket id>`.
  For an SDK name, the ticket is an issue on `atlanhq/application-sdk` asking
  for a public API; never invent an id. A constant the app rebinds to change
  the SDK's own behaviour is not a plain value: copying it changes nothing
  the SDK reads, so it is this class.
- **false positive** — a B001 method-name match on an object that is not
  the SDK's, for example a call of an `upload_to_atlan` task the app defines
  itself on an `App` subclass or a mixin with no deprecated base above it in
  the class's base order. Ignore it with that reason.

Order the inventory by urgency: an entry whose `removal_version` is at or
below the SDK version locked in `uv.lock` (compare as versions: `4.0` equals
`4.0.0`) can disappear in any release, even if it still exists today. Its
class does not change: an **owner decision** still waits for the developer.

**Stop 1** (see Agent protocol).

## Step 2 — Apply

1. Apply the **swap** sites. Change the import or call only, and keep the
   surrounding logic — except where the old call was wrapped for an
   exception, as with `_resolve_store`; replace that wrapper too, and remove
   imports only the old wrapper used.
2. Apply **base**, **transformer wrapper**, **dead**, **identity** and
   **error** sites, each with the test that covers the changed path. A base
   site goes after the `upload_to_atlan` calls it depends on.
3. Apply **storage** sites per task: producer and consumer of one hand-off
   change in the same edit.
4. Add the agreed ignores for **no public equivalent** and **false
   positive** sites.
5. Leave the **owner decisions** unchanged and list them in the hand-off.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule B001,B008 --exit-zero --output <file>` — no finding
   left except the owner decisions and the agreed ignores.
2. The test suite passes (the Step 0.2 command), with no failure that was not
   in the baseline.
3. A test collects for every changed test module (`pytest --collect-only`):
   B008 changes in `tests/` break collection first.
4. `detect` does not check `tests/`: re-run the Step 1 search and confirm each
   test site is fixed or carries its agreed ignore.

**Stop 2** (see Agent protocol).

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site, its class, the replacement
   checked against the installed SDK, and the owner decisions needed. No
   edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   and the owner-decision sites left open. Then hand off for PR review.

## Decided defaults

Owner answers for the choices a fixer meets in this skill, read by
remediation tooling. A default keeps what the app does today. Each row names
a fix shape for a Step 1 class as its `fix_shape`: `public-reexport` and
`public-rename` (safe swap, swap), `own-method-ignore` (false positive),
`base-to-app` (base),
`transformer-wrapper`, `workflow-identity` (identity), `writer-rolling` and
`reader-file-reference` (storage). A row with `block` is an owner decision:
`match` is a pattern on the finding message or source line, `fix_shape` a
pattern on the planned shape; such a site waits for the person the `brief`
asks.

```decided-defaults
{"rule": "B008", "fix_shape": "public-reexport", "decision": "Import the same names from the public module that re-exports them (application_sdk.execution for get_object_store_prefix and build_output_path). A module-level import stays module level and needs no test change; a function-local import stays local, and a test patch of the private path moves to the public module path (patch(\"application_sdk.execution.<name>\")).", "why": "The public module re-exports the same objects, so behaviour cannot change; a function-local import now looks the name up on the public module when it runs."}
{"rule": "B001", "fix_shape": "public-rename", "decision": "Change the call to the public SqlApp method (resolve_credential_ref, build_transform_input) and move test patches of the old name to the public one. Only when the app defines no method of either name; otherwise the site is the sql-app-override owner decision.", "why": "The deprecated names delegate to the public ones with the same signature, so a call changes nothing."}
{"rule": "B001", "fix_shape": "base-to-app", "decision": "class X(App) in place of class X(BaseMetadataExtractor) when the app does not read the source with SQL through an SDK SQL client. First change every call of the SDK upload_to_atlan task to self.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED)) and read the same count from its UploadOutput; a method of that name the app defines itself stays. Update tests that assert the task is inherited.", "why": "The base adds only the deprecated upload_to_atlan task. Reference shape: atlan-metabase-app app/connector.py MetabaseApp(App) and its self.upload call.", "interface": false}
{"rule": "B001", "fix_shape": "transformer-wrapper", "decision": "Drop the TransformerInterface or QueryBasedTransformer base from a class that does its own mapping (no call reaches the base transform_metadata, no YAML template, no super() call). Delete an override that exists only to satisfy the base, with the error class and the test that only covered it. An annotation of the base becomes the app's own class.", "why": "Nothing the app runs uses the base, so removing it changes no output."}
{"rule": "B008", "fix_shape": "workflow-identity", "decision": "get_workflow_id() becomes input.workflow_id when the task's typed Input is in scope, else self.context.workflow_id; get_workflow_run_id() becomes self.run_id or self.context.run_id. Keep every key, path and log field built from the value.", "why": "App.run_id is the workflow run id and the task context carries the same workflow and run ids the private helpers read, so the values do not change."}
{"rule": "B001", "fix_shape": "writer-rolling", "decision": "Replace ParquetFileWriter / JsonFileWriter with RollingFileWriter and a flush_fn that writes one chunk per batch, or with a local write, in the task's own scratch directory; return FileReference.from_local(<dir>, tier=StorageTier.RETAINED) on the task Output. Keep the counts the task returned (count the rows appended; RollingFileWriter.chunk_count). RollingFileWriter chunk names and no statistics sidecar are kept only where no reader matches file names; where one does, write the old names with a local write. Files a reader outside the run reads are delivered from run() with upload_refs at the same prefix.", "why": "The deprecated writers wrote and uploaded each chunk; the interceptor now persists the returned ref, and the explicit delivery keeps the same keys for outside readers."}
{"rule": "B001", "fix_shape": "reader-file-reference", "decision": "The reading task declares the producer's FileReference on its typed Input and reads the chunk files from .local_path through run_in_thread the way the deprecated reader did: for parquet read_batches, pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same size>) with each batch converted to pandas on its own; for parquet read(), one pyarrow table of all files converted once; for JSON, an orjson line loop. Data already in the store before this run is downloaded in run() and its ref handed to the task. A reader that caught the missing-prefix error today keeps that behaviour for a missing ref.", "why": "The interceptor materialises the ref before the task runs; the old reader downloaded the prefix itself."}
{"rule": "B008", "fix_shape": "reader-file-reference", "decision": "For a _download_files use: The reading task declares the producer's FileReference on its typed Input and reads the chunk files from .local_path through run_in_thread the way the deprecated reader did: for parquet read_batches, pyarrow.parquet.ParquetFile(path).iter_batches(batch_size=<same size>) with each batch converted to pandas on its own; for parquet read(), one pyarrow table of all files converted once; for JSON, an orjson line loop. Data already in the store before this run is downloaded in run() and its ref handed to the task. A reader that caught the missing-prefix error today keeps that behaviour for a missing ref.", "why": "The interceptor materialises the ref before the task runs; the old reader downloaded the prefix itself."}
{"rule": "B001", "match": "'(?:Incremental)?SqlMetadataExtractor'", "block": "The replacement is SqlApp, which runs extract_* / transform_* tasks and writes raw JSONL where the deprecated base ran fetch_* tasks and wrote parquet: task names, raw layout and the transform change together.", "brief": "Should this app move to SqlApp now (task names, raw layout and transform together, with upgrade-v3), or keep the deprecated base until v4?"}
{"rule": "B001", "fix_shape": "^sql-extractor$", "block": "BaseMetadataExtractor on a source read with SQL: the replacement is SqlApp, which changes task names, raw layout and the transform together.", "brief": "Should this app move to SqlApp now with upgrade-v3, or keep the deprecated base until v4?"}
{"rule": "B001", "fix_shape": "^live-transformer$", "block": "QueryBasedTransformer or AtlasTransformer builds the published assets from YAML templates or entity classes. Asset mappers change what the app publishes unless each asset type is proven equal.", "brief": "Should this transformer be rewritten as map_<entity>() asset mappers with a per-type equality proof, or kept until v4?"}
{"rule": "B001", "match": "PreflightStatus\\.PARTIAL", "block": "PreflightStatus.PARTIAL becomes READY or NOT_READY; either changes the status label the UI, Pulse and the Automation Engine show.", "brief": "READY (the gate behaves as today) or NOT_READY (the run blocks)? Either way keep the undetermined-readiness message."}
{"rule": "B008", "fix_shape": "^no-public-equivalent$", "block": "The private name has no public equivalent, or the app rebinds it to change the owner's own behaviour. The end state is an ignore that names a tracking issue.", "brief": "Which issue asks the owner (atlanhq/application-sdk for an SDK name) for a public API? The ignore then reads '# conformance: ignore[B008] no public equivalent — tracked in <id>'."}
{"rule": "B001", "fix_shape": "own-method-ignore", "suppress": true, "reason": "calls the app's own method of this name, not the SDK's deprecated one", "decision": "Add # conformance: ignore[B001] calls the app's own method of this name, not the SDK's deprecated one on the call line. Only when the receiver is self (or the app's own instance) and the first class in that class's base order that defines the method is an app class with no deprecated SDK base above it.", "why": "B001 matches method calls by name only; the rule names a reasoned ignore as the end state for a call that is not the SDK's."}
{"rule": "B001", "fix_shape": "^sql-app-override$", "block": "The app defines _resolve_credential_ref or _build_transform_input (or the public name) on an SqlApp subclass. The SDK's run() and preflight gate call only the public name, so renaming the override makes the SDK start calling the app's body.", "brief": "Should the SDK call this app's credential or transform-input override (rename it to the public name), or should the override be removed?"}
```
