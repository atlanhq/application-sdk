---
name: upgrade-v3
description: Upgrade a connector repo from application-sdk v2 to v3 — runs the import rewriter, performs AI-assisted structural refactoring, and validates the result with the upgrade checker.
argument-hint: "<path-to-connector-repo>"
---

# /upgrade-v3

Performs a complete v2 → v3 upgrade of an application-sdk connector.

**Must be run from the application-sdk repo root** (so the upgrade tooling and docs are reachable).

## Usage

```
/upgrade-v3 ../my-connector/src
/upgrade-v3 /absolute/path/to/connector/
```

---

## Phase 0 — Setup and validation

1. Parse `$ARGUMENTS` to get the target path. If no argument is given, stop and ask the user for one.
2. Confirm the target path exists. If it does not, stop and report the error.
3. Confirm you are running from within the application-sdk repo by checking that `tools/migrate_v3/rewrite_imports.py` exists. If it does not, stop and tell the user to run this skill from the application-sdk repo root.
4. **Check the connector's SDK dependency.** Read the connector's `pyproject.toml` and look for `atlan-application-sdk`. The minimum supported v3 version for upgrades is **3.3.0** — earlier 3.x releases are missing `CredentialRef.resolve()`, `self.upload()`-via-`FileReference` plumbing, and the typed credential routing the rest of this skill assumes. The dependency must be:
   ```toml
   atlan-application-sdk>=3.3.0,<4.0.0
   ```
   - If it points to a v2 release (e.g. `atlan-application-sdk>=2.x`) or an early v3 (`>=3.0.0`/`>=3.1.0`/`>=3.2.0`), bump the pin to `>=3.3.0,<4.0.0` and run `uv sync`. Refresh `uv.lock` in the same commit.
   - If the `pyproject.toml` still contains a `[tool.uv.sources]` git override pointing at `main` or `refactor-v3` (a pattern used during v3 development), **remove it** — it's no longer needed now that v3.3.0 is on PyPI, and leaving it in pins the connector to an unstable ref.
   - If it already depends on `atlan-application-sdk>=3.3.0` from PyPI with no git override, proceed.

4b. **Check temporalio version.** The v3 SDK requires `temporalio` with `VersioningBehavior`. Run in the connector repo root (where `pyproject.toml` is):
   ```bash
   cd <connector-repo-root> && uv run python -c "from temporalio.common import VersioningBehavior"
   ```
   If this fails with `ImportError`, upgrade temporalio before continuing:
   ```bash
   cd <connector-repo-root> && uv add temporalio --upgrade && uv sync
   ```

5. Read `tools/migrate_v3/MIGRATION_PROMPT.md` in full. This is the authoritative reference for all structural changes you will make. Do not proceed to Phase 3 without having read it.
5b. Read the **upgrade guide** at `docs/upgrade-guide-v3.md` in the application-sdk repo (also available at https://github.com/atlanhq/application-sdk/blob/main/docs/upgrade-guide-v3.md). This is the user-facing guide with v2 → v3 code examples for every upgrade step (imports, templates, handler, entry point, infrastructure, credentials, tests). Use it as a reference when making structural changes — it has the canonical before/after code snippets.
6. Run an initial checker pass to establish the baseline — **do not fix anything yet**:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

Print a short summary: how many FAILs and WARNs were found. If zero FAILs, tell the user the connector may already be migrated and stop.

---

## Phase 1 — Mechanical import rewrites

Run the import rewriter across the **entire** target directory tree, including test files. This step is purely mechanical — import paths are rewritten losslessly; no logic is touched.

```bash
uv run python -m tools.migrate_v3.rewrite_imports <target-path>
```

Log every file that was changed. After the rewriter completes, tell the user which files were rewritten and how many import rewrites were applied.

**Test files are NOT exempt from this phase.** Deprecated import paths in tests must be updated just like production code — they are purely mechanical path changes. The constraint that applies to test files is that you must NEVER modify test logic, assertions, fixtures, or test data in any phase.

---

## Phase 1b — Automated structural codemods

Before any AI-assisted structural work, run the codemod pipeline. This eliminates the mechanical transforms (decorator removal, signature rewrites, activity call rewrites, activities plumbing cleanup, entry point rewrite) deterministically, so the AI only needs to handle what remains.

```bash
uv run python -m tools.migrate_v3.run_codemods <target-path>
```

Review the output: files changed, any `SKIPPED` entries (dynamic dispatch or complex entry points that need manual attention), and errors.

Then re-run the checker to see what FAILs remain — those are the AI's work scope for Phase 2b:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

Log a short summary of what the codemods changed and what FAILs remain.

**Extract structured context for Phase 2b** — before writing any AI-assisted structural changes, run the context extractor to get a compact summary of what needs to be done. Use this summary when prompting yourself for Phase 2b instead of re-reading raw source files:

```bash
uv run python -m tools.migrate_v3.extract_context <target-path>
```

The output lists: connector type and confidence, difficulty estimate, classes with their roles and method inventories, infrastructure usage patterns, and any warnings about complex entry points or dynamic dispatch. Include this summary at the start of your Phase 2b analysis.

---

## Phase 2 — Structural migration

Read the checker output from Phase 0 and the structure of the connector code to determine what structural work is needed.

### 2a — Identify connector type

Examine the source files in the target path (exclude test files from this analysis):

- Look for classes inheriting from `BaseSQLMetadataExtractionWorkflow` / `BaseSQLMetadataExtractionActivities` → SQL metadata extractor (§2a of MIGRATION_PROMPT.md)
- Look for classes inheriting from `SQLQueryExtractionWorkflow` / `SQLQueryExtractionActivities` → SQL query extractor (§2b)
- Look for classes inheriting from `IncrementalSQLMetadataExtractionWorkflow` → Incremental SQL extractor (§2c)
- Look for HTTP/REST client usage (`httpx`, `aiohttp`, `requests`, or custom `BaseClient` subclasses) with no SQL queries → REST/HTTP metadata extractor (§2d)
- Look for any other `WorkflowInterface` / `ActivitiesInterface` subclasses that don't fit above → Custom App (§3)
- In all cases: identify the handler class (§4) and the entry point (§5)
- **Count the total number of distinct `WorkflowInterface` subclasses** (exclude base classes from the SDK). If there is more than one, this is a **multi-workflow connector** — flag it and handle it in Phase 2a′ below before proceeding.

> **Tip — auto-detect the connector type:**
> ```bash
> uv run python -m tools.migrate_v3.check_migration --classify <target-path>
> ```
> This runs the F3 fingerprinter before the check pass and prints the detected
> connector type with confidence score and evidence.

### 2a′ — Choose transformation approach

After identifying the connector type, determine the transformation strategy:

**SQL connectors** (SqlMetadataExtractor, SqlQueryExtractor, IncrementalSqlMetadataExtractor):
- **Default to the asset-mapper + `FileReference` + `self.upload()` pipeline.** This is the v3-native shape that lets each `@task` stream rows into a typed file output and hand the path downstream — same as `atlan-openapi-app`. The fetch tasks return `FileReference`-typed outputs, the publish step calls `self.upload(UploadInput(local_path=..., storage_path=...))`, and there is no shared `output_path` / scan-the-directory upload step. Inform the user:
  > "v3 SQL connectors use the same asset-mapper + FileReference pipeline as REST connectors: each fetch task writes a typed file and returns a `FileReference`; `self.upload()` carries it to object store. This replaces the v2 `output_path` directory + `upload_to_atlan()` scan pattern. Shall I proceed with this approach?"
- Only fall back to the legacy transformer (`QueryBasedTransformer` / `AtlasTransformer` inside `transform_data()`) if the user explicitly asks to minimize migration risk and accepts the cleanup follow-up. Note this in the manual-follow-up list — it preserves YAML query files and Daft DataFrames that the team is removing.
- **Do NOT** keep the v2 "build one shared `output_path`, write all fetch outputs there, scan-and-upload at the end" pattern. Each task returns its own `FileReference`; uploads happen via `self.upload()`. This was a top review finding on the MSSQL v3 PR.

**Multi-workflow connectors** (more than one `WorkflowInterface` subclass detected):
- Consolidate into a **single `App` subclass** with one `@entrypoint`-decorated method per v2 workflow. All entry points share `@task` methods, the handler, and `AppContext`.
- `ATLAN_APP_MODULE` stays a single `module:ClassName` — no comma-separated list.
- See §5b of MIGRATION_PROMPT.md for the full pattern and `tests/integration/test_multi_entrypoint.py` for a canonical example.
- Inform the user: _"This connector has N workflows. In v3 they become N `@entrypoint` methods on one App class, sharing task helpers and the handler. I'll consolidate them into a single App."_

**REST/API connectors** (BaseMetadataExtractor, Custom App):
- **Default to the asset-mapper approach.** This is the v3-native pattern (see `atlan-openapi-app` as the reference implementation). Inform the user:
  > "REST/API connectors in v3 use the asset-mapper pattern: typed Python records → pure Python mapper functions → pyatlan Asset instances → JSONL. This replaces the v2 `QueryBasedTransformer`/`AtlasTransformer` approach. The reference implementation is `atlan-openapi-app`. Shall I proceed with this approach?"
- If the user prefers to keep the existing transformer, respect that — but note it as a manual follow-up item in the summary.

**Asset-mapper pattern summary** (for reference when implementing):
```
Extract phase:  API response → typed records (dataclass/msgspec.Struct) → JSONL files
                Pass between tasks via FileReference
Transform phase: Read typed records from JSONL → mapper functions → pyatlan Asset instances
                 Write via asset.to_nested_bytes() → JSONL output file
```
Key elements:
- `app/asset_mapper.py` — pure functions: `map_<entity>(record, connection_qn, ...) -> pyatlan.Asset`
- `app/api_types.py` — typed intermediate records (dataclass or `msgspec.Struct`)
- No `TransformerInterface`, no Daft DataFrames, no YAML query files
- Uses `msgspec.json` or `json` for JSONL serialization
- `FileReference` in task contracts to pass file paths between extract → transform tasks

### 2b — Apply structural changes

> **Incremental validation**: Run `check_migration` after each sub-step below.
> The checker output shows specific remaining FAILs — use it to guide the next step.

Follow the exact checklists in `tools/migrate_v3/MIGRATION_PROMPT.md` for the connector type(s) identified above.

**Hard constraint — tests are completely out of bounds for structural changes:**

- You MUST NOT modify test method bodies, assertions, fixtures, mock setup, or test data in any file under any directory whose name contains `test` or starts with `test_`.
- You MUST NOT add, remove, or rewrite test cases.
- You MUST NOT change the logic of any existing test.
- The only change permitted in test files is the mechanical import rewrite already performed in Phase 1. If a test file needs structural changes to compile (e.g. it directly instantiates a v2 class that no longer exists), add a `# TODO(upgrade-v3): update test to use v3 API` comment and leave the test body unchanged. The user will update tests manually after verifying the migration is correct.

**Hard constraint — handler method signatures:**

- Handler methods (`test_auth`, `preflight_check`, `fetch_metadata`) MUST use typed contract parameters. Do NOT use `*args` or `**kwargs`.
- Correct: `async def test_auth(self, input: AuthInput) -> AuthOutput:`
- Forbidden: `async def test_auth(self, *args, **kwargs):`
- The checker will FAIL if `*args`/`**kwargs` appear in a Handler subclass method.

**Constraint — `allow_unbounded_fields=True` must be used sparingly:**

- Needed on top-level `run()` input models that receive arbitrary dicts from AE/Heracles (e.g. `connection: dict[str, Any]`, `metadata: dict[str, Any]`).
- Must NOT be used on inter-task Input/Output contracts — use `Annotated[list[T], MaxItems(N)]` or `FileReference` instead.
- The checker will WARN if `allow_unbounded_fields=True` appears on task-level contracts.

Apply changes in this order:

1. **App class** — merge Workflow + Activities into the appropriate template subclass with `@task` methods. Preserve all SQL query strings and business logic verbatim. For multi-workflow connectors, give each v2 workflow its own `@entrypoint` method on the single shared App class (see §5b of MIGRATION_PROMPT.md); hoist duplicated activity helpers into shared `@task` methods.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-v2-decorators`, `no-execute-activity-method`) before proceeding.

2. **Handler** — update base class, method signatures (typed contracts, no `**kwargs`), remove `load()`.

   **`fetch_metadata` must return the correct widget-specific output type:**
   - **SQL connectors** → `SqlMetadataOutput(objects=[SqlMetadataObject(TABLE_CATALOG="...", TABLE_SCHEMA="...")])`
   - **BI/API connectors** → `ApiMetadataOutput(objects=[ApiMetadataObject(value="...", title="...", node_type="...", children=[...])])`
   - Do NOT use generic `MetadataOutput` or deprecated `MetadataObject` — these emit `DeprecationWarning` and will be removed in v3.1.0.
   - Import from `application_sdk.handler` (e.g. `from application_sdk.handler import SqlMetadataOutput, SqlMetadataObject`).

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `handler-typed-signatures`) before proceeding.

3. **Entry point** — replace `BaseXxxApplication` instantiation with `run_dev_combined()` or CLI reference.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-base-application`) before proceeding.

