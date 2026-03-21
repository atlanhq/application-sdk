# Plan: Phase 2 Migration Automation (A2, A6, A7, A8, B2, D1, E1)

## Context

Phase 1 implemented the highest-ROI codemods (A1/A3/A4/A5) plus the checker enhancements (E5), incremental checker feedback (D4), and connector type fingerprinting (F3). These handle bulk FAIL items deterministically.

Phase 2 completes the codemod pipeline (A2/A6/A7), wraps it in a composable CLI command (A8), adds post-LLM cleanup automation (B2), restructures the skill to run codemods before the LLM (D1), and adds snapshot regression tests (E1).

Note: **B1** (pre-processing context extraction) was deferred to Phase 3.

### Real Connector Patterns Observed (4 connector repos)

**Connectors studied:** atlan-anaplan-app, atlan-athena-app, atlan-publish-app, atlan-schema-registry-app

**A2 — `execute_activity_method` call variants:**

1. **Instance-method + `args=` kwarg** (Anaplan, Schema Registry, most connectors):
   ```python
   activities_instance = self.activities_cls()
   result = await workflow.execute_activity_method(
       activities_instance.get_workflow_args,
       args=[workflow_config],
       retry_policy=retry_policy,
       start_to_close_timeout=self.default_start_to_close_timeout,
       heartbeat_timeout=self.default_heartbeat_timeout,
       summary="Get workflow args",   # optional kwarg
   )
   ```
   Target: `result = await self.get_workflow_args(workflow_config)`

2. **Class-method + positional arg** (Athena — first call only):
   ```python
   workflow_args = await workflow.execute_activity_method(
       AthenaActivities.get_workflow_args,
       workflow_config,          # positional, NOT in args=[]
       retry_policy=...,
       ...
   )
   ```
   Target: `workflow_args = await self.get_workflow_args(workflow_config)`

3. **Dynamic dispatch via `getattr`** (Anaplan loop body — NOT automatable):
   ```python
   activity_method = getattr(activities_instance, activity_method_name, None)
   extraction_statistics = await workflow.execute_activity_method(
       activity_method, args=[workflow_args], ...
   )
   ```
   Action: Insert `# TODO(migrate-v3): dynamic dispatch — rewrite manually` comment above the block; leave code unchanged.

**A6 — `activities_cls` and `get_activities` variants:**

- Annotated class attribute: `activities_cls: Type[XxxActivities] = XxxActivities`
- Plain class attribute: `activities_cls = AthenaActivities`
- `activities_instance = self.activities_cls()` inside `run()` body — delete this line
- `get_activities(activities: XxxActivities) -> list` static method — delete entirely
- Note: `get_asset_extraction_activities()` and other connector-specific helpers are NOT `get_activities` variants and must NOT be deleted

**A7 — Entry point patterns across 4 connectors:**

Standard form (automatable, 3 of 4 connectors):
```python
async def main():
    application = BaseApplication(name=NAME, client_class=Client, handler_class=Handler)
    await application.setup_workflow([(WF, ACT)])
    await application.start(workflow_class=WF, has_configmap=True)
```
→ Replace with:
```python
async def main():
    await run_dev_combined(AppClass, handler_class=Handler)
```

Partial/complex form (Athena — NOT fully automatable):
```python
async def main():
    application = BaseApplication(name=APPLICATION_NAME)
    await application.setup_workflow(...)
    has_worker = APPLICATION_MODE in (...)
    has_server = APPLICATION_MODE in (...)
    if has_worker:
        await application.start_worker(daemon=has_server)
    if has_server:
        await application.setup_server(...)
        application.server.app.include_router(manifest_router)
        await application.start_server()
```
Action: Insert `# TODO(migrate-v3): custom server setup — rewrite manually` comment; leave code unchanged.

Complexity: A7 needs to know the merged App class name. Since the workflow/activities merge hasn't happened when A7 runs, A7 should substitute a `# TODO: replace with your App class` placeholder.

---

## Architecture Decision

Phase 2 stays with the same raw `CSTTransformer` pattern established in Phase 1. The `CodemodPipeline` infrastructure already handles chaining and import injection.

