---
name: parity-check
description: End-to-end flow-based parity check between main and refactor-v3 branches of application-sdk. Traces every endpoint and worker flow through both branches to find functional gaps.
argument-hint: "<optional: specific flow to check, e.g. '/start', 'worker', 'credentials', or blank for full check>"
---

# /parity-check

Perform a **flow-based** parity check between `main` and `refactor-v3` branches.
Unlike a file-diff approach, this traces each user-facing endpoint and worker-side
flow end-to-end through both branches to find functional gaps.

**Must be run from the `application-sdk` repo root.**

## Usage

```
/parity-check                     # Full parity check (all flows)
/parity-check /start              # Check only the /start endpoint flow
/parity-check worker              # Check only the worker-side execution flow
/parity-check credentials         # Check only credential handling across all flows
```

---

## Core Principle

**Never diff files — trace flows.** A file diff shows what changed; a flow trace
shows what broke. The critical bugs (missing credential save, unregistered
interceptors) live at the boundaries between modules, not within any single file.

---

## Stage 1 — Setup

### Step 1.1 — Verify environment

```bash
git branch --show-current
git rev-parse --show-toplevel
```

Confirm we are in `application-sdk`. Note the current branch.

### Step 1.2 — Identify branches

- `BASE` = `main` (production baseline)
- `TARGET` = `refactor-v3` (or current branch if it descends from refactor-v3)

### Step 1.3 — Determine scope

If `$ARGUMENTS` is provided, map it to the relevant flow set:

| Argument | Flows to check |
|----------|---------------|
| `/start` | Flow 1 only |
| `/auth` | Flow 2 only |
| `/check` or `/metadata` | Flow 3 only |
| `/events` or `dapr` | Flow 5 only |
| `worker` | Flow 6 only |
| `credentials` | Credential path in Flows 1, 2, 5, 6 |
| `interceptors` | Interceptor registration in Flow 6 |
| (empty) | All flows |

---

## Stage 2 — Flow Tracing

For each flow in scope, launch a **parallel Agent** (subagent_type=Explore) to
trace it through both branches. Each agent must use `git show main:<path>` and
`git show refactor-v3:<path>` to read files from each branch — never read the
working tree directly (it may have uncommitted changes).

**Critical rule for agents**: Report ONLY functional behavior differences. Ignore:
- Logging style changes (f-string → %-style)
- Deprecation warnings
- Import reorganization
- Variable renames
- Comment/docstring changes

### Flow 1 — POST /workflows/v1/start (Workflow Start)

Trace through both branches:

1. **HTTP entry**: Where is the route defined? What request model is used?
2. **Body parsing**: How is the request body parsed and validated?
3. **Credential extraction**: Is `credentials` extracted from body? Saved to secret store? Replaced with `credential_guid`?
4. **Workflow ID generation**: How is the workflow ID created when not provided?
5. **State store save**: What is saved? In what format? What key?
6. **Temporal dispatch**: What args are passed to `client.start_workflow()`? Is `cron_schedule` passed? `execution_timeout`?
7. **Response format**: What shape is the response? What fields?
8. **Error handling**: What exceptions are caught? What HTTP status codes returned?

**Files to read**:
- main: `application_sdk/server/fastapi/__init__.py`, `application_sdk/clients/temporal.py`, `application_sdk/services/secretstore.py`
- v3: `application_sdk/handler/service.py`

### Flow 2 — POST /workflows/v1/auth (Auth Test)

Trace through both branches:

1. **HTTP entry**: Route definition and request model
2. **Credential normalization**: Is `_normalize_credentials()` called? What formats are handled?
3. **Handler initialization**: Is `handler.load(credentials)` called (main) or context injected (v3)?
4. **Client loading**: How do credentials reach the SQL/HTTP client?
5. **Business logic**: What method performs the actual auth test?
6. **Response format**: Shape, fields, status codes
7. **Metrics**: Are request metrics recorded?

**Files to read**:
- main: `application_sdk/server/fastapi/__init__.py` (Server.test_auth), `application_sdk/handlers/base.py`
- v3: `application_sdk/handler/service.py` (/workflows/v1/auth), `application_sdk/handler/base.py`

### Flow 3 — POST /workflows/v1/check and /metadata

Same trace pattern as Flow 2. Focus on:
- Handler method signature changes
- Credential access pattern
- Response model differences

### Flow 4 — GET/POST Config, Status, Stop, File, Configmap

Trace each CRUD endpoint:

1. **State store interaction**: Same API? Same key format?
2. **Temporal client interaction**: Same query pattern for status?
3. **Response format**: Same shape?
4. **File upload**: Same storage mechanism?

**Files to read**:
- main: `application_sdk/server/fastapi/__init__.py` (relevant methods)
- v3: `application_sdk/handler/service.py` (relevant routes)

