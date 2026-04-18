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
4. **Check the connector's SDK dependency.** Read the connector's `pyproject.toml` and look for `atlan-application-sdk` in the dependencies. Until v3 is released on PyPI the connector must use the git source:
   ```toml
   [tool.uv.sources]
   atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "main" }
   ```
   If it still points to a v2 PyPI release (e.g. `atlan-application-sdk>=2.x`), add the `[tool.uv.sources]` block above and run `uv sync` in the connector repo before continuing. If it already has this source override or references a v3+ PyPI release, proceed.

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
- Ask the user which approach they prefer:
  1. **Keep existing transformer** — preserve `QueryBasedTransformer`/`AtlasTransformer` usage inside `transform_data()`. Less migration effort, leverages existing YAML query files.
  2. **Asset-mapper approach** — replace the transformer with pure Python mapper functions that take typed records and return pyatlan Asset instances directly. More upfront work but eliminates Daft DataFrame and YAML query file dependencies.
- If the user has no preference, keep the existing transformer to minimize migration risk.

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
   - Use the `AppConfig` fixture with real values derived from the connector's `pyproject.toml` (`name`, `tool.poetry.name`, or Helm chart values) — not generic placeholders.
4. Place the new file alongside the original, named `tests/e2e/test_<connector_name>_v3.py`.
5. Add `# TODO(upgrade-v3): human must validate this test is equivalent to the original` at the top of the new file.
6. Do NOT delete or modify the original test file.
7. Add the new test file to the manual follow-up list so the user knows to validate it.

If the connector has no v2-style e2e tests, skip this step.

### Phase 4c — Contract generation

After tests pass and e2e tests are generated, the app needs a PKL contract that generates workflow config, credential config, AE manifest, and typed Input class. Use the **`/contract` skill** from [`atlanhq/connector-os`](https://github.com/atlanhq/connector-os/blob/main/skills/contract/SKILL.md) to create or migrate the contract.

1. Check if the connector already has a `contract/` directory with `app.pkl`:
   ```bash
   ls <target-path>/contract/app.pkl 2>/dev/null && echo "EXISTS" || echo "MISSING"
   ```

2. **If `contract/app.pkl` exists:** Ask the user whether they want to update it for v3 compatibility (e.g. bump toolkit version, regenerate artifacts). If yes, invoke `/contract` with the update action.

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
from pyatlan.model.assets import Table

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

### Handler credentials — v2 nested dict vs v3 list format

v3 handler endpoints receive credentials as `list[HandlerCredential]` (`[{key, value}]` pairs). Heracles and existing frontends send v2 nested dicts (`{host, username, password, extra: {workspace}}`). The SDK normalizes v2 format to v3 automatically in `service.py`, but your handler's `_build_client` helper must reconstruct the nested dict from `input.credentials`:

```python
async def _build_client(credentials: list[HandlerCredential]) -> MyClient:
    cred_dict = {}
    extra = {}
    for cred in credentials:
        if cred.key.startswith("extra."):
            extra[cred.key[len("extra."):]] = cred.value
        else:
            cred_dict[cred.key] = cred.value
    if extra:
        cred_dict["extra"] = extra
    client = MyClient()
    await client.load(credentials=cred_dict)
    return client
```

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
FROM registry.atlan.com/public/app-runtime-base:main-latest

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
- Base image: `registry.atlan.com/public/app-runtime-base:main-latest` — NOT `ghcr.io/atlanhq/application-sdk-main:2.x`
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

### Write to local disk, not ParquetFileWriter/JsonFileWriter

`ParquetFileWriter` and `JsonFileWriter` internally call `ObjectStore` → `DaprClient()` → Dapr health check (60s timeout). This fails locally and adds unnecessary coupling. Write directly to local disk:
```python
# Parquet
pandas_df.to_parquet(str(output_file))

# JSONL
with output_file.open("wb") as f:
    f.write(json.dumps(entity).encode() + b"\n")
```

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

### Credential resolution only via credential_guid

No `credentials` dict fallback. Always resolve from secret store inside `@task` methods:
```python
creds_json = await self.context.get_secret(credential_guid)
creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
```

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