**A8 produces `tools/migrate_v3/run_codemods.py`** — both a standalone CLI module (`python -m tools.migrate_v3.run_codemods <path>`) and a programmatic API (`run_codemods(path, connector_type=None) -> RunResult`). The skill (D1) calls this programmatic API.

**Pipeline order:** A1 → A3 → A4 → A5 → A2 → A6 → A7
Rationale: Decorators and signatures first (A1/A3/A4/A5 — already implemented), then execute_activity_method calls (A2 depends on activities_instance removal that A6 does NOT do — A2 must run before A6 removes `activities_instance` line), then plumbing cleanup (A6), then entry point (A7).

Wait — A2 reads `activities_instance.method_name` to extract the method name, so A2 must run BEFORE A6 deletes `activities_instance = self.activities_cls()`. Order is correct: A2 → A6.

---

## File Layout

### New files
```
tools/migrate_v3/
    run_codemods.py              # A8: CLI + programmatic API
    codemods/
        rewrite_activity_calls.py   # A2
        remove_activities_cls.py    # A6
        rewrite_entry_point.py      # A7

tests/unit/tools/
    test_codemods/
        test_rewrite_activity_calls.py   # A2
        test_remove_activities_cls.py    # A6
        test_rewrite_entry_point.py      # A7
```

### Modified files
```
.claude/skills/migrate-v3/SKILL.md      # D1: run codemods before LLM step
```

---

## Implementation Order

### Step 1: A2 — `rewrite_activity_calls.py`

**Detects and transforms `workflow.execute_activity_method(...)` calls.**

Detection: Look for `Attribute(value=Name("workflow"), attr=Name("execute_activity_method"))` as the `func` of a `Call` node.

**Extraction logic:**

From the first argument, extract the method name:
- If arg is `Attribute(value=Name(x), attr=Name(method_name))` — both `instance.method` and `Class.method` forms
- If arg is `Attribute(value=Attribute(...), ...)` — skip (deeply nested, emit TODO)
- If arg is `Name(...)` — extracted via a prior `getattr` call → emit TODO comment

From remaining args, extract the data argument:
- If keyword arg `args=[single_item]` present → `single_item` is the data
- If second positional arg present → that is the data
- If neither → use `workflow_args` as a fallback (and note the uncertainty)

**Rewrite:**
```python
await workflow.execute_activity_method(
    activities_instance.fetch_databases,
    args=[workflow_args],
    retry_policy=...,
    start_to_close_timeout=...,
    heartbeat_timeout=...,
    summary=...,
)
```
→
```python
await self.fetch_databases(workflow_args)
```

Strip all kwargs: `retry_policy`, `start_to_close_timeout`, `heartbeat_timeout`, `summary`, `schedule_to_close_timeout`, `task_queue`. These are handled by `@task` in v3.

**Assignment preservation:** `result = await ...` stays as `result = await self.method(arg)`.

**TODO emission for dynamic dispatch:** When the method arg is a `Name` (variable, not attribute), replace the entire `await workflow.execute_activity_method(...)` call with:
```python
# TODO(migrate-v3): dynamic dispatch — cannot auto-rewrite; replace with await self.method(input)
await workflow.execute_activity_method(variable, args=[workflow_args], ...)
```
(Leave original call intact, add comment above it.)

**`self.changes` entries:** `"Rewrote execute_activity_method(activities_instance.{method}) → await self.{method}(arg)"`, or `"Skipped dynamic dispatch: {lineno}"`.

**Tests (~10 cases):**
- Instance method + `args=` kwarg → `await self.method(arg)`
- Class method + positional arg → `await self.method(arg)`
- Assignment preserved: `result = await ...`
- Strips retry_policy, timeout, heartbeat kwargs
- Strips `summary=` kwarg
- Multiple calls in one `run()` body — all rewritten
- Dynamic dispatch via `getattr` — TODO comment inserted, code left intact
- No-op: already `await self.method(...)` — unchanged
- Nested call as arg — handled without crashing
- `schedule_to_close_timeout` also stripped

### Step 2: A6 — `remove_activities_cls.py`

**Removes v2-specific class-level plumbing:**