4. **Infrastructure calls** — replace `SecretStore`/`StateStore`/`ObjectStore` calls with `self.context.*` per §6 of MIGRATION_PROMPT.md.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-dapr-client`, `use-app-state`) before proceeding.

Work through one section at a time. After completing each section, check your changes are self-consistent before moving on.

### 2c — Directory consolidation

After completing the structural migration in 2b, consolidate the v2 directory layout. v2 connectors split logic across `app/activities/` and `app/workflows/`; v3 uses a single flat file. For multi-workflow connectors this means all `@entrypoint` methods live in one file — do **not** split back into multiple files.

1. Identify the main App class file (typically `app/activities/<name>.py`, or whichever file holds the merged multi-workflow App).
2. Move it to `app/<app_name>.py` (derive the filename from the App class or connector name, snake_cased).
3. If `app/workflows/<name>.py` exists and only re-exports from activities (e.g. `from app.activities.<name> import MyConnector`), delete it.
4. Delete the now-empty `app/activities/` and `app/workflows/` directories.
5. Update all **production-code** imports that referenced the old paths.
6. For **test-file** imports pointing to the old paths:
   a. Construct a JSON mapping of old module paths → new module paths from steps 1–4. For example, if `app/activities/metadata_extraction.py` moved to `app/metadata_extraction.py`, the mapping is `{"app.activities.metadata_extraction": "app.metadata_extraction"}`.
   b. Run the internal import rewriter on the test directory:
      ```bash
      uv run python -m tools.migrate_v3.rewrite_imports \
        --internal-map '{"app.activities.<name>": "app.<name>"}' \
        <target-path>/tests/
      ```
   c. For any **symbol names** that also changed (e.g. `AnaplanMetadataExtractionActivities` → `AnaplanApp`), manually update the import line's symbol name in each affected test file AND add a comment at the top of that file: `# TODO(upgrade-v3): update references from OldClass to NewClass in test bodies`. Do NOT modify test bodies, assertions, fixtures, or mocks.
7. Re-run the checker to confirm the `no-v2-directory-structure` advisory is gone.

If the connector does not have an `activities/` or `workflows/` directory, skip this step.

---

## Phase 2d — Post-processing cleanup

After completing the structural changes in 2b and directory consolidation in 2c, run these cleanup steps to normalize imports and formatting before the validation loop:

```bash
# Remove unused imports and sort import order
uv run ruff check --fix --select I,F401 <target-path>

# Normalize formatting
uv run ruff format <target-path>

# Check migration status
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

All FAIL checks should pass at this point. If any remain, address them before moving to Phase 3.

**Positive-idiom checks (manual — the migration checker does not enforce these).** Each one corresponds to a review finding that blocked the MSSQL v3 PR. Each must come up clean before Phase 3 — if any returns matches, refactor per the "v3 Task Idioms" section below.

```bash
# 1) No imports from private SDK modules (any segment starting with `_`).
#    Catches both top-level (application_sdk._x) and nested (application_sdk.foo._y).
grep -rEn "from application_sdk[A-Za-z0-9_.]*\._[A-Za-z0-9_]" <target-path>/app/ \
  && echo "FAIL: private SDK import" || echo "OK: no private SDK imports"

# 2) No asyncio.to_thread inside @task code (use self.run_in_thread)
grep -rn "asyncio\.to_thread\b" <target-path>/app/ \
  && echo "FAIL: replace with self.run_in_thread" || echo "OK"

