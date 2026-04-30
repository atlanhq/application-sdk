---
name: incremental-migrate
description: >
  Orchestrates adding incremental extraction to any Atlan connector app.
  Handles both SQL connectors (delegates to SDK's implement-incremental-extraction
  skill) and REST/GraphQL connectors (uses the Tableau pattern as reference).
  Runs as isolated sub-agents: reconnaissance -> feasibility -> implementation
  (branched by type) -> metrics -> test design -> test implementation -> iterative
  test-fix loop. Trigger with "Add incremental extraction to @atlan/<connector>".
metadata:
  author: platform-engineering
  version: "1.0.0"
  category: connector-incremental
  keywords:
    - incremental-extraction
    - application-sdk
    - metadata-extraction
    - temporal-workflows
    - sql-connector
    - rest-connector
    - graphql-connector
---

# Incremental Extraction Migration -- Orchestrator

## Trigger pattern

```
Add incremental extraction to @atlan/<connector>
```

Run this from inside the connector repo. `app_path` is resolved from `pwd`.

---

## Step 1 -- Parse inputs

From the trigger prompt and environment, extract:

| Variable | Source | Example |
|---|---|---|
| `connector` | slug from `@atlan/<connector>` | `sigma` |
| `ConnectorName` | title-case of slug | `Sigma` |
| `app_path` | current working directory (`pwd`) | `/Users/you/repos/atlan-sigma-app` |
| `sdk_skills_dir` | resolve from app venv: `find <app_path>/.venv -path "*/incremental/skills" -type d` | `<app_path>/.venv/.../application_sdk/common/incremental/skills` |
| `refs_dir` | `<sdk_skills_dir>/incremental-migrate/references` | (resolved from sdk_skills_dir) |
| `sdk_sql_skill_dir` | `<sdk_skills_dir>/implement-incremental-extraction` | (sibling skill in SDK) |
| `sdk_marketplace_skill_dir` | `<sdk_skills_dir>/marketplace-packages-incremental` | (sibling skill in SDK) |
| `tableau_app_dir` | ask user if not known; check common paths: `~/Atlan/atlan-tableau-app`, `~/Documents/GitHub/atlan-tableau-app` | `/Users/you/Atlan/atlan-tableau-app` |
| `marketplace_dir` | ask user if not known; check common paths: `~/Documents/GitHub/marketplace-packages`, `~/Atlan/marketplace-packages` | `/Users/you/Documents/GitHub/marketplace-packages` |

Resolve `app_path` first by running `pwd`.

### Pre-flight checks (Brick 0)

Run these in the main session before spawning any sub-agent:

1. Verify `<app_path>/pyproject.toml` exists and contains `atlan-application-sdk`
2. Run `cd <app_path> && uv sync --all-groups` -- stop if it fails
3. Run `cd <app_path> && uv run pytest tests/unit/ -x -q 2>/dev/null` and record the baseline test count
4. Classify connector type:
   - Run `grep -r "BaseSQLMetadataExtractionActivities\|IncrementalSQLMetadataExtractionActivities" <app_path>/app/`
   - If matches found: `connector_type = SQL`
   - Otherwise: `connector_type = REST`
5. Verify `<sdk_sql_skill_dir>/SKILL.md` exists (already resolved in step 1)
6. Write `<app_path>/incremental-analysis/connector-type.txt` with the classification
7. Report classification and baseline to user before proceeding

**Stop if:** no SDK dependency, no `app/activities/`, or existing unit tests fail.

---

## Step 2 -- Sub-agent execution model

Every brick runs as an isolated sub-agent using the `Agent` tool with
`subagent_type: "general-purpose"`. The main session only:
- Spawns sub-agents
- Reads cross-brick outputs from disk to pass forward
- Tracks completion and reports failures

**Do not execute any brick inline in the main session.**

### Sub-agent prompt template

For every brick, construct the prompt by substituting all `<placeholders>`:

```
You are a sub-agent implementing **Brick <N> -- <brick-name>** for adding
incremental extraction to the **<ConnectorName>** connector app.

## Prime Directive -- Safety First

Your goal is to add incremental extraction without breaking existing functionality.
The full extraction path must remain fully functional when incremental is disabled.

The following are explicitly forbidden:
- Do NOT delete existing extraction code. Incremental wraps around it.
- Do NOT auto-enable incremental. Default must be `incremental_enabled=false`.
- Do NOT persist state before the publish/upload step succeeds.
- Do NOT pass large data (>100KB) through Temporal activity arguments.
- Do NOT store single files larger than 50MB on ObjectStore.
- Do NOT modify the application_sdk package.

Read `<refs_dir>/guardrails.md` before writing any code. Every guardrail
applies. When in doubt, ask the user.

## Variables
- connector         = <connector>
- ConnectorName     = <ConnectorName>
- connector_type    = <connector_type>    (SQL or REST)
- app_path          = <app_path>          <- write all output files here
- refs_dir          = <refs_dir>          <- SDK-bundled reference docs (guardrails, patterns, templates)
- tableau_app_dir   = <tableau_app_dir>   <- read-only reference app
- marketplace_dir   = <marketplace_dir>   <- read-only reference repo

## Cross-brick context
<embed cross-brick context here -- see handoff rules below; omit if N/A>

## Your task
<embed the brick-specific instructions here>
```

### Cross-brick handoff rules

| After brick | Read from disk | Pass to bricks |
|---|---|---|
| Brick 1 (Recon) | `incremental-analysis/reconnaissance.md` | 2 |
| Brick 2 (Feasibility) | `incremental-analysis/feasibility-report.md` | All implementation bricks |
| Brick 3R (API Design) | `incremental-analysis/api-change-detection.md` | 4R, 5R, 6R, 7R |
| Brick B (Test Design) | `incremental-analysis/test-plan.md` | C |
| All others | Sub-agents read disk themselves | -- |

---

## Step 3 -- Run bricks in sequence

Execute each brick fully before starting the next. Halt on failure and report.

---

### Brick 1 -- Reconnaissance

1. Spawn a sub-agent with the prompt template. The brick-specific instructions:

   ```
   Read every file in <app_path>/app/ (activities, workflows, extracts, sql,
   client.py, models.py, handler.py, transformers). Produce a structured report
   at <app_path>/incremental-analysis/reconnaissance.md containing:

   1. **Entity Type Inventory** -- every entity type extracted, with parent-child
      relationships (e.g., "Workbook -> Worksheet -> Field")
   2. **API / Query Map** -- every API call or SQL query, its purpose, and what
      it returns. Include request shapes for REST and query text for SQL.
   3. **Timestamp Fields** -- per entity type, list any updatedAt/createdAt/
      lastModifiedTime/LAST_DDL_TIME fields available from the API or DB catalog
   4. **Cost Structure** -- which extraction steps are cheap (summary/list APIs)
      vs expensive (detail APIs with N+1 queries, nested field extraction)
   5. **Current Workflow Sequence** -- numbered list of all activities in execution
      order, noting which are conditional
   ```

2. Verify `<app_path>/incremental-analysis/reconnaissance.md` exists.

---

### Brick 2 -- Feasibility Analysis

1. Read `<refs_dir>/feasibility-checklist.md` into memory.
2. Read `<app_path>/incremental-analysis/reconnaissance.md` into memory.
3. Spawn a sub-agent with both documents embedded. The brick-specific instructions:

   ```
   Using the reconnaissance report and the feasibility checklist, produce
   <app_path>/incremental-analysis/feasibility-report.md containing:

   1. **Per-entity feasibility verdict** -- for each entity type, evaluate against
      the 5 non-negotiable rules. Mark PASS or FAIL with reasoning.
   2. **Incremental strategy** -- server-side filtering (SQL WHERE) vs client-side
      diffing (fetch all, compare updatedAt in code)
   3. **What to make incremental** -- which entity types are expensive enough to
      justify incremental, and which should remain full-extraction (cheap parents)
   4. **Risk register** -- one risk per identified concern, scored by likelihood x
      impact (1-5 each), with mitigation strategy
   5. **State artifacts needed** -- marker only? marker + entity list? marker +
      entity list + cache + backfill extracts?
   6. **Marker strategy** -- SDK standard marker, prepone buffer (default 1h),
      configurable via marker_offset_hours
   7. **False-positive patterns** -- any scenarios where updatedAt bumps without
      metadata change (data refreshes, no-op publishes, etc.)
   8. **Cascade requirements** -- any parent-child relationships where parent
      changes don't propagate timestamps to children
   9. **Delete detection strategy** -- previous_ids vs current_ids comparison
   ```