Three deletions:
1. **Class attribute `activities_cls`**: Remove `FunctionDef`... no, this is a `SimpleStatementLine` containing `AnnAssign` or `Assign` where the target name is `activities_cls`. Remove at class body level.
2. **Static method `get_activities`**: Remove the `FunctionDef` where `name.value == "get_activities"` and it has a `@staticmethod` decorator. Also handle the case where it's just a `staticmethod` without the decorator (i.e., `get_activities` as a `@staticmethod` in the base class overridden here).
3. **`activities_instance` assignment line**: Remove `SimpleStatementLine` containing `activities_instance = self.activities_cls()` or `activities_instance: XxxActivities = self.activities_cls()` inside method bodies.

**Scope:** Only act inside classes that look like v2 workflow classes (have `activities_cls` attribute OR extend a known workflow base). Use conservative heuristic: if any of the three deletion targets is found, apply all three.

**Safety:** Do NOT delete:
- `get_asset_extraction_activities()` (different name — exact match only)
- Any `activities_cls`-named variable in a non-class-attribute position (e.g., local variable in a function)
- `activities` parameter in method signatures

**`self.changes` entries:** `"Removed activities_cls class attribute"`, `"Removed get_activities() static method"`, `"Removed activities_instance = self.activities_cls() line"`.

**Tests (~8 cases):**
- Remove plain `activities_cls = Xxx` class attribute
- Remove annotated `activities_cls: Type[Xxx] = Xxx` class attribute
- Remove `get_activities(activities: Xxx) -> list` static method
- Remove `activities_instance = self.activities_cls()` line inside run()
- All three in one class → all removed
- `get_asset_extraction_activities()` NOT removed (exact name match only)
- `application_name: str = APP_NAME` NOT removed (different attribute name)
- No-op on already-clean class

### Step 3: A7 — `rewrite_entry_point.py`

**Rewrites `main.py` entry point from `BaseApplication` pattern to `run_dev_combined`.**