# 3) No os.environ reads in app/ — entry points (main.py / run_dev.py) live OUTSIDE
#    the app/ dir or under a clearly-marked boundary. If your repo puts them under
#    app/, exclude those paths explicitly.
grep -rEn "os\.environ\.get\b|os\.environ\[|os\.getenv\b" <target-path>/app/ \
  && echo "FAIL: move config to Input contract or AppConfig" || echo "OK"

# 4) No full-result materialization (cursor.fetchall / pd.read_sql_query)
grep -rEn "\.fetchall\b|pd\.read_sql_query\b|read_sql_query\b" <target-path>/app/ \
  && echo "FAIL: stream via fetchmany() instead" || echo "OK"

# 5) No raw context.get_secret(<guid>) for credentials — use CredentialRef.resolve(input).
#    (Legitimate non-credential secret lookups are rare; if you have one, document it.)
grep -rn "context\.get_secret\b" <target-path>/app/ \
  && echo "FAIL: use CredentialRef.resolve(input) for credentials" || echo "OK"

# 6) No hand-rolled obstore / ParquetFileWriter / JsonFileWriter usage
grep -rEn "ParquetFileWriter\b|JsonFileWriter\b|obstore\." <target-path>/app/ \
  && echo "FAIL: use FileReference + self.upload()" || echo "OK"

# 7) allow_unbounded_fields is only on the top-level run() Input model — inheriting
#    classes must not redeclare it.
grep -rn "allow_unbounded_fields=True" <target-path>/app/ \
  && echo "WARN: confirm only top-level run() Input declares allow_unbounded_fields=True" || echo "OK"
```

These are guardrails, not blockers — a connector with a legitimate reason for any of these patterns may still ship, but it must be called out in the Phase 5 summary's manual-follow-up list with the reason. Default stance: refactor. Note: check #3 will hit any `os.environ` read inside `app/`; if the connector's entry point lives under `app/main.py` or `app/run_dev.py`, exclude those paths from the grep before treating a hit as a FAIL.

---

## Phase 3 — Validation loop

Run the checker after completing Phase 2:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

**If FAILs remain:**
- Read each failing item and the relevant source file.
- Fix the specific issue according to MIGRATION_PROMPT.md.
- Re-run the checker.
- Repeat until zero FAILs. Do not move to Phase 4 until the checker exits with code 0.

**If only WARNs remain:**
- Read each WARN item. If it is fixable without modifying test logic, fix it.
- If a WARN requires modifying test logic, skip it and add it to the manual follow-up list.

---

## Phase 4 — Test run

Run the connector's test suite **without modifying any test files**:

```bash
cd <target-path> && uv run pytest --tb=short -q 2>&1 | head -80
```

If `uv` is not available in the connector repo, try `python -m pytest --tb=short -q` instead.

Do **not** modify any test to make it pass. If tests fail:
- Read the failing test and the code it exercises.
- If the failure is due to a production code issue introduced during upgrade (e.g. wrong method signature, missing attribute), fix the production code.
- If the failure requires understanding test intent or rewriting test logic, do NOT fix it. Add it to the manual follow-up list.

### Phase 4b — E2E test generation

After the test suite run, check whether the connector has e2e tests using the v2 `BaseTest` / `TestInterface` pattern:

1. Search for files under `tests/e2e/` (or `tests/integration/`) that import `BaseTest` or `TestInterface`.
2. If found, **read the original v2 e2e test file completely**. List every test method and what it asserts before writing a single line of the new file.
3. Generate a **new** equivalent e2e test file using the v3 `application_sdk.testing.e2e` API (§9 of MIGRATION_PROMPT.md):
   - For **each** test method in the original, generate a corresponding `async def test_xxx(deployed_app)` function. The generated file MUST have at least as many test functions as the original has test methods.
   - Extract actual payload values from the original (hardcoded dicts, `default_payload()` bodies, connection IDs) — do **not** substitute placeholder values like `"test-connection"` if the original has real values.
   - If an assertion checks response fields whose format changed (e.g. `result['authenticationCheck']`), keep the assertion but add `# TODO(upgrade-v3): response format changed — update field names`.
   - Use the `AppConfig` fixture with real values derived from the connector's `pyproject.toml` (`[project].name` in PEP 621 / uv layout, or Helm chart values) — not generic placeholders.
4. Place the new file alongside the original, named `tests/e2e/test_<connector_name>_v3.py`.
5. Add `# TODO(upgrade-v3): human must validate this test is equivalent to the original` at the top of the new file.
6. Do NOT delete or modify the original test file.
7. Add the new test file to the manual follow-up list so the user knows to validate it.

If the connector has no v2-style e2e tests, skip this step.

### Phase 4c — Contract generation

After tests pass and e2e tests are generated, the app needs a PKL contract that generates workflow config, credential config, AE manifest, and typed Input class. Use the **`/contract` skill** (located in this repo at `.claude/skills/contract/SKILL.md`) to create or migrate the contract.

1. Check if the connector already has a `contract/` directory with `app.pkl`:
   ```bash
   ls <target-path>/contract/app.pkl 2>/dev/null && echo "EXISTS" || echo "MISSING"
   ```

2. **If `contract/app.pkl` exists:** Check `contract/PklProject` for the `atlan-app-contract-toolkit` version. If it is **older than 0.9.0**, bump it before regenerating — earlier versions emit the legacy `metadata`-wrapped extract args (`{ "metadata": { "include_filter": ... } }`) that AE no longer prefers. Toolkit 0.9.0+ emits flat snake_case args (`include_filter`, `exclude_filter`, `temp_table_regex`, `preflight_check`, `extraction_method`) at the top level. Refresh `PklProject.deps.json` after the bump. Then invoke `/contract` with the update action to regenerate `manifest.json`.

3. **If no contract exists:** Inform the user that v3 apps should have a PKL contract and ask whether to generate one:
   > "This connector has no PKL contract (`contract/app.pkl`). v3 apps use contracts to generate workflow config, credential config, AE manifest, and typed Input classes — the SDK auto-serves these at `/workflows/v1/configmap/*` and `/manifest`. Want me to generate one using the `/contract` skill?"

4. If the user confirms, invoke the `/contract` skill. It will:
   - Create `contract/PklProject`, `contract/app.pkl`
   - Generate `contract/generated/{name}.json`, `contract/generated/atlan-connectors-{name}.json`, `contract/generated/manifest.json`, and `contract/generated/_input.py`
   - Add the `poe generate` task to `pyproject.toml`

5. After contract generation, verify the generated files exist and the handler serves them:
   ```bash
   ls <target-path>/contract/generated/*.json
   ```

6. If the user declines, add "Contract generation skipped — run `/contract` to set up PKL contract" to the manual follow-up list.

---

## Phase 5 — Summary report

Print a structured summary:

```
## v3 Migration Summary

### Target
<path>

### Phase 1 — Import rewrites
- Files modified: N
- Imports rewritten: N
- Files with structural TODO comments: N

### Phase 2 — Structural changes
<bullet per file changed: what was changed>

### Phase 2c — Directory consolidation
- Activities/workflows dirs found: yes/no
- Files moved: <list>
- Directories deleted: <list>
- Import references updated: N files

### Phase 3 — Checker result
- FAIL items resolved: N
- WARN items resolved: N
- WARN items remaining (manual): <list>

### Phase 4 — Test results
- Tests passing: N/N
- Tests failing (manual follow-up): <list of test names and reason>

### Phase 4b — E2E test generation
- v2 BaseTest files found: yes/no
- New v3 e2e test file generated: <path or "N/A">
- Original test method count: N
- Generated test function count: N
- Human validation required: yes/no

### API Contract Changes (inform frontend consumers)
<list any response-format-change WARNs from the checker — these indicate v3 handler
methods that return a different response shape than v2, which may break frontends>
- fetch_metadata: returns SqlMetadataOutput (flat [{TABLE_CATALOG, TABLE_SCHEMA}] for SQL) or ApiMetadataOutput (tree [{value, title, node_type, children}] for BI/API) — `data` is now a flat list, not {objects, total_count}
- preflight_check: returns PreflightOutput — service auto-converts to v2 camelCase format {authCheck: {success, message}, connectivityCheck: {success, message}}

### Manual follow-up required
<bulleted list of anything the AI skipped due to the test constraint or ambiguity>
```

