---
name: migrate-v3
description: Migrate a connector repo from application-sdk v2 to v3 — runs the import rewriter, performs AI-assisted structural refactoring, and validates the result with the migration checker.
argument-hint: "<path-to-connector-repo>"
---

# /migrate-v3

Performs a complete v2 → v3 migration of an application-sdk connector.

**Must be run from the application-sdk repo root** (so the migration tooling and docs are reachable).

## Usage

```
/migrate-v3 ../my-connector/src
/migrate-v3 /absolute/path/to/connector/
```

---

## Phase 0 — Setup and validation

1. Parse `$ARGUMENTS` to get the target path. If no argument is given, stop and ask the user for one.
2. Confirm the target path exists. If it does not, stop and report the error.
3. Confirm you are running from within the application-sdk repo by checking that `tools/migrate_v3/rewrite_imports.py` exists. If it does not, stop and tell the user to run this skill from the application-sdk repo root.
4. **Check the connector's SDK dependency.** Read the connector's `pyproject.toml` and look for `atlan-application-sdk` in the dependencies. Until v3 is released on PyPI the connector must use the git source:
   ```toml
   [tool.uv.sources]
   atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "refactor-v3" }
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

---

## Phase 2 — Structural migration

Read the checker output from Phase 0 and the structure of the connector code to determine what structural work is needed.

### 2a — Identify connector type

Examine the source files in the target path (exclude test files from this analysis):

- Look for classes inheriting from `BaseSQLMetadataExtractionWorkflow` / `BaseSQLMetadataExtractionActivities` → SQL metadata extractor (§2a of MIGRATION_PROMPT.md)
- Look for classes inheriting from `SQLQueryExtractionWorkflow` / `SQLQueryExtractionActivities` → SQL query extractor (§2b)
- Look for classes inheriting from `IncrementalSQLMetadataExtractionWorkflow` → Incremental SQL extractor (§2c)
- Look for any other `WorkflowInterface` / `ActivitiesInterface` subclasses → Custom App (§3)
- In all cases: identify the handler class (§4) and the entry point (§5)

> **Tip — auto-detect the connector type:**
> ```bash
> uv run python -m tools.migrate_v3.check_migration --classify <target-path>
> ```
> This runs the F3 fingerprinter before the check pass and prints the detected
> connector type with confidence score and evidence.

### 2b — Apply structural changes

> **Incremental validation**: Run `check_migration` after each sub-step below.
> The checker output shows specific remaining FAILs — use it to guide the next step.

Follow the exact checklists in `tools/migrate_v3/MIGRATION_PROMPT.md` for the connector type(s) identified above.

**Hard constraint — tests are completely out of bounds for structural changes:**

- You MUST NOT modify test method bodies, assertions, fixtures, mock setup, or test data in any file under any directory whose name contains `test` or starts with `test_`.
- You MUST NOT add, remove, or rewrite test cases.
- You MUST NOT change the logic of any existing test.
- The only change permitted in test files is the mechanical import rewrite already performed in Phase 1. If a test file needs structural changes to compile (e.g. it directly instantiates a v2 class that no longer exists), add a `# TODO(v3-migration): update test to use v3 API` comment and leave the test body unchanged. The user will update tests manually after verifying the migration is correct.

**Hard constraint — handler method signatures:**

- Handler methods (`test_auth`, `preflight_check`, `fetch_metadata`) MUST use typed contract parameters. Do NOT use `*args` or `**kwargs`.
- Correct: `async def test_auth(self, input: AuthInput) -> AuthOutput:`
- Forbidden: `async def test_auth(self, *args, **kwargs):`
- The checker will FAIL if `*args`/`**kwargs` appear in a Handler subclass method.

**Hard constraint — connector contracts must not use `allow_unbounded_fields=True`:**

- This escape hatch is reserved for SDK-internal types only.
- If a connector contract has an unbounded list, use `Annotated[list[T], MaxItems(N)]` or `FileReference` instead.
- The checker will FAIL if `allow_unbounded_fields=True` appears in connector code.

Apply changes in this order:

1. **App class** — merge Workflow + Activities into the appropriate template subclass with `@task` methods. Preserve all SQL query strings and business logic verbatim.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-v2-decorators`, `no-execute-activity-method`) before proceeding.

2. **Handler** — update base class, method signatures (typed contracts, no `**kwargs`), remove `load()`.

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

After completing the structural migration in 2b, consolidate the v2 directory layout. v2 connectors split logic across `app/activities/` and `app/workflows/`; v3 uses a single flat file.

1. Identify the main App class file (typically `app/activities/<name>.py`).
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
   c. For any **symbol names** that also changed (e.g. `AnaplanMetadataExtractionActivities` → `AnaplanApp`), manually update the import line's symbol name in each affected test file AND add a comment at the top of that file: `# TODO(v3-migration): update references from OldClass to NewClass in test bodies`. Do NOT modify test bodies, assertions, fixtures, or mocks.
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
- If the failure is due to a production code issue introduced during migration (e.g. wrong method signature, missing attribute), fix the production code.
- If the failure requires understanding test intent or rewriting test logic, do NOT fix it. Add it to the manual follow-up list.

### Phase 4b — E2E test generation

After the test suite run, check whether the connector has e2e tests using the v2 `BaseTest` / `TestInterface` pattern:

1. Search for files under `tests/e2e/` (or `tests/integration/`) that import `BaseTest` or `TestInterface`.
2. If found, **read the original v2 e2e test file completely**. List every test method and what it asserts before writing a single line of the new file.
3. Generate a **new** equivalent e2e test file using the v3 `application_sdk.testing.e2e` API (§9 of MIGRATION_PROMPT.md):
   - For **each** test method in the original, generate a corresponding `async def test_xxx(deployed_app)` function. The generated file MUST have at least as many test functions as the original has test methods.
   - Extract actual payload values from the original (hardcoded dicts, `default_payload()` bodies, connection IDs) — do **not** substitute placeholder values like `"test-connection"` if the original has real values.
   - If an assertion checks response fields whose format changed (e.g. `result['authenticationCheck']`), keep the assertion but add `# TODO(v3-migration): response format changed — update field names`.
   - Use the `AppConfig` fixture with real values derived from the connector's `pyproject.toml` (`name`, `tool.poetry.name`, or Helm chart values) — not generic placeholders.
4. Place the new file alongside the original, named `tests/e2e/test_<connector_name>_v3.py`.
5. Add `# TODO(v3-migration): human must validate this test is equivalent to the original` at the top of the new file.
6. Do NOT delete or modify the original test file.
7. Add the new test file to the manual follow-up list so the user knows to validate it.

If the connector has no v2-style e2e tests, skip this step.

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
- fetch_metadata: returns MetadataOutput (flat list) — was hierarchical [{value, title, children}]
- preflight_check: returns PreflightOutput — was {authenticationCheck, hostCheck, permissionsCheck}

### Manual follow-up required
<bulleted list of anything the AI skipped due to the test constraint or ambiguity>
```

Remind the user:
- Run `uv run pre-commit run --all-files` in the connector repo before committing.
- Review all `# TODO(v3-migration)` comments — each one marks a location that needs human verification.
- The typed `Input`/`Output` dataclasses for custom `@task` methods should be defined (see §7 of MIGRATION_PROMPT.md) — these were not auto-generated.
- If an e2e test was generated in Phase 4b, validate that it is logically equivalent to the original before deleting the old file.