4. Verify `<app_path>/incremental-analysis/feasibility-report.md` exists.
5. Read the feasibility report and show it to the user.
6. **PAUSE -- wait for explicit user confirmation before continuing.**
   The user must confirm: connector type, feasibility verdicts, strategy, and risks.

---

### BRANCH -- Read `connector_type` and take the appropriate path

---

## SQL Path

### Brick 3S -- SDK Delegation

1. Read `<sdk_sql_skill_dir>/SKILL.md` (the SDK's implement-incremental-extraction
   SKILL.md) into memory. If the SDK skill doesn't exist, stop and tell the user
   to upgrade the SDK.
2. Read all files under `<sdk_sql_skill_dir>/references/`.
3. Read `<app_path>/incremental-analysis/feasibility-report.md` into memory.
4. Spawn a sub-agent with the full SDK skill text + all reference docs + feasibility
   report embedded. The sub-agent follows the SDK skill's own instructions to:
   - Create `app/sql/extract_table_incremental.sql`
   - Create `app/sql/extract_column_incremental.sql`
   - Modify the activities class to inherit from `IncrementalSQLMetadataExtractionActivities`
   - Implement `build_incremental_column_sql()`
   - Modify the workflow class to inherit from `IncrementalSQLMetadataExtractionWorkflow`
   - Update models and dependencies
5. Verify the SQL files and modified classes exist.

### Brick 4S -- Marketplace Docs

1. Read `<sdk_marketplace_skill_dir>/SKILL.md` for the marketplace-packages change pattern.
2. Spawn a sub-agent to produce `<app_path>/incremental-analysis/marketplace-changes.md`
   documenting what YAML parameters to add to the Argo configmap. The sub-agent reads
   `<marketplace_dir>/packages/atlan/<connector>/` for the current template structure.
3. Tell the user: "Marketplace changes documented -- apply them manually in the
   marketplace-packages repo."

**Skip to Brick A (Metrics).**

---

## REST/GraphQL Path

### Brick 3R -- API Change-Detection Design

1. Read `<refs_dir>/rest-incremental-pattern.md` into memory.
2. Read `<app_path>/incremental-analysis/feasibility-report.md` into memory.
3. Read `<tableau_app_dir>/app/extracts/incremental.py` into memory (reference).
4. Spawn a sub-agent. The brick-specific instructions:

   ```
   Using the feasibility report, the REST incremental pattern reference, and the
   Tableau incremental.py as a code reference, produce
   <app_path>/incremental-analysis/api-change-detection.md containing:

   1. Per entity type: which API field serves as the change marker (updatedAt,
      lastModifiedTime, etc.)
   2. Server-side vs client-side filtering decision per entity
   3. False-positive patterns with detection logic (e.g., if updatedAt ==
      lastRefreshTime -> skip)
   4. Cascade requirements with detection logic (parent updated -> find children
      referencing that parent via upstreamX fields)
   5. Delete detection strategy (previous_ids - current_ids, source of each set)
   6. ID format map -- which API surface returns which ID format, and how to
      normalize if multiple formats exist
   ```

5. Verify `<app_path>/incremental-analysis/api-change-detection.md` exists.

### Brick 4R -- Detection Logic Implementation

1. Read `<app_path>/incremental-analysis/api-change-detection.md` into memory.
2. Read `<refs_dir>/guardrails.md` into memory.
3. Read `<tableau_app_dir>/app/extracts/incremental.py` into memory (reference).
4. Spawn a sub-agent. Instructions: implement `<app_path>/app/extracts/incremental.py`
   with detection functions per the API change-detection design. Include:
   - `detect_updated_<entity>()` per entity type needing incremental
   - False-positive filter functions (if identified)
   - `cascade_<parent>_to_<child>()` (if cascade required)
   - `detect_deleted_<entity>()` for delete detection
   - Helper functions: `load_<entity>_records()`, `batch_ids()`
5. Verify `<app_path>/app/extracts/incremental.py` exists.

### Brick 5R -- State Management

1. Read `<app_path>/incremental-analysis/api-change-detection.md` into memory.
2. Read `<tableau_app_dir>/app/activities/metadata_extraction.py` into memory (reference
   -- particularly `read_marker`, `read_previous_ds_list`, `persist_incremental_state`).