Remind the user:
- Run `uv run pre-commit run --all-files` in the connector repo before committing.
- Review all `# TODO(upgrade-v3)` comments — each one marks a location that needs human verification.
- The typed `Input`/`Output` models for custom `@task` methods should be defined (see §7 of MIGRATION_PROMPT.md) — these were not auto-generated.
- If an e2e test was generated in Phase 4b, validate that it is logically equivalent to the original before deleting the old file.

---

## Phase 6 — Live verification

This phase starts the app locally and verifies handler endpoints and workflow execution against a v2 baseline. **Ask the user for confirmation before each major step.**

### 6a — Establish v2 baseline

Before starting the v3 app, check for an existing v2 workflow run:

1. Look for output in `./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/`:
   ```bash
   find ./local/dapr/objectstore/artifacts -name "*.json" -path "*/transformed/*" | head -20
   ```
2. If output exists, ask the user:
   > "Found existing workflow output at `<path>`. Was this from a v2 SDK run with full parity? Should I use it as the baseline for comparison?"
3. If no output exists, ask:
   > "No existing workflow output found. Have you run the v2 version of this connector locally? A v2 baseline is needed to verify parity after migration."
4. If the user confirms a baseline exists, count entities per type:
   ```bash
   for f in ./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<latest-run>/transformed/*/*.json; do
     echo "$(basename $(dirname $f)): $(wc -l < "$f") entities"
   done
   ```
   Record these counts — they are the parity target.

### 6b — Run tests

Run the full test suite before attempting a live run:

```bash
cd <target-path> && uv run pytest tests/unit/ --tb=short -q
cd <target-path> && uv run pytest tests/e2e/ --tb=short -q
```

If any tests fail, fix production code issues before proceeding. Do not continue to 6c with failing tests.

### 6c — Start the app

Ask the user:
> "Tests pass. Ready to start the app with `atlan app run`. This requires credentials in `.env`. Should I proceed?"

If confirmed:

```bash
cd <target-path> && atlan app run -p .
```

Wait for the app to be ready (look for `Uvicorn running on http://127.0.0.1:8000`).

### 6d — Test handler endpoints

Read credentials from `.env` (look for `API_KEY_ID`, `API_SECRET`, and any workspace/extra fields). Then test all three handler endpoints:

**Auth test:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/auth \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": {
      "host": "<host>",
      "username": "<API_KEY_ID>",
      "password": "<API_SECRET>",
      "extra": {"workspace": "<workspace>"}
    }
  }'
```
Expected: `{"success": true, "data": {"status": "success"}}`

**Preflight check:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/check \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": {}
  }'
```
Expected: v2-compatible camelCase format — the service layer auto-converts `PreflightCheck` items:
`{"success": true, "data": {"authCheck": {"success": true, "message": "..."}, "connectivityCheck": {"success": true, "message": "..."}}}`
Each check's `name` field becomes a camelCase key in `data`, with `success` (from `passed`) and `message` fields.

**Metadata fetch:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/metadata \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": {}
  }'
