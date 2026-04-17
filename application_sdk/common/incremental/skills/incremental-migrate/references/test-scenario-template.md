# Test Scenario Generation Template

Use this template to **generate** test scenarios for any incremental connector.
It provides categories and rules — not pre-baked test cases. Sub-agents should
read the connector's feasibility report and codebase, then produce concrete
test cases by instantiating the categories below.

---

## Section 1: Standard Lifecycle Scenarios (Always Apply)

These scenarios test the fundamental incremental lifecycle and apply regardless
of connector type. Every connector MUST have one test per row.

| Category | What to Test | Key Assertions |
|----------|-------------|----------------|
| Full baseline | Run with `incremental_enabled=false` | No marker read/written, no state persisted, output matches non-incremental run |
| First incremental | Run with `incremental_enabled=true`, no prior marker | Marker not found -> graceful fallback to full extraction. State persisted after run. |
| Normal incremental | Run with marker + state from previous run, source has changes | Changed entities detected, fresh extraction for changed, backfill for unchanged, total output = fresh + backfill |
| Zero changes | Run with marker + state, source unchanged | 0 entities detected as changed, all data from backfill, complete output produced |
| Force full override | `force_full_extraction=true` with existing marker | Marker bypassed, full extraction runs, state overwritten |

### Generation rules

- Name each test `test_<category>_<entity>` (e.g., `test_first_incremental_dashboard`).
- For "Normal incremental", mutate at least one entity in the mock source data
  between the baseline and the incremental run.
- For "Zero changes", use identical mock source data for both runs.
- Assert on **output completeness**: the final file/upload must contain the
  union of freshly-extracted and backfilled records.

---

## Section 2: Risk-Register-Derived Scenarios (Generate from Feasibility Report)

For each **confirmed** risk in the feasibility report, generate a corresponding
test scenario. The mapping:

| Confirmed Risk | Test Scenario |
|----------------|---------------|
| Cascade risk confirmed | Update parent entity, verify child entities are included in changed set via cascade |
| False-positive pattern identified | Trigger the false positive (e.g., data refresh, metadata-only change), verify the filter skips it |
| Eventual consistency delay | Verify prepone buffer covers the delay window; entities updated within the buffer are re-extracted |
| ID format divergence | Verify IDs are normalized consistently across detection and extraction phases |
| Rate limiting under incremental load | Verify retry/backoff handles 429s during detection API calls |
| Pagination boundary | Verify detection correctly pages through all results, no entities dropped at page boundaries |

### Generation rules

- One test scenario per confirmed risk. No exceptions.
- **Skip** scenarios for risks marked "not confirmed" or "deferred" in the
  feasibility report.
- Name each test `test_risk_<short_risk_name>` (e.g., `test_risk_cascade_dashboard_to_widget`).
- Each test must include a setup step that creates the risky condition, an
  action step that runs incremental, and assertions that prove the risk is
  handled.

---

## Section 3: State Management Scenarios (Always Apply)

These scenarios verify that persistent state (S3 artifacts, markers) is handled
correctly across edge cases.

| Category | What to Test | Key Assertions |
|----------|-------------|----------------|
| Entity deleted | Entity exists in previous state but not in current extraction | Excluded from backfill, not in final output |
| Corrupted state | Write garbage to state file, run incremental | Graceful fallback to full extraction, no crash |
| Missing state | Delete all S3 state artifacts, run incremental | Equivalent to first-run behavior |
| State after publish | Run incremental successfully | All persistent artifacts (marker, index, output) exist with correct content |
| State NOT after failure | Simulate upload failure mid-run | Marker NOT updated, next run re-extracts the same delta |
| Backward compat | Read state written by older code version | No crash, correct behavior, missing keys get defaults |

### Generation rules

- Mock the ObjectStore for unit tests; use localstack or equivalent for
  integration tests.
- "Corrupted state" should use at least two corruption variants: truncated
  JSON and completely invalid bytes.
- "Backward compat" requires a fixture file representing the old state format.

---

## Section 4: Filter Scope Scenarios (Only If Connector Has Scope Filters)

Skip this section entirely if the connector does not support scope filters
(project selectors, workspace selectors, etc.).

| Category | What to Test | Key Assertions |
|----------|-------------|----------------|
| Scope added | Add new scope (project/workspace) between runs | New scope's entities treated as "new", full extraction for them, existing scopes use incremental |
| Scope removed | Remove scope between runs | Removed scope's entities excluded from backfill and output |
| First run with filters | No prior state + filters active | Correct scoped extraction, only in-scope entities in output |
| Filter + zero changes | Filters active, no changes in any scoped entity | All data from backfill, output matches previous run |

### Generation rules

- Use at least two scopes in the mock data to test add/remove independently.
- Verify that scope changes do NOT corrupt state for unaffected scopes.

---

## Section 5: Unit Test Matrix (What to Test Per Module)

Use this matrix to decide what belongs in unit tests vs. what should be mocked.

| Module Type | What to Unit Test | What NOT to Unit Test (Mock Instead) |
|-------------|------------------|--------------------------------------|
| Detection logic | Each detection function with mock API responses: changed, unchanged, R4-filtered, cascaded, deleted entities | SDK marker read/write (tested by SDK itself) |
| Cache/state | Serialization round-trips, missing keys, empty data, backward compat with old formats | ObjectStore operations (mock them) |
| Backfill | Index-based line extraction, chunked reads, v1->v2 fallback, empty index, missing files | Actual S3 downloads (mock ObjectStore) |
| Workflow wiring | Activity invocation order, conditional branches, short-circuit on 0 changes | Temporal internals (worker, task queue) |
| Filter logic | Include/exclude by scope, empty filters, overlapping filters | API calls to fetch filter values |

### Generation rules

- Aim for **one test file per module** (e.g., `test_detection.py`,
  `test_backfill.py`, `test_state.py`).
- Each test function should test exactly one behavior. Prefer many small tests
  over few large ones.
- Use `pytest.mark.parametrize` for detection tests that vary by entity type.
- All mocks should use `unittest.mock.patch` or `pytest-mock` fixtures, never
  monkey-patching at module level.