3. Read `<refs_dir>/rest-incremental-pattern.md` Section 2 (State Management Template).
4. Spawn a sub-agent. Instructions: add new activities and model fields.

   **New activities** in `<app_path>/app/activities/metadata_extraction.py`:
   - `read_marker` -- calls SDK `fetch_marker_from_storage` with configurable prepone
   - `read_previous_<entity>_list` -- downloads previous scope from S3 via ObjectStore
   - `persist_incremental_state` -- writes marker + entity lists + cache to S3
     (GUARD-IMPL-01: this MUST be the last activity, after upload_to_atlan)

   **Modified** `<app_path>/app/models.py`:
   - `incremental_enabled: bool = False`
   - `force_full_extraction: bool = False`
   - `marker_offset_hours: int = 1`
   - Kebab-case mappings in `_map_kebab_keys` if the connector uses them

5. Verify the new activities and model fields exist.

### Brick 6R -- Backfill and Chunked Storage

1. Read `<app_path>/incremental-analysis/feasibility-report.md` (state artifacts section).
2. Read `<tableau_app_dir>/app/activities/metadata_extraction.py` (backfill + chunked
   storage sections -- particularly `backfill_unchanged_fields`, `_write_chunked_field_extracts`).
3. Read `<refs_dir>/rest-incremental-pattern.md` Section 3 (Backfill Template).
4. Spawn a sub-agent. Instructions:
   - Add `backfill_unchanged_<entity>` activity that restores previous data for unchanged
     entities from S3 using the v2 chunked index pattern
   - Add chunked write logic if estimated state > 50MB
   - Create `<app_path>/app/extracts/incremental_cache.py` if per-entity field snapshots
     are needed (based on feasibility report)
   - Read `<refs_dir>/guardrails.md` GUARD-IMPL-05/06 for chunk size rules
5. Verify the backfill activity exists.

### Brick 7R -- Workflow Wiring

1. Read all implementation files from Bricks 4R-6R.
2. Read `<tableau_app_dir>/app/workflows/metadata_extraction.py` (reference for
   conditional branching, activity ordering, short-circuit logic).
3. Read `<refs_dir>/rest-incremental-pattern.md` Section 5 (Workflow Wiring Template).
4. Spawn a sub-agent. Instructions: modify `<app_path>/app/workflows/metadata_extraction.py`:
   - Parse `incremental_enabled` and `force_full_extraction` from workflow args
   - Add conditional branch after parent entity extraction:
     - `read_marker` (skip if incremental disabled)
     - `read_previous_<entity>_list` (skip if no marker found)
     - `detect_updated_<entities>` (compare against cutoff)
     - Modify expensive extraction to use only changed entity IDs
     - `backfill_unchanged_<entities>` after fresh extraction
   - Add short-circuit: if 0 entities changed, skip expensive extraction entirely
   - Add `persist_incremental_state` AFTER `upload_to_atlan` (GUARD-IMPL-01)
   - Register all new activities in `get_activities()`
   - Update `<app_path>/app/templates/workflow.json` to add incremental UI toggles
5. Verify the workflow modifications.

### Brick 8R -- Marketplace Docs

Read `<sdk_marketplace_skill_dir>/SKILL.md` for the marketplace-packages change pattern.

Same as Brick 4S. Produce `marketplace-changes.md`, tell user to apply manually.

---

## Converged Path (both SQL and REST rejoin here)

### Brick A -- Metrics Instrumentation

1. Read `<refs_dir>/metrics-template.md` into memory.
2. Spawn a sub-agent. Instructions: add Segment metrics to the connector following
   the metrics template. Use `get_metrics().record_metric()` directly -- no custom
   sinks or decorators. Add calls at: detection, backfill, filter, and workflow-level
   emission points. Prefix all metrics with `<connector>_`.
3. Verify metrics calls exist in the activities file.
4. **PAUSE -- show the user what was implemented and wait for confirmation** that
   the implementation phase is complete before moving to testing.

### Brick B -- Test Design

1. Read `<refs_dir>/test-scenario-template.md` into memory.
2. Read `<app_path>/incremental-analysis/feasibility-report.md` into memory.
3. Read all implementation code in `<app_path>/app/extracts/incremental*.py` and
   `<app_path>/app/activities/metadata_extraction.py`.