**Standard pattern detection** (all four of these lines must be present):
1. `application = BaseApplication(name=..., ...)` or `app = BaseApplication(...)` assignment
2. `await application.setup_workflow(...)` call
3. `await application.start(...)` call (NOT `start_worker` or `start_server` — that's the complex form)

**Extraction from the `BaseApplication(...)` call:**
- `name=` kwarg → note for logging (not used in output)
- `client_class=` kwarg → extract class name (e.g., `AnaplanApiClient`)
- `handler_class=` kwarg → extract class name (e.g., `AnaplanHandler`)

**Extraction from `setup_workflow(...)` call:**
- `workflow_and_activities_classes=[(WF, ACT)]` → extract `WF` and `ACT` class names
- Also handle positional list form

**App class name derivation:** Infer from the root directory name using the `atlan-{app-name}-app` convention:
- Strip `atlan-` prefix (if present) and `-app` suffix (if present)
- Split remaining on `-`, capitalize each segment, join → PascalCase
- Append `App`
- Examples: `atlan-anaplan-app` → `AnaplanApp`, `atlan-schema-registry-app` → `SchemaRegistryApp`
- Fallback: `MyApp` if the directory name does not match the convention

**Output — replace the `main()` function body** with:
```python
async def main():
    await run_dev_combined(AnaplanApp, handler_class=AnaplanHandler)
```
Where `handler_class=` is omitted if not present in the original `BaseApplication(...)` call.

**Complex pattern detection** (any of these indicates non-standard flow):
- `start_worker(` present in function body
- `start_server(` present in function body
- `setup_server(` present in function body
- Multiple `setup_workflow` calls
- Any `.include_router(` or `.app.` attribute access

When detected: insert `# TODO(migrate-v3): custom entry point — rewrite manually` as the first line of `main()`, leave body intact.

**Import side effects:**
- Add `("application_sdk.main", "run_dev_combined")` to `imports_to_add`
- Add `("application_sdk.application", "BaseApplication")` to `imports_to_remove`
- Add `("application_sdk.constants", "APPLICATION_NAME")` etc. where applicable (from imports_to_remove)

**`self.changes` entries:** `"Rewrote BaseApplication entry point → run_dev_combined(MyApp, ...)"` or `"Skipped complex entry point (manual rewrite required)"`

**Tests (~8 cases):**
- Simple form (name + client_class + handler_class) → `run_dev_combined(MyApp, handler_class=Handler)`
- Simple form without client_class/handler_class → `run_dev_combined(MyApp)`
- `has_configmap=True` in `start()` is dropped (not applicable to v3 — the Helm chart handles this)
- `ui_enabled=...` in `start()` is dropped
- Complex form with `start_worker`/`start_server` → TODO comment, body unchanged
- Complex form with `.include_router(` → TODO comment
- `app =` variable name (vs `application =`) — handled correctly
- No-op when no `BaseApplication(` found

### Step 4: A8 — `run_codemods.py` (Pipeline CLI)

**Standalone CLI + programmatic API for the full codemod pipeline.**

```python
@dataclass
class RunResult:
    connector_type: str
    files_changed: int
    changes_by_file: dict[str, dict[str, list[str]]]  # {file: {codemod: [changes]}}
    todos_inserted: int
    errors: list[str]  # files that failed to parse
```

**Programmatic API:**
```python
def run_codemods(
    root: Path,
    connector_type: str | None = None,
    dry_run: bool = False,
    *,
    skip: list[str] | None = None,   # codemod names to skip, e.g. ["A7"]
) -> RunResult:
    ...
```

**Pipeline execution:**
1. Call `fingerprint_connector(root)` to get `connector_type` (unless provided)
2. For each `.py` file in `root` (excluding `tests/`, `__pycache__/`, `.venv/`):
   - Run codemods in order: A1 → A3 → A4 → A5 → A2 → A6 → A7 (skip A7 if file is not `main.py` or does not contain `BaseApplication`)
   - Collect changes per codemod
   - If not `dry_run`, write modified source back to file
3. Return `RunResult`

**CLI interface:**
```
python -m tools.migrate_v3.run_codemods [options] <path>

Options:
  --connector-type {sql_metadata,incremental_sql,sql_query,custom}
                        Override auto-detected connector type
  --dry-run             Print what would change without writing files
  --skip A1,A2,...      Skip specific codemods by ID
  --no-color            Disable colored output
```

**Output format (stdout):**
```
Fingerprint: sql_metadata (confidence=1.0)
Running 7 codemods on 12 files...

  app/workflow.py
    A1: Removed @workflow.defn from AnaplanMetadataExtractionWorkflow
    A1: Removed @workflow.run from run()
    A2: Rewrote execute_activity_method(activities_instance.get_workflow_args) → await self.get_workflow_args(workflow_config)
    A2: [5 more calls rewritten]
    A6: Removed activities_cls class attribute
    A6: Removed get_activities() static method
    A6: Removed activities_instance = self.activities_cls() line

  app/activities/metadata_extraction.py
    A1: Removed @activity.defn → @task(timeout_seconds=1800) on fetch_databases
    A3: Rewrote fetch_databases(workflow_args) → fetch_databases(input: FetchDatabasesInput)
    A4: Rewrote ActivityStatistics return → FetchDatabasesOutput

  main.py
    A7: Rewrote BaseApplication entry point → run_dev_combined(MyApp, handler_class=AnaplanHandler)

Summary: 3 files changed, 0 TODOs inserted, 0 errors
```

**Tests (~6 cases):**
- End-to-end on a synthetic connector directory — all 7 codemods applied
- `--dry-run` — files not written, output shows changes
- `--skip A7` — A7 not applied
- `--connector-type sql_metadata` — overrides fingerprint
- Files with parse errors skip gracefully (error logged, other files continue)
- `RunResult.files_changed` count is correct

### Step 5: B2 — Post-LLM Fixup (skill step, not a codemod)

B2 is not a codemod — it's a skill step that runs after the LLM completes its structural changes.

**Steps:**
1. `uv run ruff check --fix --select I,F401 <path>` — fix unused imports and sort import order
2. `uv run ruff format <path>` — normalize formatting
3. `uv run python -m tools.migrate_v3.check_migration --no-color <path>` — surface remaining FAILs

These three commands are added to SKILL.md as a "Post-processing cleanup" block at the end of Phase 2b (the AI structural refactoring phase).

**No new Python files needed** — B2 is documentation/skill changes only.

### Step 6: D1 — Restructure Skill

Modify `.claude/skills/migrate-v3/SKILL.md`:

**Current skill flow:**
1. Phase 1: Run `rewrite_imports.py` (automated)
2. Phase 2a: Detect connector type (manual or `--classify`)
3. Phase 2b: AI structural refactoring (4 sub-steps)
4. Phase 3: Validation
5. Phase 4: Testing
6. Phase 5: Summary

**New flow after D1:**
1. Phase 1: Run `rewrite_imports.py` (automated) ← unchanged
2. **Phase 1b (NEW):** Run `run_codemods` (automated) — eliminates bulk structural boilerplate before AI touches it
3. Phase 2a: Confirm connector type (fingerprint already ran in Phase 1b, check output)
4. Phase 2b: AI structural refactoring — handle only what codemods couldn't (class merging, custom logic, TODOs)
5. Phase 3 (B2): Post-processing cleanup (ruff + check_migration)
6. Phase 4: Validation
7. Phase 5: Testing
8. Phase 6: Summary

**Phase 1b block to add:**
```markdown
### Phase 1b: Run Automated Codemods

Before asking the AI to restructure anything, run the codemod pipeline. This eliminates the
mechanical transforms (decorator removal, signature rewrites, activity call rewrites, entry
point rewrite) so the AI only handles what remains.

```bash
uv run python -m tools.migrate_v3.run_codemods <target-path>
```

Review the output: files changed, TODOs inserted, errors. Then re-run the checker to see what's left:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

The remaining FAILs are the AI's work scope for Phase 2b.
```

**Phase 2b intro change:** Prepend "The following items were not automatable and require AI-assisted refactoring:"

**Phase 3 cleanup block (B2):**
```markdown
### Phase 3: Post-Processing Cleanup

After the AI finishes its structural changes, run these cleanup steps:

```bash
# Remove unused imports, fix import order
uv run ruff check --fix --select I,F401 <target-path>

# Normalize formatting
uv run ruff format <target-path>

# Verify migration status
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

All FAIL checks should now pass. If any remain, address them before proceeding.
```

### Step 7: E1 — Snapshot Tests for A2, A6, A7

Following the existing pattern in `tests/unit/tools/test_codemods/conftest.py`.

**New test files:**
- `tests/unit/tools/test_codemods/test_rewrite_activity_calls.py` (~10 cases, specified in Step 1)
- `tests/unit/tools/test_codemods/test_remove_activities_cls.py` (~8 cases, specified in Step 2)
- `tests/unit/tools/test_codemods/test_rewrite_entry_point.py` (~8 cases, specified in Step 3)

For A8 (`run_codemods`), integration-style tests in `tests/unit/tools/test_run_codemods.py` (~6 cases, specified in Step 4).

---

## Verification

1. **Unit tests**: `uv run pytest tests/unit/tools/ -v` — all existing + new tests pass
2. **Pre-commit**: `uv run pre-commit run --all-files` — pyright + ruff clean
3. **End-to-end smoke tests**: Run `run_codemods` on all 4 real connectors:
   ```bash
   for repo in atlan-anaplan-app atlan-athena-app atlan-publish-app atlan-schema-registry-app; do
       uv run python -m tools.migrate_v3.run_codemods --dry-run ../$repo/
   done
   ```
   Verify: no parse errors; expected changes appear; Athena `start_worker`/`start_server` gets TODO comment; Anaplan dynamic dispatch gets TODO comment.

---

## Known Limitations and Non-Goals

- **Class merge (Workflow + Activities → App):** Not automated in this phase. The `run()` method body is left in the workflow class; the AI handles merging it into the App class. Codemods run on the pre-merge files.
- **`run_exit_activities()`:** Calls like `await self.run_exit_activities(workflow_args)` are v2 plumbing that disappears when using a template. Left for the AI/manual step.
- **`application_name: str = APP_NAME`** class attribute: Left in place by A6 (not `activities_cls`). Will be handled by A3/AI when the class is restructured.
- **`@observability` decorator on `main()`:** Removed by A7 when the whole `main()` body is replaced. If complex form is detected, left in place.
- **`has_configmap`, `ui_enabled` in `start()`:** Stripped by A7 in the standard form. These concepts move to the Helm chart / environment config in v3.
