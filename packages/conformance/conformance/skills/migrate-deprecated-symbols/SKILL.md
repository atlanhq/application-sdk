---
name: migrate-deprecated-symbols
description: >
  Move a connector app off deprecated SDK symbols (B001) and off private
  modules and names it does not own (B008) without changing what the app
  does. B001 reads the deprecation manifest the SDK ships, so the finding
  message carries the SDK's own notice of what replaces each symbol. A site is
  fixed here only where the replacement does the same thing; a site whose
  replacement changes what the app publishes, runs or shows is held for its
  owner, and a storage reader or writer goes to migrate-storage. Proven with
  re-detection and the test suite. Run it after any skill that raises the SDK,
  because a newer SDK can deprecate more symbols.
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
last_updated: "2026-11-25"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code and tests on the public name that does the same thing
  - justified ignores for private names with no public equivalent, each naming a tracking issue
  - held sites, each with what its owner must decide or which skill takes it
---

# Migrate deprecated and private symbols (B001, B008)

## What this protects

1. **Behaviour.** A deprecated symbol is on a removal path, and a private
   name has no compatibility promise at all (SDK 3.36.0 reshaped one private
   module, and every app that imported it stopped collecting tests). Both are
   reasons to move, never reasons to change what the app does. A site is
   fixed only where the replacement does the same thing: the same object
   under a public name, or the same signature and result. A test whose
   expectation must change for the fix to pass shows that behaviour changed:
   hold the site.