4. Spawn a sub-agent. Instructions:

   ```
   Design a test plan at <app_path>/incremental-analysis/test-plan.md. Derive ALL
   test scenarios from the feasibility report's risk register and the implemented
   code -- do NOT copy test cases from any other connector.

   For each file in app/extracts/incremental*.py, identify every branch, edge case,
   and error path. Generate unit test cases for each.

   For the workflow, generate E2E scenarios from:
   - Standard lifecycle categories (from the test-scenario-template)
   - One scenario per confirmed risk in the feasibility report
   - State management scenarios (always apply)
   - Filter scope scenarios (only if connector has scope filters)

   Express all scenarios in terms of THIS connector's entities, APIs, and data model.
   ```

5. Verify `<app_path>/incremental-analysis/test-plan.md` exists.
6. Read and show the test plan to the user.
7. **PAUSE -- wait for explicit user confirmation** that the test plan is sufficient.

### Brick C -- Test Implementation (parallel)

1. Read `<app_path>/incremental-analysis/test-plan.md` into memory.
2. Read existing test patterns from `<app_path>/tests/unit/` (conftest, fixtures, mocks).
3. Spawn **two sub-agents in parallel**:

   **Sub-agent A -- Unit Tests:**
   ```
   Implement unit tests from the test plan. Read every file in
   <app_path>/app/extracts/incremental*.py and each new activity in
   <app_path>/app/activities/metadata_extraction.py.
   Match the project's existing test conventions (fixture style, mock patterns).
   Mock the API client and file I/O -- no external dependencies.
   Write to: tests/unit/extracts/test_incremental.py (and test_incremental_cache.py
   if a cache module exists).
   ```

   **Sub-agent B -- E2E Tests:**
   ```
   Implement E2E tests from the test plan. Read the workflow file and models.
   Match existing E2E patterns in <app_path>/tests/e2e/ if they exist.
   Use workflow runner pattern (mock Temporal or invoke locally).
   Write to: tests/e2e/test_incremental_*.py per scenario group, conftest.py,
   and helpers/ as needed.
   ```

4. Verify test files exist for both unit and E2E.

### Brick D -- Test Execution and Fix Loop

This brick is managed directly by the orchestrator, NOT as a single sub-agent.

```
iteration = 0
max_iterations = 5
prev_fail_count = 999999
stall_count = 0

WHILE iteration < max_iterations:
    iteration += 1
    Log: "=== Test iteration {iteration}/{max_iterations} ==="

    # Phase A: Run tests (2 parallel sub-agents)
    Spawn Agent A: cd <app_path> && uv run pytest tests/unit/ -v --tb=short 2>&1
    Spawn Agent B: cd <app_path> && uv run pytest tests/e2e/ -v --tb=short 2>&1
        (skip Agent B if no e2e tests exist or no test infra available)

    # Phase B: Collect and analyze results
    Parse pass/fail counts from both outputs.
    total_failures = unit_failures + e2e_failures

    IF total_failures == 0:
        Log: "All tests pass after {iteration} iteration(s)."
        BREAK

    IF total_failures >= prev_fail_count:
        stall_count += 1
    ELSE:
        stall_count = 0

    IF stall_count >= 2:
        Log: "Test loop stalled -- no improvement for 2 consecutive iterations."
        Report remaining failures to user.
        BREAK

    prev_fail_count = total_failures

    # Phase C: Fix failures (1 sub-agent per failing category)
    For each category (unit, e2e) with failures:
        Spawn a sub-agent with:
        - The full pytest output (error messages, tracebacks)
        - The source file being tested
        - The test file
        - Instruction: "Analyze each failure. If the test expectation is wrong
          (doesn't match the implemented behavior), fix the test. If the
          implementation has a bug (doesn't match the feasibility report's
          design), fix the source code. Fix one issue at a time. Read
          <refs_dir>/guardrails.md before making any source changes."

    # Phase D: Verify compilation
    Run: cd <app_path> && uv run python -c "import app" 2>&1
    If import fails, spawn a sub-agent to fix the syntax error.
```

---

## Step 4 -- Final report

After all bricks complete, output a summary:

```
Incremental extraction added: atlan-<connector>-app
  Connector type: <SQL | REST>
  Entities made incremental: <list>
  Entities kept full: <list>
  New files created: <list>
  Files modified: <list>
  Test results: <N> unit tests, <M> e2e tests, all passing
  Risks: <count> identified, all mitigated
  Marketplace changes: see incremental-analysis/marketplace-changes.md

Next steps:
  1. Review the code changes and create a PR
  2. Apply marketplace-packages changes from marketplace-changes.md
  3. Test on a staging tenant with incremental_enabled=true
  4. Ring-release: internal -> beta -> GA
```