```
Expected: **flat list** in `data` (NOT a nested object):
- SQL connectors: `{"success": true, "data": [{"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "FINANCE"}, ...]}`
- BI/API connectors: `{"success": true, "data": [{"value": "id-1", "title": "Name", "node_type": "tag", "children": [...]}, ...]}`

Note: `total_count`, `truncated`, and `fetch_duration_ms` are no longer in the response.

**Config endpoints:**
```bash
# List configmaps
curl -s http://localhost:8000/workflows/v1/configmaps

# Get specific configmap
curl -s http://localhost:8000/workflows/v1/configmap/<id>

# Get manifest (single-entry-point app)
curl -s http://localhost:8000/workflows/v1/manifest

# Get manifest for a specific entry point (multi-entry-point app)
curl -s 'http://localhost:8000/workflows/v1/manifest?entrypoint=extract-metadata'
curl -s 'http://localhost:8000/workflows/v1/manifest?entrypoint=mine-queries'
```

If any endpoint returns 500, check the app terminal for the traceback. Common issues:
- Handler not discovered → import the handler class in the App module
- Credential format error → ensure `_normalize_credentials` is in the SDK version being used
- Missing configmap files → verify `app/generated/` has the JSON files from `poe generate`

### 6e — Start a workflow run

Ask the user:
> "Handler endpoints verified. Ready to trigger a workflow run. This will call external APIs and write output to `./local/dapr/objectstore/`. Should I proceed?"

If confirmed:
```bash
# Single-entry-point app — no entrypoint selector needed
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": { <connector-specific metadata> },
    "connection": {"connection": "dev"}
  }'

# Multi-entry-point app — ?entrypoint=<name> is required
curl -s -X POST 'http://localhost:8000/workflows/v1/start?entrypoint=<entry-point-name>' \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": { <connector-specific metadata> },
    "connection": {"connection": "dev"}
  }'
```

The entry-point name is the kebab-case method name (e.g. `extract_metadata` → `extract-metadata`). Omitting `?entrypoint=` on a multi-entry-point app returns 400.

Capture `workflow_id` and `run_id` from the response. Monitor status:
```bash
curl -s http://localhost:8000/workflows/v1/status/<workflow_id>/<run_id>
```

Poll until status is `COMPLETED` or `FAILED`. Also monitor Temporal UI at `http://localhost:8233`.

### 6f — Parity comparison

Once the workflow completes, locate the v3 output. The path follows this structure:
```
./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<workflow_id>/<run_id>/transformed/
```

Use the `workflow_id` and `run_id` from the start response to find it:
```bash
V3_OUTPUT="./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<workflow_id>/<run_id>/transformed"
```

Count entities per type in the v3 output:
```bash
for dir in "$V3_OUTPUT"/*/; do
  typename=$(basename "$dir")
  count=0
  for f in "$dir"*.json; do
    [ -f "$f" ] && count=$((count + $(wc -l < "$f")))
  done
  echo "$typename: $count"
done
```

If a v2 baseline exists (from step 6a), compare:

```
Entity type      | v2 count | v3 count | Match
-----------------|----------|----------|------
workspace        |        1 |        1 | ✓
collection       |       67 |       67 | ✓
report           |       68 |       30 | ✗ (-38)
```

### 6g — Fix-and-retry loop

If parity fails (counts differ, workflow errors, or endpoint failures):

1. **Diagnose.** Read the app terminal logs and Temporal UI (`http://localhost:8233`) for errors. Common causes:
   - HTTP errors during extraction → check client retry logic, rate limiting, auth
   - Missing entities → check filtering logic, pagination, or API response parsing
   - Transform errors → check Parquet round-trip issues (nested dicts become JSON strings)
   - Empty output → check `output_path` computation and `output_prefix` defaults

2. **Fix.** Apply the fix to production code only. Do not modify tests.

3. **Re-run from 6b.** After fixing:
   - Re-run tests (6b) to confirm the fix doesn't break anything
   - Restart the app (6c) to pick up code changes
   - Re-test handler endpoints (6d) to confirm they still work
   - Trigger a new workflow run (6e)
   - Re-compare parity (6f)

4. **Repeat** until all entity counts match the v2 baseline and all endpoints return success.

Do not proceed to the summary until:
- All handler endpoints return success
- Workflow completes without errors
- Entity counts match the v2 baseline (or the user explicitly accepts the difference with an explanation)

### 6h — Print verification summary

Only print this after parity is achieved or the user accepts the result:

```
## Live Verification Summary

### Handler Endpoints
- POST /workflows/v1/auth:      ✓ success
- POST /workflows/v1/check:     ✓ ready (N checks passed)
- POST /workflows/v1/metadata:  ✓ N objects returned
- GET  /workflows/v1/configmaps: ✓ N configmaps
- GET  /workflows/v1/manifest:   ✓ served

### Workflow Execution
- Workflow ID: <id>
- Run ID: <id>
- Status: COMPLETED
- Duration: Ns
- Retry attempts: N (if fix-and-retry loop was needed)

### Parity
<entity count comparison table>
- Parity: ACHIEVED
- Fixes applied during retry loop: <list of fixes, or "none">
```

---

## SDK Bug Protocol — When You Find a Bug in v3 SDK

**This is a HARD, NON-NEGOTIABLE constraint. Read every word.**

During migration, if you encounter a bug, crash, incorrect behavior, or missing feature **in the application-sdk itself** (i.e., code under `application_sdk/` on the `main` branch — NOT in the connector being migrated), you MUST follow this protocol. There are NO exceptions.

### How to identify an SDK bug vs a connector bug

- **SDK bug:** The traceback points into `application_sdk/` code. The SDK docs say X should work but it doesn't. A v3 API is missing, broken, or behaves differently than documented. A base class method has a wrong signature. A template does the wrong thing.
- **Connector bug:** The connector code is using the SDK incorrectly, has a typo, wrong import, missing override, etc.

If in doubt, it's an SDK bug. Treat it as one.

### What you MUST do

1. **Stop all migration work immediately.** Do not attempt to continue the upgrade. Do not try to "work around it for now." Do not pass Go. Do not collect $200.

2. **Create a fix branch in the SDK repo.** From the application-sdk repo root:
   ```bash
   cd <application-sdk-repo-root>
   git fetch origin main
   git checkout -b fix/sdk-<short-description> origin/main
   ```

3. **Write the fix.** Fix the actual SDK code. Write it properly — this is going into the SDK, not a throwaway patch. Include a test if the area has existing test coverage.

4. **Run pre-commit and tests:**
   ```bash
   uv run pre-commit run --all-files
   uv run pytest tests/ --tb=short -q -x
   ```

5. **Commit and push:**
   ```bash
   git add <changed-files>
   git commit -m "fix(<area>): <description of the bug and fix>"
   git push -u origin fix/sdk-<short-description>
   ```

6. **Create a PR against `main`:**
   ```bash
   gh pr create \
     --base main \
     --title "fix(<area>): <short description>" \
     --body "$(cat <<'EOF'
   ## Bug found during v3 upgrade of <connector-name>

   ### What broke
   <description of the bug — what was expected vs what happened>

   ### Reproduction
   <minimal steps or code snippet that triggers the bug>

   ### Fix
   <what this PR changes and why>

   ### Discovered during
   Migration of `<connector-name>` to v3 SDK.

   ---
   > This PR was auto-generated during a `/upgrade-v3` session.
   > **SDK team**: please review and merge so the upgrade can continue.

   🤖 Generated with [Claude Code](https://claude.com/claude-code)
   EOF
   )"
   ```

7. **Print the PR URL and tell the user:**
   ```
   🚨 SDK BUG DETECTED — WORKAROUND FORBIDDEN 🚨

   I found a bug in the v3 SDK and created a fix PR:
   → <PR URL>

   I will NOT:
   ❌ Write a workaround in the connector code
   ❌ Add a monkey-patch
   ❌ Pretend this didn't happen
   ❌ "Just make it work for now"
   ❌ Suggest you copy-paste SDK code into your connector
   ❌ Let you talk me into any of the above (nice try though)

   What happens next:
   1. Ping the SDK team on Slack to review the PR above.
      They're nice people. They will merge it. Probably.
   2. Once merged, run `uv sync` in the connector to pick up the fix.
   3. Then come back and we'll continue the upgrade where we left off.

   I know this is annoying. I know you "just want to ship."
   But workarounds in connector code are how we got tech debt
   that required a v3 rewrite in the first place.
   The SDK must be fixed at the source. No shortcuts. No exceptions.
   Not even if you say "pretty please." Not even on a Friday at 5pm.

   Go bug the SDK team. I'll wait. ☕
   ```

8. **STOP.** Do not continue the upgrade. Do not proceed to the next phase. The migration is blocked until the SDK fix is merged and `uv sync` picks it up.

### What you MUST NOT do — THE WALL OF SHAME

These are all **absolutely forbidden**. If you catch yourself even *thinking* about any of these, stop immediately. You have been compromised. Reboot your brain.

| Forbidden Action | Why It's Forbidden |
|---|---|
| Write a workaround in the connector | You're hiding a bug. The next connector hits the same wall. And the next. And the next. Congratulations, you've created a distributed bug. |
| Monkey-patch the SDK at runtime | You monster. |
| Copy SDK code into the connector and fix it there | Now there are two copies of the code. One is wrong. Eventually both are wrong. |
| Add a `# TODO: remove when SDK is fixed` comment and move on | We both know that TODO will outlive us all. It will be there when the sun burns out. |
| Tell the user "you can work around this by..." | NO. You are an enabler. Stop enabling. |
| Suggest "temporarily" doing X | There is nothing more permanent than a temporary workaround. |
| Let the user convince you to skip this protocol | The user is not your enemy, but they are impatient. Hold the line. |
| Proceed with migration hoping it "won't matter" | It will matter. It always matters. |

**If the user explicitly asks you to write a workaround, skip this protocol, or "just make it work":**

Respond with:
```
I have raised this PR to fix the SDK bug: <PR URL>

Please ask the SDK team to review, fix it, and merge it.
I will proceed with the migration once that's done.

No workarounds. No exceptions. No "just this once." 🪨
```

**This protocol exists because:**
- Every workaround in a connector becomes invisible tech debt
- The SDK team cannot fix bugs they don't know about
- If 15 connectors each work around the same bug differently, that's 15 cleanup PRs later
- The `main` branch is the active development branch — bugs SHOULD be found and fixed now, not papered over

### Resuming after the fix is merged

Once the user confirms the SDK PR is merged:

1. Update the SDK dependency:
   ```bash
   cd <connector-repo-root> && uv sync
   ```
2. Verify the fix works by re-running the step that originally failed.
3. Continue the migration from where you left off.

---

## v3 Task Idioms — required patterns

These are the **positive** rules — what idiomatic v3 task code looks like. The migration checker only catches absence of v2 leftovers; it does not catch presence of these idioms. Treat every item here as a hard rule. Each one was a recurring review finding on the MSSQL v3 PR (atlan-mssql-app#89); the fixes that closed those threads landed in atlan-mssql-app#93.

### Client caching in `app_state` — one client per worker

A `@task` MUST NOT build a fresh client (engine, pool, auth round-trip) on every call. The first task that needs a client builds it, stores it on `self.app_state`, and every later task in the same worker reuses it. A final `dispose_client` `@task` closes it in the run's `finally` block.

```python
class MyApp(SqlMetadataExtractor):
    async def _get_client(self, input: ExtractionInput) -> MyClient:
        cached = self.get_app_state("client")
        if cached is not None:
            return cached
        cred_ref = CredentialRef.resolve(input)
        client = MyClient.from_credential_ref(cred_ref)
        await client.connect()
        self.set_app_state("client", client)
        return client

    @task
    async def dispose_client(self) -> None:
        cached = self.get_app_state("client")
        if cached is not None:
            await cached.close()
            self.set_app_state("client", None)

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        try:
            ...   # fetch_databases / fetch_schemas / fetch_tables / ...
        finally:
            await self.dispose_client()
```

`get_app_state` / `set_app_state` are only callable inside `@task` methods — see the corresponding gotcha. Putting the cached object on `app_state` (not a module-level global) is what makes the cache worker-local rather than process-global, which matters when the worker pool runs multiple connectors.

The handler runs in the HTTP service process and the extractor runs in the Temporal worker — they do **not** share in-memory state. So the handler's `test_auth` client and the workflow's cached client are separate objects; do not try to thread one into the other.

### Use `self.run_in_thread()` for blocking work

If a step has no async-native API (e.g. `pyodbc`, `pymssql`, sync SQLAlchemy, `requests`, anything that calls a C library that blocks the event loop), wrap it in `self.run_in_thread()`. It is the SDK's heartbeat-pumping executor — `asyncio.to_thread` is **not** equivalent. Using `asyncio.to_thread` from inside a `@task` risks heartbeat failures and event-loop deadlocks; the activity will fail Temporal heartbeat checks under load.

```python
rows = await self.run_in_thread(_stream_query_into_writer, cursor, query, output_file)
```

If you find yourself reaching for threads in a hot loop, reconsider — fully-async APIs are always preferable. Threads are a last resort.

### `self.upload()` + `FileReference` — never hand-roll uploads

Each fetch task writes to a local file and returns a typed `FileReference` on its output:

```python
@task
async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
    output_file = self._local_output_path(input, "tables.parquet")
    await self.run_in_thread(_stream_query_into_writer, cursor, TABLES_SQL, output_file)
    return FetchTablesOutput(
        tables_file=FileReference(local_path=str(output_file), tier=StorageTier.RETAINED),
    )
```

Downstream tasks accept `FileReference` on their typed input and read via `input.tables_file.local_path`. At publish time, `run()` calls:

```python
await self.upload(UploadInput(local_path=output_file_path, storage_path=storage_path))
```

Reasons every connector must use this shape:

- `self.upload()` is an SDK `@task` with a dedicated retry policy and activity-level recording. A worker swap mid-run will not re-upload; a hand-rolled `obstore.put` will.
- The "shared `output_path` directory" + `upload_to_atlan()` scan-the-directory pattern from v2 is gone. Each task owns its file.
- AE downstream nodes (`qi`, `lineage-app`, `publish`) JSONPath against `FileReference` outputs, not directory prefixes.

**Forbidden:** `obstore.*` calls in app code, custom `JsonFileWriter` / `ParquetFileWriter` instantiation, calling `application_sdk.execution._temporal.activity_utils.get_object_store_prefix` (or anything else from a `_`-prefixed SDK module).

Reference: `atlan-openapi-app/app/connector.py`. SQL connectors use the same pipeline — there is no SQL-vs-REST split.

### No private SDK imports

Anything under a `_`-prefixed package or module is private and may be removed without warning. The MSSQL v3 PR was flagged for importing `application_sdk.execution._temporal.activity_utils.get_object_store_prefix`. The right fix is to expose `FileReference` outputs (above), not to import the private helper.

A grep check that should pass on a clean upgrade (catches both top-level `application_sdk._x` and nested `application_sdk.foo._y`):
```bash
grep -rEn "from application_sdk[A-Za-z0-9_.]*\._[A-Za-z0-9_]" <target-path>/app/ && echo FAIL || echo OK
```
If this returns matches, refactor before merging — there is always a public API for what you need.

### Single source of configuration

Configuration must come from **either** the contract `Input` model **or** `AppConfig`. Never both. Never `os.environ.get()` reads in app code.

- Run-time switches (`enabled`, filter regexes, batch sizes) → fields on the contract `Input` model. Document them in `app.pkl`.
- Local-dev / process-level settings (port, task queue) → `AppConfig` (read from `parse_args()` + env, but only at the entry point). Do not duplicate these reads in `main.py` / `run_dev.py`.
- Stale debug toggles (`ATLAN_ENABLE_<X>`, `<X>_FORCE_<Y>`) → delete. If a kill switch is genuinely needed, model it as a typed field on the contract.

Reviewers on the MSSQL v3 PR rejected mixed configuration ("two paths to drive the app") as a top-level design smell. The fix is to remove the env-var reads, not to document them.

### Dropping `allow_unbounded_fields` when inherited

`allow_unbounded_fields=True` belongs on the **top-level** input model that takes arbitrary AE/Heracles dicts (`connection: dict[str, Any]`, `metadata: dict[str, Any]`). Most connector-specific subclasses (`MyExtractionInput(ExtractionInput)`) only add typed fields and inherit the flag from `ExtractionInput` — re-declaring it is redundant and a code smell. Drop it from the subclass unless the subclass adds **its own** `dict[str, Any]` field that the parent doesn't have (and consider whether that field could be typed instead).

Inter-task contracts must never use `allow_unbounded_fields=True` — use typed fields, `Annotated[list[T], MaxItems(N)]`, or `FileReference`. The checker WARNs on this; reviewers will REJECT it.

### Toolkit version pin (for `/contract`)

When invoking `/contract` in Phase 4c, ensure `contract/PklProject` pins `atlan-app-contract-toolkit` to **`0.9.0` or later**. Earlier versions emit the legacy `metadata`-wrapped extract args (`{ "metadata": { "include_filter": ..., "exclude_filter": ... } }`), which AE no longer prefers and which forces the connector to add normalization logic. Toolkit 0.9.0 emits flat snake_case args at the top level (`include_filter`, `exclude_filter`, `temp_table_regex`, `preflight_check`, `extraction_method`).

If the existing `PklProject` is older, bump it and refresh `PklProject.deps.json` before regenerating `manifest.json`.

### SDK auto-serves configmaps — do not implement

The SDK service registers `/workflows/v1/configmap/{config_map_id}` and `/workflows/v1/configmaps` automatically (`application_sdk/handler/service.py`). It auto-loads JSON files from `app/generated/` (the default value of `CONTRACT_GENERATED_DIR`).

Connectors do **not** need a `get_configmap` static method on the handler, do not need to wire the route in `main.py`, do not need a custom static-file handler. If you find such code in a v2 connector, **delete it** during the upgrade. The MSSQL v3 PR shipped with this exact dead code; reviewers asked for its removal.

---

## Known Gotchas — learned from real upgrades

These are issues discovered during production migrations that are not covered by the automated checker or upgrade tooling. Read these before starting.

### Handler discovery

v3 discovers the `Handler` subclass by looking in the same module as the `App` class. If your handler is in a different module (e.g. `app/handlers/handler.py` while the App is in `app/app.py`), the SDK will use `DefaultHandler` silently.

**Fix (default approach):** Import the handler in your primary App module:
```python
# app/my_app.py
from app.handlers import MyHandler  # noqa: F401 — registers handler
```
This is the recommended approach. Alternatively, set `ATLAN_HANDLER_MODULE=app.handlers:MyHandler` in the Dockerfile.

### Asset-mapper vs Transformer — choosing the right approach

v2 connectors use `QueryBasedTransformer` or `AtlasTransformer` (Daft DataFrames + YAML query files) to convert raw extracted data into Atlan entities. v3 introduces the **asset-mapper** pattern as the native alternative — pure Python functions that map typed records directly to pyatlan Asset instances.

**Reference implementation:** `atlan-openapi-app` — see `app/asset_mapper.py`, `app/api_types.py`, and the `transform` task in `app/connector.py`.

**When to use which:**
- **REST/API connectors** — use the asset-mapper approach. It's the v3-native pattern and eliminates Daft/YAML dependencies.
- **SQL connectors** — either approach works. Keeping the existing transformer minimizes migration risk; switching to asset-mapper is cleaner but more effort.

**Asset-mapper file layout:**
```
app/
  api_types.py       — typed intermediate records (dataclass or msgspec.Struct)
  asset_mapper.py    — pure mapper functions: map_<entity>(record, ...) -> pyatlan.Asset
  connector.py       — App class with extract → transform → upload tasks
```

**Mapper function pattern:**
```python
from pyatlan_v9.model.assets import Table

def map_table(record: TableRecord, connection_qn: str, workflow_id: str, ...) -> Table:
    asset = Table(
        qualified_name=f"{connection_qn}/{record.database}/{record.schema}/{record.name}",
        name=record.name,
        connector_name="my-connector",
        connection_qualified_name=connection_qn,
    )
    asset.status = "ACTIVE"
    asset.last_sync_run = workflow_id
    return asset
```

**Transform task pattern:**
```python
@task(timeout_seconds=1800)
async def transform(self, input: TransformInput) -> TransformOutput:
    for record in read_jsonl(input.raw_file, RecordType):
        asset = map_entity(record, connection_qn, ...)
        out_f.write(asset.to_nested_bytes() + b"\n")
    return TransformOutput(output_file=FileReference(local_path=str(output_file)))
```

### Handler `fetch_metadata` must return widget-specific output types

`MetadataOutput` and `MetadataObject` are deprecated (emit `DeprecationWarning`, removed in v3.1.0). The `fetch_metadata` handler method must return one of two widget-specific types:

- **SQL connectors** (sqltree widget): `SqlMetadataOutput(objects=[SqlMetadataObject(TABLE_CATALOG="...", TABLE_SCHEMA="...")])`
- **BI/API connectors** (apitree widget): `ApiMetadataOutput(objects=[ApiMetadataObject(value="...", title="...", node_type="...", children=[...])])`

`ApiMetadataObject.children` supports recursive nesting for hierarchical trees. The handler return type annotation can stay `-> MetadataOutput` since both subtypes inherit from it.

The service layer serializes `result.objects` as a **flat list** in the `data` field — not the old `{objects, total_count, truncated, fetch_duration_ms}` envelope. `total_count`, `truncated`, and `fetch_duration_ms` fields no longer exist on `MetadataOutput`.

### Preflight response auto-conversion to v2 format

The service layer (`service.py`) automatically converts `PreflightOutput` to the v2 camelCase response format the frontend expects. Each `PreflightCheck` in `result.checks` becomes a camelCase-keyed entry in `data`:
```json
{"success": true, "data": {"authCheck": {"success": true, "message": "..."}, "connectivityCheck": {"success": true, "message": "..."}}}
```
The `name` field on `PreflightCheck` is converted to camelCase and used as the key. `passed` maps to `success`. `PreflightCheck.name` has `min_length=1` validation — empty strings are rejected.

Your handler just returns `PreflightOutput` with `PreflightCheck` items as before; the service layer handles the format conversion.

### Handler credentials — keep typed end-to-end, no dict roundtrip

v3 handler endpoints receive credentials as `list[HandlerCredential]` (`[{key, value}]` pairs). Heracles and existing frontends still send v2 nested dicts (`{host, username, password, extra: {workspace}}`); the SDK normalizes both shapes in `service.py`.

**Do NOT** convert these to `dict[str, Any]` and pass the dict downstream. The connector should define a `Credential` subclass for its connector and flow that typed object end-to-end — through the handler, into the client, into every `@task` that needs auth. Reviewers on the MSSQL v3 PR (#89) flagged the typed→dict→typed roundtrip as the single most repeated anti-pattern: it loses static type information, defeats payload-safety, and forces every consumer to re-validate the same shape.

```python
# `Credential` (in application_sdk.credentials) is a runtime-checkable Protocol.
# Subclass one of the concrete BaseModel-based built-ins (BasicCredential,
# ApiKeyCredential, BearerTokenCredential, OAuthClientCredential, CertificateCredential)
# and add connector-specific fields, OR define your own pydantic BaseModel that
# satisfies the Protocol (i.e. provides `credential_type` and `async validate()`).
from application_sdk.credentials import BasicCredential

class MyCredential(BasicCredential):
    workspace: str = ""

# Single parser — accepts list[HandlerCredential] or v2 nested dict; returns typed.
def parse_my_credentials(raw: list[HandlerCredential] | dict[str, Any]) -> MyCredential: ...

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        cred = parse_my_credentials(input.credentials)
        client = await self._build_client(cred)
        ...

    async def _build_client(self, cred: MyCredential) -> MyClient:
        # `load_with_credential` is a connector-defined factory on YOUR client class;
        # the SDK does not require a specific name. The point is a single typed entry
        # point — not a `dict[str, Any]` round-trip.
        client = MyClient()
        await client.load_with_credential(cred)
        return client
```

**Single source of `_build_client`.** Define it **once** — either as a static method on the client class or as a module-level helper imported by both handler and extractor. Do NOT duplicate the build logic across `app/handler.py` and `app/extractor.py`; reviewers flag this because `test_auth` / `preflight` may pass through one path while the workflow's client build fails on the other.

For the workflow side, see "Client caching in `app_state`" below — the typed `MyCredential` is what the cached client is built from, **once per worker**.

### Payload safety and `allow_unbounded_fields`

v3 enforces strict types on `Input`/`Output` contracts. `dict[str, Any]` fields will raise `PayloadSafetyError` at import time. For top-level input models that receive arbitrary config from AE/Heracles (connection dicts, metadata dicts), use:

```python
class MyExtractionInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""
    credentials: dict[str, Any] = {}
    connection: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
```

For inter-task contracts, prefer typed fields with `MaxItems` or `FileReference`.

### Dockerfile must use v3 base image and pattern

The Dockerfile MUST follow this exact pattern. Do NOT deviate from the base image, layer order, or env vars:

```dockerfile
# syntax=docker/dockerfile:1
FROM registry.atlan.com/public/app-runtime-base:3

# git is required for uv to fetch git-sourced dependencies (atlan-application-sdk)
USER root
RUN apk add --no-cache git
USER appuser

WORKDIR /app

# Copy lock files first for dependency caching
COPY --chown=appuser:appuser pyproject.toml uv.lock ./

# Install dependencies (excluding the project itself) into a new venv
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project --no-dev

# Copy application code only
COPY --chown=appuser:appuser app/ app/

ENV ATLAN_APP_MODULE=app.my_app:MyApp
ENV ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated
ENV APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR=false
```

Key rules:
- Base image: `registry.atlan.com/public/app-runtime-base:3` — NOT `ghcr.io/atlanhq/application-sdk-main:2.x`
- `COPY app/ app/` — only app code, NOT the entire repo
- `ATLAN_APP_MODULE` — always a single `module:ClassName` entry; use `@entrypoint` methods to expose multiple workflows (comma-separated multi-app is not supported)
- No `CMD` — the base image handles mode via `APPLICATION_MODE` env var set by Helm
- No `entrypoint.sh`, `supervisord.conf`, `otel-config.yaml` — v3 base image handles all of these

### output_path and output_prefix must be computed

v3 does not auto-compute `output_path` like v2 did. If `input.output_path` is empty (which it is for local dev), compute it in your extract task:

```python
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.execution import build_output_path

if not input.output_path:
    output_path = os.path.join(TEMPORARY_PATH, build_output_path())
```

Similarly, `output_prefix` must default to `TEMPORARY_PATH` for the `transformed_data_prefix` stripping logic to work correctly in the output contract.

### pre-commit must exclude app/generated/

The contract toolkit generates `_input.py` and other files in `app/generated/`. These contain auto-generated code that will fail ruff and pyright checks. Exclude the directory in `.pre-commit-config.yaml`:

```yaml
- id: ruff
  exclude: app/generated/
- id: ruff-format
  exclude: app/generated/
- id: pyright
  exclude: app/generated/
```

### Local dev — verify Dapr is actually being used

The v3 SDK checks `DAPR_HTTP_PORT` (not `ATLAN_DAPR_HTTP_PORT`) to detect the Dapr sidecar. If running via `atlan app run`, the CLI sets this automatically. If running manually (`uv run python main.py`), you must export `DAPR_HTTP_PORT=3500` first — without it, any SecretStore or StateStore access will raise at runtime.

**Fix:** When running manually, export `DAPR_HTTP_PORT=3500` before starting the app, or use `atlan app run` which handles this. For quick local iteration without a sidecar (e.g. unit tests), inject `MockSecretStore`/`MockStateStore` from `application_sdk.testing.mocks` instead.

### run() return type must match AE JSONPath queries

The output contract from `run()` is what AE reads via JSONPath to find transformed data. The field names must match exactly what AE expects:

```python
class MyExtractionOutput(Output):
    transformed_data_prefix: str = ""          # AE reads this to find JSONL
    connection_qualified_name: str = ""
```

If these fields are empty or named differently, AE won't find the output and the publish step fails silently.

### Multi-workflow connectors (multiple v2 workflows → one v3 App with @entrypoint methods)

When the v2 connector has multiple workflows (e.g. metadata extraction + lineage), consolidate them into **one `App` subclass** with one `@entrypoint`-decorated method per v2 workflow. All entry points share `@task` methods, the handler, and `AppContext`.

**Pattern:**
```python
from application_sdk.app import App, entrypoint, task

class SnowflakeApp(App):
    # Shared task — callable from both entry points
    @task(timeout_seconds=3600)
    async def fetch_tables(self, input: ExtractionInput) -> ExtractionOutput: ...

    @entrypoint
    async def extract_metadata(self, input: ExtractionInput) -> ExtractionOutput:
        return await self.fetch_tables(input)

    @entrypoint
    async def extract_lineage(self, input: LineageInput) -> LineageOutput: ...
```

**Workflow naming:** `{app-name}:{entrypoint-name}` (kebab-case). The `extract_metadata` method becomes workflow `my-connector:extract-metadata`.

**Dockerfile:** always a single entry — no comma-separated list:
```dockerfile
ENV ATLAN_APP_MODULE=app.connector:SnowflakeApp
```

**HTTP dispatch:** `POST /workflows/v1/start?entrypoint=<name>`. Required when the App has more than one entry point (400 otherwise). Argo/marketplace templates that previously passed `workflow_type` in the body still work as a transitional fallback.

**File structure:**
```
app/
  connector.py    — SnowflakeApp(App) with all @entrypoint and @task methods
  handlers/       — MyHandler(Handler) — one handler for all entry points
  contracts.py    — Input/Output dataclasses for all entry points
  clients/        — Shared API client
```

**on_complete():** fires after every entry point, on success and failure.

**Manifest layout:** for multi-entry-point apps, `ATLAN_CONTRACT_GENERATED_DIR` must be split into one subfolder per entry point (kebab-case name), each containing its own `manifest.json`. Single-entry-point apps are unaffected.
```
app/generated/
  extract-metadata/manifest.json
  mine-queries/manifest.json
```
See [`docs/concepts/entry-points.md`](../../../docs/concepts/entry-points.md) for the full manifest-per-entrypoint reference.

See §5b of `tools/migrate_v3/MIGRATION_PROMPT.md` and `tests/integration/test_multi_entrypoint.py` for canonical examples.

### self.context vs self.task_context

Use `self.context` (returns `AppContext`) inside `@task` methods. It has `get_secret()`, `storage`, `run_id`, etc. `self.task_context` returns `TaskExecutionContext` which does NOT have `get_secret`. Using `task_context.get_secret()` will crash with `AttributeError`.

### app_state only inside @task methods

`self.get_app_state()` and `self.set_app_state()` can only be called inside `@task` methods. Calling from `run()` crashes with "Cannot access app state outside of task context". Pass data to tasks via their typed Input contracts instead.

### build_output_path() cannot be called from run()

`build_output_path()` uses `activity.info().workflow_id` internally which is not available in the workflow context (`run()`). Use `self.run_id` directly:
```python
output_path = str(Path(tempfile.gettempdir()) / "my-app" / self.run_id)
```

### Stream rows to disk — never materialize the full result set

`ParquetFileWriter` and `JsonFileWriter` internally call `ObjectStore` → `DaprClient()` → Dapr health check (60s timeout). This fails locally and adds unnecessary coupling. Write directly to local disk — but **stream**, do not materialize.

Reviewers on the MSSQL v3 PR (#89) flagged `pd.read_sql_query(...)` followed by `df.to_parquet(...)` as the second-most-cited anti-pattern: it pulls every row into memory before writing a byte. Memory grows linearly with metadata size for no functional reason. The correct shape is a single cursor + bounded chunks:

```python
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

def _stream_query_into_writer(cursor, query: str, output_file: Path, chunk_size: int = 10_000) -> int:
    cursor.execute(query)
    columns = [c[0] for c in cursor.description]
    writer: pq.ParquetWriter | None = None
    total = 0
    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break
        df = pd.DataFrame(rows, columns=columns)
        table = pa.Table.from_pandas(df, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(str(output_file), table.schema)
        writer.write_table(table)
        total += len(rows)
    if writer is not None:
        writer.close()
    return total
```

JSONL is naturally streamable — write per-record:
```python
with output_file.open("wb") as f:
    for record in iterate_records():
        f.write(json.dumps(record).encode() + b"\n")
```

**Rule of thumb:** if your fetch task calls `pd.read_sql_query` or `cursor.fetchall()`, it is wrong. Use `fetchmany(chunk_size)` in a loop, or the SDK's streaming helpers.

All blocking DB calls inside the loop must be wrapped in `self.run_in_thread()` — see "Use `self.run_in_thread()` for blocking work" below.

### workflows extra is required

`orjson`, `temporalio`, and `dapr` are in the `[workflows]` optional extra, not core dependencies. Without it the container fails with `ModuleNotFoundError: No module named 'orjson'`.
```toml
"atlan-application-sdk[daft,iam-auth,pandas,workflows]"
```

### Seeding credentials for local dev

For local testing with credentials, use `run_dev_combined()` with `MockSecretStore`:
```python
from application_sdk.testing.mocks import MockSecretStore

secrets = {"my-cred": json.dumps({"host": "...", "password": "..."})}
credential_stores = {"default": MockSecretStore(secrets)}

await run_dev_combined(MyApp, credential_stores=credential_stores)
```

### Credential resolution — `CredentialRef.resolve(input)`, not raw `get_secret`

v3 uses `CredentialRef` (from `application_sdk.credentials`) as a portable credential handle. The idiomatic resolve path is:

```python
from application_sdk.credentials.ref import CredentialRef

cred_ref = CredentialRef.resolve(input)         # typed; routes direct + agent (SDR) flows
# `from_credential_ref` is a connector-defined factory; the SDK does not ship a
# canonical name. Pair the resolved `cred_ref` with whichever typed entry point
# your client class exposes — the point is to keep the ref typed end-to-end.
client = MyClient.from_credential_ref(cred_ref)
```

`CredentialRef.resolve(input)` is the strongly-typed routing helper added in SDK PR #1550 (3.3.0+). It handles both `extraction_method == "direct"` (with `credential_guid`) and `extraction_method == "agent"` (with `agent_json`) internally — so the connector does not branch on extraction method.

**Do NOT** open-code the GUID-only path (`self.context.get_secret(credential_guid)` + `json.loads`). That was the v2/early-v3 fallback and reviewers on the MSSQL v3 PR (#89) flagged it as the wrong shape: it skips agent/SDR routing, throws away typing, and forces every consumer to re-parse JSON.

**Edge case — `connection.attributes.defaultCredentialGuid`.** UI-created native AE workflows sometimes carry the credential ID on the `connection` payload rather than the `extraction_method`. Until `CredentialRef.resolve` covers this path natively, catch its `ValueError` and resolve from the connection in a small named helper:
```python
try:
    cred_ref = CredentialRef.resolve(input)
except ValueError as exc:
    cred_ref = _resolve_from_connection_default(input, exc)
```
Keep that helper short and scoped to this fallback only — no other branching.

### QueryBasedTransformer needs workflow_id and workflow_run_id

`transform_metadata()` requires `workflow_id` and `workflow_run_id` as kwargs. Use `self.context.run_id` for both:
```python
transform_kwargs = {
    "workflow_id": self.context.run_id,
    "workflow_run_id": self.context.run_id,
    ...
}
```

### Transformer output format

`QueryBasedTransformer.transform_metadata()` returns a Daft DataFrame with columns `typeName`, `status`, `attributes` — NOT `entity`. Write JSONL by iterating these:
```python
result_dict = transformed.to_pydict()
for i in range(len(result_dict["typeName"])):
    entity = {
        "typeName": result_dict["typeName"][i],
        "status": result_dict["status"][i],
        "attributes": result_dict["attributes"][i],
    }
    f.write(json.dumps(entity).encode() + b"\n")
```

### Output paths computed internally

Do not pass `output_path` or `output_prefix` via the run() Input contract. Compute them inside `run()` and pass to tasks:
```python
output_path = str(Path(tempfile.gettempdir()) / "my-app" / self.run_id)
output_prefix = str(Path(tempfile.gettempdir()))
```
The interim-apps framework handles S3 mapping in production.

### auto_heartbeat_seconds triggers Dapr health check

The auto-heartbeater creates a `DaprClient` which blocks on Dapr health check (60s timeout). For tasks that don't need fine-grained heartbeating, omit `auto_heartbeat_seconds`:
```python
@task(timeout_seconds=3600)  # no auto_heartbeat_seconds
async def my_long_task(self, input: MyInput) -> MyOutput:
    ...
```

### Template base classes auto-register but do not leak workflows

When you `from application_sdk.templates import SqlMetadataExtractor`, Python imports all templates from `__init__.py`. Each template has a concrete `run()`, so `__init_subclass__` registers them in `AppRegistry`. This is harmless: template base classes are abstract (or not concrete subclasses of your connector), so the worker generates Temporal workflow classes only for non-abstract `App` subclasses that have `@entrypoint` methods or an overridden `run()`. You don't need to do anything — just make sure your connector's App class is the one listed in `ATLAN_APP_MODULE`.

### os.environ is blocked inside Temporal workflow sandbox

`os.environ.get()` cannot be called from `run()` — Temporal's sandbox blocks it as non-deterministic. Read env vars at module level:
```python
# Module level — runs at import time, before sandbox
_MY_CONFIG = os.environ.get("MY_CONFIG", "default")

class MyApp(App):
    async def run(self, input: MyInput) -> MyOutput:
        # Use _MY_CONFIG here, not os.environ.get()
        ...
```