2. **The SDK names the replacement.** It is in the deprecation notice (the
   finding message, and the manifest's `message`) or in the detector's
   `atlan/safeSwap` property. Never guess it. A notice that points at a
   private module (`application_sdk._runtime...`) names no replacement: that
   site has no public equivalent.
3. **What the app publishes, runs or shows belongs to its owner.** Where the
   replacement changes the published assets, the task names or raw layout,
   a status a person sees, an HTTP status, or a key built from a value, the
   owner decides. Hold the site with the question; never pick an answer.
4. **One kind of change per skill.** A storage reader or writer
   (`ParquetFileReader`, `ParquetFileWriter`, `JsonFileReader`,
   `JsonFileWriter`, their subclasses and annotations, `upload_to_atlan`,
   `_download_files`, `_resolve_store`) changes how tasks hand files to each
   other: `migrate-storage` owns it (its `also_clears`), whichever way the
   symbol appears (import, subclass, annotation or call). A SQL extractor base
   (move to `SqlApp`) and a YAML transformer that builds the published assets
   (rewrite to asset mappers) are the app's v2 to v3 move, which the
   `upgrade-v3` skill in the `atlanhq/application-sdk` repo does for the
   whole app. Here these sites are held, never half migrated: a half
   migration breaks the hand-off between tasks or changes what the app
   publishes.
5. **When 1 cannot be proven, hold the site and say what is missing.** Your
   plan text is what the person reads. A site whose shape this skill does not
   describe gets the same treatment.

The rule definitions own what "correct" is: each finding carries its rule's
`full_description`. This skill owns how to reach it without breaking 1.

## Facts you need

- The manifest ships in the package:
  `$(dirname "$(atlan-application-sdk-conformance programs-dir)")/data/deprecated_symbols.json`,
  entries with `symbol`, `kind`, `module`, `message` (the notice) and
  `removal_version`. A notice can cite a doc that is only in the
  `atlanhq/application-sdk` repo; read it there.
- `atlan/safeSwap` on a finding is one fixed text change that keeps
  behaviour: `public-reexport` (B008, a module-level import from a private
  module whose names a public module re-exports as the same objects) and
  `public-rename` (B001, a call of a deprecated `SqlApp` method whose public
  name takes the same arguments, marked only when the receiver is proven to
  be the SDK's `SqlApp` and no file binds either name). Replace `find` with
  `replace` on the finding's line only, then run the app's import sorter and
  formatter. Apply it only when `uv.lock` pins `requires.package` at or above
  `requires.min`. A finding without the property is never a safe swap.
- B001 matches method calls by name only, so a call of the app's own method
  of the same name fires it: a false positive. B008 never fires on relative
  imports, on names the repo defines, or on dunders.
- `detect` does not scan `tests/`, but tests break on the same changes. Find
  them: `grep -rnE 'application_sdk[^ ]*\._|from application_sdk[^ ]* import .*\b_[A-Za-z]|PreflightStatus\.PARTIAL|in PreflightStatus|list\(PreflightStatus' tests/`,
  plus every symbol the app findings named and every symbol in this skill's
  `also_clears`. A string patch target (`patch("app.app.download_files")`)
  moves with the symbol it names; iterating `PreflightStatus` still yields
  `PARTIAL`.
- A function-local import of a re-exported name looks the name up on the
  public module when it runs, so a test that patched the private path patches
  the public path after the swap; a module-level import needs no test change.
- Each deprecated SDK error class has a same-code leaf that subclasses it
  until v4.0 and keeps its code, category, retry default and `str()` (#4707,
  FND-4910). The leaves take keyword arguments only.
- `HandlerError` has no single successor: the handler routes answer a typed
  `AppError` with the HTTP status of its category, so the successor is the
  subclass whose category answers with the status this raise answers today.
- `BaseMetadataExtractor` adds only the deprecated `upload_to_atlan` task to
  `App` (`atlan-metabase-app` `app/connector.py`, `MetabaseApp(App)`).
- `get_yaml_query_template_path_mappings` also merged in the SDK's bundled
  `QueryBasedTransformer` templates; its successor
  `application_sdk.common.yaml_templates.yaml_template_paths` does not
  (c8e5cd9b). It is the same result only when every key the app reads is a
  file the app ships.
- `get_workflow_id()` and `get_workflow_run_id()` return the same values as
  the task's typed `Input.workflow_id` / `self.context.workflow_id` and
  `self.run_id` / `self.context.run_id`.
- `fetch_marker` / `persist_marker` (SDK 3.41.0) take the arguments of the
  deprecated `fetch_marker_from_storage` / `persist_marker_to_storage` but
  return dataclasses: read `pair.marker` / `pair.next_marker` where the old
  code unpacked a tuple, and `result.s3_key` / `result.marker_timestamp`
  where it read a dict.
- Find the version a replacement needs from the SDK at the release tag, never
  from memory.

## The rules

### B001 — deprecated SDK symbol

End state per site: the replacement the notice names, where it does the same
thing (principle 1); a flagged import with no reader in `app/`, `tests/`,
generated manifests or `contract/app.pkl` deleted; a reasoned ignore for a
false positive; otherwise held (principles 3 to 5). An entry whose `removal_version` is at or below the
SDK version in `uv.lock` goes first: it can disappear in any release.

### B008 — private name the app does not own

End state per site: the public name for the same object; in an annotation,
which changes nothing at run time, the public types a private one stands for
(`ipaddress._BaseAddress` → `ipaddress.IPv4Address | ipaddress.IPv6Address`);
for a plain value
(a constant) the app does not rebind, a copy under the app's own name; first
look for a public surface that answers the same question (a test of how an
error maps to an HTTP status calls the real route through
`create_app_handler_service`, not `_app_error_to_http_status`). Otherwise
`# conformance: ignore[B008] no public equivalent — tracked in <id>`, where
`<id>` is an issue that asks the owner (`atlanhq/application-sdk` for an SDK
name) for a public API. Never invent an id: without one, hold the site.

Suppression form, on the line or on a comment-only line directly above:
`# conformance: ignore[<ID>] <reason>`.

## Before you change anything

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first, or
   record that an earlier migration can still change what is deprecated.
2. Baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule B001,B008 --exit-zero --output "$TMPDIR/before.sarif"`,
   and the tests with every group and extra installed
   (`UV_FROZEN=1 uv sync --all-groups --all-extras`, then
   `uv run --no-sync pytest tests/unit tests/integration`). Record the tests
   that already fail.
3. One row per site, and per use of a flagged import: file:line, the symbol,
   the notice, the end state, and the principle that decides it.

## Prove it

1. Re-detect with the baseline command: only held sites and reasoned ignores
   remain.
2. Tests: no failure that was not in the baseline, and no existing test
   assertion changed (principle 1). Collect every changed test module
   (`pytest --collect-only`).
3. Re-run the `tests/` search: each test site is fixed or held with its app
   site.

## PR description

- the sites changed, the replacement, and why it does the same thing;
- every behaviour difference a decided default allows (its `pr_note`);
- every site held: what its owner must decide, or which skill takes it.

## Working with a developer, and in the remediation lane

With a developer: stop after the inventory (before any edit), and after the
proof. In the remediation lane the decided defaults below are the
developer's answers; a held site exits with its plan text as the brief.

## Decided defaults

Owner answers read by remediation tooling. A default keeps what the app does
today. A row with `block` holds a site for a person: `match` is a pattern on
the finding message or source line, `fix_shape` a pattern on the planned
shape.

```decided-defaults
{"rule": "B008", "fix_shape": "public-reexport", "decision": "Import the same names from the public module that re-exports them (application_sdk.execution for get_object_store_prefix and build_output_path). A module-level import stays module level and needs no test change; a function-local import stays local, and a test patch of the private path moves to the public module path (patch(\"application_sdk.execution.<name>\")).", "why": "The public module re-exports the same objects, so behaviour cannot change; a function-local import now looks the name up on the public module when it runs."}
{"rule": "B001", "fix_shape": "public-rename", "decision": "Change the call to the public SqlApp method (resolve_credential_ref, build_transform_input) and move test patches of the old name to the public one. Only when the app defines no method of either name; otherwise the site is the sql-app-override owner decision.", "why": "The deprecated names delegate to the public ones with the same signature, so a call changes nothing."}
{"rule": "B001", "fix_shape": "base-to-app", "decision": "class X(App) in place of class X(BaseMetadataExtractor) when the app does not read the source with SQL through an SDK SQL client. First change every call of the SDK upload_to_atlan task to self.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED)) and read the same count from its UploadOutput; a method of that name the app defines itself stays. Update tests that assert the task is inherited.", "why": "The base adds only the deprecated upload_to_atlan task. Reference shape: atlan-metabase-app app/connector.py MetabaseApp(App) and its self.upload call.", "interface": false}
{"rule": "B001", "fix_shape": "transformer-wrapper", "decision": "Drop the TransformerInterface or QueryBasedTransformer base from a class that does its own mapping (no call reaches the base transform_metadata, no YAML template, no super() call). Delete an override that exists only to satisfy the base, with the error class and the test that only covered it. An annotation of the base becomes the app's own class.", "why": "Nothing the app runs uses the base, so removing it changes no output."}
{"rule": "B008", "fix_shape": "workflow-identity", "decision": "get_workflow_id() becomes input.workflow_id when the task's typed Input is in scope, else self.context.workflow_id; get_workflow_run_id() becomes self.run_id or self.context.run_id. Keep every key, path and log field built from the value.", "why": "App.run_id is the workflow run id and the task context carries the same workflow and run ids the private helpers read, so the values do not change."}
{"rule": "B001", "fix_shape": "error-typed-leaf", "decision": "Replace HandlerError with the application_sdk.errors subclass whose category the handler routes map to the HTTP status this raise answers with today: its http_status argument, or 500 when none is passed (InternalError). For example 400 InvalidInputError, 401 AuthError, 403 AppPermissionDeniedError, 404 NotFoundError, 503 DependencyUnavailableError or SourceUnavailableError (pick by what failed). A raise reachable only from a route that ignores http_status (/warmup) answers 500 today. A class that subclasses HandlerError takes the new class as its base. Keep the message and the cause, passed as message= and cause= (the typed classes take keyword arguments only, so a positional message raises TypeError), and drop the http_status=, handler_name= and error_code= arguments: the typed classes do not take them (only SourceUnavailableError takes http_status), and a kept one raises TypeError when the error is raised. Pass retryable=False when the new class retries by default (DependencyUnavailableError, SourceUnavailableError, ResourceExhaustedError, RateLimitedError, AppTimeoutError), because HandlerError does not retry. Update every except and isinstance that named HandlerError, and the tests that assert it. When no subclass maps to today's status, the site is the error-status-change owner decision.", "why": "HandlerError is deprecated with its category varying by raise site; the routes answer a typed AppError with the status of its category, so a subclass of the same status keeps the response status. The PR notes what changes: on the auth and metadata routes the detail loses the [CODE] ... | handler=... text HandlerError added (the preflight route already drops it), a custom error_code is dropped, and the failure category is no longer INTERNAL.", "pr_note": true}
{"rule": "B001", "fix_shape": "error-same-code-leaf", "decision": "Replace each deprecated SDK error class with its same-code leaf in every raise, except, isinstance and base-class list: CredentialVaultError -> CredentialVaultUnavailableError, BindingError -> BindingUnavailableError, StateStoreError -> StateStoreUnavailableError, PubSubError -> PubSubUnavailableError (application_sdk.infrastructure); ContractValidationError -> application_sdk.contracts.ContractDeclarationError; EntryPointContractError -> application_sdk.app.entrypoint.EntryPointDeclarationError; TaskContractError -> application_sdk.app.task.TaskDeclarationError; AppNotFoundError -> AppNotRegisteredError, TaskNotFoundError -> TaskNotRegisteredError, AppAlreadyRegisteredError -> AppVersionAlreadyRegisteredError (application_sdk.app.registry). None of the app leaves is importable from application_sdk.app itself: import each from the module named. Pass the message as message= (the leaves take keyword arguments only, so a positional message raises TypeError), keep the context arguments (credential_guid=, binding_name=, key=, topic=, operation=, cause=; contract_type=, field_name=, expected_type=, actual_type= on ContractDeclarationError) and drop error_code=, which no leaf takes. The registry classes built their message from positional arguments; pass that text as message= and the same values as keywords, or they go missing from FailureDetails.evidence: AppNotFoundError(name, version) -> AppNotRegisteredError(message=f\"App '{name}' version '{version}' not found\" (f\"App '{name}' not found\" without a version), name=name, version=version); AppAlreadyRegisteredError(name, version) -> AppVersionAlreadyRegisteredError(message=f\"App '{name}' version '{version}' is already registered\", name=name, version=version); TaskNotFoundError(app_name, task_name) -> TaskNotRegisteredError(message=f\"Task '{task_name}' not found in app '{app_name}'\", app_name=app_name, task_name=task_name).", "why": "Each leaf subclasses the class it replaces until v4.0 and keeps its code, category, retry default and str(), so the wire code, the HTTP status and every except that matched before are unchanged. The PR notes one change: type(exc).__name__, and so ApplicationError.type on the Temporal wire, names the leaf.", "pr_note": true}
{"rule": "B001", "fix_shape": "^error-status-change$", "block": "The HandlerError raise answers with an HTTP status that no application_sdk.errors subclass maps to, so any typed replacement changes the status the route returns.", "brief": "Which status should this error return after the move to a typed error, given that the current one has no typed equivalent?"}
{"rule": "B001", "match": "'(?:Incremental)?SqlMetadataExtractor'", "block": "This app still uses the v2 SQL extractor base. Its replacement is part of the v2 to v3 move that the upgrade-v3 skill (in the atlanhq/application-sdk repo) does for the whole app: SqlApp runs extract_* / transform_* tasks and writes raw JSONL where the old base ran fetch_* tasks and wrote parquet, so task names, raw layout and the transform change together. The lane does not make that move.", "brief": "This app needs the upgrade-v3 migration (a whole-app move a developer does); the site stays open until then."}
{"rule": "B001", "fix_shape": "^sql-extractor$", "block": "This app still uses the v2 SQL extractor base. Its replacement is part of the v2 to v3 move that the upgrade-v3 skill (in the atlanhq/application-sdk repo) does for the whole app: SqlApp runs extract_* / transform_* tasks and writes raw JSONL where the old base ran fetch_* tasks and wrote parquet, so task names, raw layout and the transform change together. The lane does not make that move.", "brief": "This app needs the upgrade-v3 migration (a whole-app move a developer does); the site stays open until then."}
{"rule": "B001", "fix_shape": "^live-transformer$", "block": "This app still uses the v2 YAML transformer (QueryBasedTransformer or AtlasTransformer) to build its published assets. Its replacement is part of the v2 to v3 move that the upgrade-v3 skill (in the atlanhq/application-sdk repo) does for the whole app: asset mappers (typed records, map_<entity>() functions, pyatlan assets) build every asset anew, so the published output changes unless it is proven equal per asset type. The lane does not make that move.", "brief": "This app needs the upgrade-v3 migration (a whole-app move a developer does); the site stays open until then."}
{"rule": "B001", "match": "PreflightStatus\\.PARTIAL", "block": "PreflightStatus.PARTIAL becomes READY or NOT_READY; either changes the status label the UI, Pulse and the Automation Engine show.", "brief": "READY (the gate behaves as today) or NOT_READY (the run blocks)? Either way keep the undetermined-readiness message."}
{"rule": "B008", "fix_shape": "^no-public-equivalent$", "block": "The private name has no public equivalent, or the app rebinds it to change the owner's own behaviour. The end state is an ignore that names a tracking issue.", "brief": "Which issue asks the owner (atlanhq/application-sdk for an SDK name) for a public API? The ignore then reads '# conformance: ignore[B008] no public equivalent — tracked in <id>'."}
{"rule": "B001", "fix_shape": "own-method-ignore", "suppress": true, "reason": "calls the app's own method of this name, not the SDK's deprecated one", "decision": "Add # conformance: ignore[B001] calls the app's own method of this name, not the SDK's deprecated one on the call line. Only when the receiver is self (or the app's own instance) and the first class in that class's base order that defines the method is an app class with no deprecated SDK base above it.", "why": "B001 matches method calls by name only; the rule names a reasoned ignore as the end state for a call that is not the SDK's."}
{"rule": "B001", "fix_shape": "^sql-app-override$", "block": "The app defines _resolve_credential_ref or _build_transform_input (or the public name) on an SqlApp subclass. The SDK's run() and preflight gate call only the public name, so renaming the override makes the SDK start calling the app's body.", "brief": "Should the SDK call this app's credential or transform-input override (rename it to the public name), or should the override be removed?"}
{"rule": "B001", "fix_shape": "^hold$", "block": "Held: the replacement does not do the same thing, changes what the app publishes, runs or shows, or belongs to another skill. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "B008", "fix_shape": "^hold$", "block": "Held: the replacement does not do the same thing, changes what the app publishes, runs or shows, or belongs to another skill. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
```