### Flow 5 — Event-Driven Endpoints

Trace:

1. **GET /dapr/subscribe**: Subscription format, bulk config serialization
2. **POST /events/v1/event/{event_id}**: Input validation, trigger lookup, workflow dispatch, credential handling, response format
3. **POST /events/v1/drop**: Response shape
4. **/subscriptions/v1/\***: Route registration pattern

**Files to read**:
- main: `application_sdk/server/fastapi/__init__.py`, `application_sdk/server/fastapi/models.py`
- v3: `application_sdk/handler/service.py`

### Flow 6 — Worker-Side Execution

Trace the full worker lifecycle:

1. **Worker startup**: How is the worker created? What interceptors are registered?
2. **Interceptor registration**: List every interceptor. Is each one from main present in v3?
3. **Workflow input**: What does the workflow receive? How does it get full config (StateStore lookup vs direct input)?
4. **Credential resolution**: How does the worker get actual credentials from `credential_guid`?
5. **Activity dispatch**: How are activities/tasks called? Retry policies? Timeouts?
6. **Event emission**: How are workflow start/complete/failed events published? What mechanism (activity vs binding)?
7. **Cleanup**: How are temp files cleaned up? Automatic or manual?
8. **Lock management**: Is `@needs_lock` enforced? Is the lock interceptor registered?
9. **Correlation context**: What fields are propagated from workflow to activity?

**Files to read**:
- main: `application_sdk/worker.py`, `application_sdk/interceptors/*.py`, `application_sdk/workflows/metadata_extraction/sql.py`
- v3: `application_sdk/execution/_temporal/worker.py`, `application_sdk/execution/_temporal/interceptors/*.py`, `application_sdk/templates/sql_metadata_extractor.py`, `application_sdk/app/base.py`

---

## Stage 3 — Consolidation

After all agents complete, consolidate findings into a single table.

### Step 3.1 — Classify each gap

For each gap found, assign:

| Field | Values |
|-------|--------|
| **Severity** | `critical` (data loss, security, silent failure), `high` (feature broken), `medium` (behavior change, may break callers), `low` (cosmetic, minor) |
| **Type** | `missing-feature`, `breaking-change`, `security`, `silent-failure`, `response-format`, `config-gap` |
| **Flow** | Which flow(s) it affects |
| **Intentional?** | `yes` (v3 design decision), `no` (oversight), `unclear` (needs team input) |

### Step 3.2 — Cross-reference with known gaps

Read the existing parity report if present:
```bash
cat docs/v3-parity-report.md 2>/dev/null
```

Mark any gaps that are already documented. Flag new gaps that aren't.

### Step 3.3 — Enter Plan Mode

Call `EnterPlanMode` and present findings:

```markdown
# Parity Check: main vs refactor-v3

**Date:** <today>
**Scope:** <full or specific flow>
**Branches:** main (<sha>) vs refactor-v3 (<sha>)

## Critical Gaps

| # | Gap | Flow | Type | Intentional? |
|---|-----|------|------|-------------|
| 1 | ... | /start | missing-feature | no |

## High Gaps

| # | Gap | Flow | Type | Intentional? |
|---|-----|------|------|-------------|

## Medium Gaps

...

## Low Gaps

...

## Already Documented

| # | Gap | Where documented |
|---|-----|-----------------|
| 1 | ... | docs/v3-parity-report.md §1.1 |

## New Findings (not previously documented)

| # | Gap | Recommended action |
|---|-----|-------------------|
```

Call `ExitPlanMode` and wait for user input.

---

## Stage 4 — Update Report (if user approves)

If the user asks to update the report:

1. Read `docs/v3-parity-report.md`
2. Merge new findings into the appropriate sections
3. Update the "Generated" date
4. Write the updated file

If the user asks to fix specific gaps, defer to `/forward-port-to-v3` or manual implementation.

---

## Constraints

- **Never trust file diffs alone.** A file can be present in both branches but have a
  broken flow because a dependency changed. Always trace the full call chain.
- **Read from git, not the working tree.** Use `git show <branch>:<path>` to avoid
  confusion with uncommitted changes.
- **Parallel agents for each flow.** Each flow trace is independent — launch them
  concurrently to minimize wall time.
- **Functional gaps only.** Logging, style, deprecation, and naming changes are noise.
  Only report differences that change runtime behavior for callers or workers.
- **Security-sensitive flows get extra scrutiny.** Credential handling, secret store
  interactions, and anything that touches Temporal history must be traced with
  particular care — credentials in Temporal history is a security incident.
- **Check both directions.** Also flag v3-only features that main lacks, labeled as
  "v3 improvement" — these help the team understand what v3 adds, not just what it lost.
