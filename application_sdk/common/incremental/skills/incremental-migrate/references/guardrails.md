# Incremental Extraction Guardrails

Comprehensive guardrails for adding incremental extraction to Atlan connector apps.
These guards prevent known failure modes discovered during the Tableau migration and
generalized for all connector types (SQL, REST, GraphQL).

---

## Section 1: Pre-Implementation Guards

These guards MUST pass before writing any code. If any guard fails, STOP and
resolve it before proceeding.

### GUARD-PRE-01: Verify SDK Dependency Version

Check that `atlan-application-sdk` is declared in `pyproject.toml` and that
the pinned version includes `application_sdk.common.incremental.marker`.

**How to verify:**
```bash
grep "atlan-application-sdk" pyproject.toml
python -c "from application_sdk.common.incremental.marker import IncrementalMarker; print('OK')"
```

**Failure mode:** If the SDK version predates incremental support, all marker
imports will fail at runtime. Upgrade the SDK dependency first.

### GUARD-PRE-02: Verify ObjectStore API Surface

Confirm that `ObjectStore` exposes all four methods needed for state persistence:
- `upload_file` -- write a single artifact
- `get_content` -- read a single artifact
- `upload_prefix` -- bulk upload a directory tree
- `download_prefix` -- bulk download a directory tree

**How to verify:**
```python
from application_sdk.common.object_store import ObjectStore
for method in ["upload_file", "get_content", "upload_prefix", "download_prefix"]:
    assert hasattr(ObjectStore, method), f"Missing: {method}"
```

**Failure mode:** Older SDK versions may lack bulk operations. Chunked storage
patterns require `upload_prefix` / `download_prefix`.

### GUARD-PRE-03: Confirm Dapr gRPC Max Message Size

The hard boundary for any single Dapr gRPC message is **100 MB**. This is NOT
configurable per-app -- it is an infrastructure constant.

**Implication:** Any file written through ObjectStore that exceeds 100 MB will
fail with a gRPC error. The working safety margin is **50 MB per file**.

### GUARD-PRE-04: Run `uv sync` and Confirm Clean

Run `uv sync` and verify it exits with code 0. This establishes a clean
dependency baseline before any modifications.

```bash
uv sync
echo "Exit code: $?"
```

**Failure mode:** If `uv sync` fails before changes, the environment is already
broken. Fix dependency issues first -- do not layer incremental changes on top
of a broken environment.

### GUARD-PRE-05: Record Baseline Test Pass Count

Run the existing test suite and record how many tests pass.

```bash
uv run pytest tests/ -v 2>&1 | tail -5
```

Record the number of passed/failed/skipped tests. After implementation, the
pass count must be >= this baseline. Any regression means the incremental
changes broke existing functionality.

### GUARD-PRE-06: Verify App Starts Without Import Errors

Run the app module to check for import errors:

```bash
uv run python -c "import app; print('imports OK')"
```

Some connectors have conditional imports or missing stubs. Identify these
before adding incremental code so you can distinguish pre-existing issues
from newly introduced ones.

### GUARD-PRE-07: Classify Connector Type

Determine whether the connector is SQL-based or REST-based by checking the
base class inheritance chain:

- **SQL connectors:** Inherit from `SQLConnector`, `SQLHandler`, or similar.
  Incremental detection uses DDL timestamps (e.g., `LAST_DDL_TIME`).
- **REST connectors:** Inherit from `RESTConnector`, `APIHandler`, or similar.
  Incremental detection uses `updatedAt` / `modifiedAt` API fields.

This classification determines which feasibility checklist section applies
and which extraction patterns to use.

### GUARD-PRE-08: Identify Source API Change Detection Mechanism

Before writing any incremental logic, document HOW the source system exposes
change information:

| Mechanism | Example | Notes |
|---|---|---|
| Server-side filtering | `GET /api?updatedSince=...` | Best case -- API does the work |
| updatedAt fields | Response contains `updatedAt` per entity | Must fetch all, filter client-side |
| Revision endpoints | `GET /api/revisions?since=...` | Dedicated change feed |
| Eventual consistency | Timestamp updates are delayed | Must apply prepone buffer |

**Failure mode:** Assuming server-side filtering exists when it does not leads
to fetching all entities anyway, but with broken filtering logic on top.

### GUARD-PRE-09: Map All API Endpoint ID Formats

Many connectors interact with multiple API surfaces (REST, GraphQL, internal
IDs). Each surface may use a DIFFERENT identifier for the same entity.

**Document the mapping:**
- REST API: uses `luid` (locally unique ID)
- GraphQL API: uses `id` (GraphQL node ID)
- Database references: uses `name` or `site/project/name` path

**Failure mode (Tableau R5):** Datasource ID format mismatch between REST
(LUID) and GraphQL (node ID) caused incremental detection to miss updates
because the ID sets were compared across formats.

### GUARD-PRE-10: Estimate Max State Artifact Size

Calculate the worst-case size of the state artifact that will be persisted
between runs:

```
artifact_size = num_entities * avg_bytes_per_entity_record
```

**If estimated size > 50 MB:** Implement chunked storage from the start.
Do NOT build monolithic storage and plan to "chunk later" -- the gRPC limit
will hit in production before you get to the refactor.

---

## Section 2: Implementation Guards

These guards govern how incremental code is written. Violating any of these
produces bugs that are difficult to diagnose in production.

### GUARD-IMPL-01: Persist Marker Before Publish ONLY If Backfill Exists

State persistence timing depends on whether the connector has a backfill mechanism:

**With backfill (e.g., Tableau):** Persisting state before publish is SAFE because
the next run can reconstruct complete output from fresh + backfilled data. Even if
publish fails, no data is lost — the next run reproduces the same complete output.
The Tableau implementation persists before publish for this reason.

**Without backfill:** State MUST be persisted ONLY after publish succeeds. If the
marker is advanced before publish and publish fails, the next run will skip entities
that were never published. This is **state corruption**.

The correct approach depends on the connector:
- **Backfill present:** Extract → Process → Transform → Persist state → Upload (safe)
- **No backfill:** Extract → Process → Transform → Upload → Persist state (required)

When in doubt, persist after publish — it is always safe, just slightly less
efficient (a failed publish causes re-extraction of the delta on the next run).

### GUARD-IMPL-02: State Persistence Must Be Atomic Per-Connection

All state artifacts must be namespaced under the `connection_qualified_name`.
Two connections to the same source must maintain independent state.

```
state_prefix = f"incremental/{connection_qualified_name}/"
```

**Failure mode:** Shared state between connections causes one connection's
incremental run to skip entities that only exist in the other connection.

### GUARD-IMPL-03: Never Pass Large ID Lists Through Temporal workflow_args

Temporal has a **2 MB payload limit** on workflow arguments and activity inputs.
If you need to pass a list of entity IDs between activities:

- Write the list to a local file or ObjectStore
- Pass the file path (not the data) as the activity argument

**Threshold:** If the serialized argument exceeds **100 KB**, use file I/O.

**Failure mode (Tableau R3):** Passing changed entity ID lists as Temporal
activity arguments hit the 2 MB limit on large Tableau sites, crashing the
workflow with a cryptic payload error.

### GUARD-IMPL-04: Activity Return Values Must Be Small

Temporal activity return values have the same 2 MB limit. Activities should
return counts, flags, and file paths -- never raw data.

```python
# WRONG
return {"entities": [<thousands of entity dicts>]}

# RIGHT
return {"entity_count": 1523, "output_path": "extract/tables.json"}
```

### GUARD-IMPL-05: All ObjectStore Files Must Stay Under 50 MB

The Dapr gRPC limit is 100 MB. Use a **50 MB safety margin** for all files
written through ObjectStore.

**How to enforce:**
- Before writing, check the serialized size
- If > 50 MB, split into chunks (see GUARD-IMPL-06)

### GUARD-IMPL-06: Never Store Monolithic Cache at Scale

Use chunked storage when the data may exceed 50 MB:

```python
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB target

def write_chunked(data: list[dict], prefix: str) -> int:
    chunk_index = 0
    current_chunk = []
    current_size = 0
    for item in data:
        item_size = len(json.dumps(item).encode())
        if current_size + item_size > CHUNK_SIZE and current_chunk:
            write_chunk(current_chunk, prefix, chunk_index)
            chunk_index += 1
            current_chunk = []
            current_size = 0
        current_chunk.append(item)
        current_size += item_size
    if current_chunk:
        write_chunk(current_chunk, prefix, chunk_index)
    return chunk_index + 1
```

**Failure mode (Tableau R4/R6):** Field-extract files and the metadata cache
both exceeded the 100 MB gRPC limit on large Tableau sites, crashing extraction.

### GUARD-IMPL-07: Use `suppress_error=True` on State Reads

When reading incremental state artifacts from ObjectStore, always use
`suppress_error=True`. Missing state is NOT an error -- it means this is
the first run (or state was cleared) and should trigger full extraction.

```python
content = await object_store.get_content(
    state_path, suppress_error=True
)
if content is None:
    # First run or cleared state -- do full extraction
    return None
```

### GUARD-IMPL-08: Handle None Markers Gracefully

A `None` marker always means "perform full extraction." Never let a `None`
marker propagate to code that expects a datetime string.

```python
if marker is None:
    logger.info("No incremental marker found, performing full extraction")
    return await self.full_extraction()
```

### GUARD-IMPL-09: Apply Configurable Time Buffer (marker_offset_hours)

Source systems have eventual consistency delays. Apply a configurable prepone
buffer to the cutoff timestamp:

```python
effective_cutoff = marker - timedelta(hours=marker_offset_hours)
```

Default `marker_offset_hours` to a sensible value for the connector type:
- SQL connectors: 1 hour (metadata views may cache)
- REST connectors: 2 hours (API eventual consistency)
- GraphQL connectors: 2-4 hours (indexing delays)

### GUARD-IMPL-10: Capture next_marker BEFORE Extraction Starts

Record the timestamp that will become the next marker BEFORE you begin
extraction. This prevents a race condition where entities modified during
extraction are missed.

```python
next_marker = datetime.utcnow().isoformat()
# NOW begin extraction
entities = await self.extract_entities(current_marker)
# After successful publish, persist next_marker
```

### GUARD-IMPL-11: Use Single ID Format Throughout Pipeline

Choose ONE canonical ID format and use it consistently across all pipeline
stages. If the source exposes multiple ID formats, maintain an explicit
mapping but never mix formats in comparisons.

```python
# Maintain mapping
id_map: dict[str, str] = {}  # graphql_id -> luid

# Always compare using the same format
changed_ids_canonical = {id_map.get(gql_id, gql_id) for gql_id in changed_graphql_ids}
```

**Failure mode (Tableau R2/R5):** Comparing LUIDs from REST API against
GraphQL IDs caused incremental detection to report everything as changed.

### GUARD-IMPL-12: Missing Type = Empty List, NOT None

When an entity type has no changed entities, represent it as an empty list `[]`.
`None` means "no filter applied = extract everything of this type."

```python
# WRONG -- None means "extract all tables"
changed_by_type = {"Table": None, "Column": ["col_1", "col_2"]}

# RIGHT -- empty list means "no tables changed"
changed_by_type = {"Table": [], "Column": ["col_1", "col_2"]}
```

**Failure mode (Tableau R3):** Typed batching returned `None` for missing
types, which downstream code interpreted as "fetch all," negating the
incremental benefit.

### GUARD-IMPL-13: Implement Cascade Detection for Parent-Child Relationships

When a parent entity changes, all its children must be re-extracted even if
their individual timestamps have not changed.

Example: If a Table's schema changes, all its Columns must be re-extracted.
If a Workbook is updated, all its Dashboards and Sheets must be re-extracted.

### GUARD-IMPL-14: First Run Falls Back to Full Extraction

When incremental is enabled but no marker exists (first run), the connector
MUST perform a full extraction and persist the marker afterward.

This is NOT an error condition -- it is the expected bootstrap behavior.

### GUARD-IMPL-15: Always Provide force_full_extraction Escape Hatch

Every incremental connector MUST accept a `force_full_extraction` flag that
bypasses incremental logic and performs a full extraction regardless of
existing markers.

This is the recovery mechanism for state corruption, missed updates, or
any other incremental failure mode.

### GUARD-IMPL-16: Implement Backfill for Complete Output

Some downstream consumers expect the COMPLETE entity set, not just changed
entities. When incremental extraction produces only the delta, implement
backfill from ObjectStore (S3) for unchanged entities.

The output of an incremental run (changed + backfilled) must be
indistinguishable from a full extraction run.

### GUARD-IMPL-17: Delete Detection Requires Full Entity List Comparison

To detect deleted entities, compare the full entity list from the current
run against the previously persisted full entity list.

```
deleted = previous_entity_ids - current_entity_ids
```

This requires persisting the full entity ID list between runs, even during
incremental extraction. Factor this into the state artifact size estimate
(GUARD-PRE-10).

### GUARD-IMPL-18: All Incremental Params in WorkflowArgs with Defaults

All incremental parameters must be declared in the WorkflowArgs model with
sensible defaults:

```python
class WorkflowArgs(BaseModel):
    incremental_enabled: bool = False
    force_full_extraction: bool = False
    marker_offset_hours: float = 2.0
```

`incremental_enabled` MUST default to `False`. Incremental is opt-in only.

---

## Section 3: Post-Implementation Verification

These checks MUST pass before the incremental implementation is considered
complete.

### GUARD-POST-01: Parity Check

Run a full extraction and an incremental extraction (with backfill) against
the same source. The output entity sets must be identical.

**How to verify:**
1. Run full extraction, save output as `full_output`
2. Run incremental extraction (which does full on first run), persist state
3. Make NO changes to the source
4. Run incremental extraction again, save output as `incremental_output`
5. Diff `full_output` vs `incremental_output` -- they must match

### GUARD-POST-02: Zero-Change Scenario

When no entities have changed since the last run, the incremental extraction
must still produce COMPLETE output via backfill.

The output must NOT be empty. It must contain all previously-extracted entities
loaded from the backfill cache.

### GUARD-POST-03: Verify All Persistent Artifacts Exist

After a successful run, verify that all expected state artifacts exist in
ObjectStore:

- Incremental marker (timestamp)
- Entity ID list (for delete detection)
- Backfill cache (chunked entity data)
- Any connector-specific state artifacts

### GUARD-POST-04: Verify Artifact Sizes Within 50 MB Limit

Check every persisted artifact:

```python
for artifact in list_artifacts(state_prefix):
    size_mb = get_artifact_size(artifact) / (1024 * 1024)
    assert size_mb < 50, f"Artifact {artifact} is {size_mb:.1f} MB (limit: 50 MB)"
```

### GUARD-POST-05: Backward Compatibility

A new version of the connector reading state persisted by the old version
must NOT crash. It should either:
- Successfully parse the old state format, OR
- Detect incompatible state and fall back to full extraction

### GUARD-POST-06: Failure Simulation -- Publish Failure

Simulate a publish failure (e.g., by disconnecting network after extraction).
Verify that:
- The incremental marker is NOT updated
- The next run re-extracts the same entities
- No data is lost

### GUARD-POST-07: Failure Simulation -- Corrupted State

Corrupt the state artifact (write garbage to the marker file). Verify that:
- The connector detects the corruption
- Falls back to full extraction
- Persists valid state after the full extraction succeeds

### GUARD-POST-08: Failure Simulation -- Missing State

Delete all state artifacts. Verify that:
- The connector treats this as a first run
- Performs full extraction
- Persists all state artifacts afterward

### GUARD-POST-09: Failure Simulation -- force_full Override

Set `force_full_extraction=True` with existing valid state. Verify that:
- The connector ignores the existing marker
- Performs full extraction
- Updates the persisted state afterward

---

## Section 4: Refusal List

These are actions that the implementation agent MUST NOT take under any
circumstances.

### REFUSE-01: Do NOT Auto-Enable Incremental

`incremental_enabled` MUST default to `False`. The user must explicitly
opt in. Auto-enabling incremental on existing connectors risks silent
data loss if the implementation has bugs.

### REFUSE-02: Do NOT Modify Argo Workflow Templates

Incremental extraction applies to SDK/Temporal apps only. Argo workflow
templates (`.yaml` files in marketplace-packages) are a separate system
and must not be modified as part of incremental work.

### REFUSE-03: Do NOT Delete Existing Extraction Code

Incremental logic WRAPS AROUND existing extraction code. The full extraction
path must remain intact and functional. Incremental is an optimization layer
on top of full extraction, not a replacement.

### REFUSE-04: Do NOT Persist State Before Publish Without Backfill

Restating GUARD-IMPL-01 as a hard refusal. If the connector does NOT have a
backfill mechanism that can reconstruct complete output from cached data,
persisting state before publish is forbidden. If backfill exists (like Tableau),
early persistence is safe and acceptable.

### REFUSE-05: Do NOT Implement Without force_full_extraction Escape Hatch

Every incremental connector needs a manual override. Without it, state
corruption requires manual ObjectStore cleanup, which is operationally
unacceptable.

### REFUSE-06: Do NOT Pass >100 KB Through Temporal Activity Args

If the serialized argument is approaching 100 KB, use file I/O instead.
The 2 MB Temporal limit is a hard crash with no graceful degradation.

### REFUSE-07: Do NOT Store >50 MB as Single Files

Any file exceeding 50 MB must be chunked. The 100 MB Dapr gRPC limit is
infrastructure-level and cannot be raised per-app.

### REFUSE-08: Do NOT Modify the application_sdk Package

The SDK is a shared dependency. All incremental logic must live in the
connector app code, not in the SDK. If SDK changes are needed, they must
go through the SDK team's review process separately.

---

## Section 5: Failure Mode Catalog

Known failures from the Tableau incremental migration, mapped to the generic
guardrails that prevent them.

| # | Failure Description | Root Cause | Guardrail |
|---|---|---|---|
| 1 | `detect_updated` read from wrong API surface | Incremental detection used REST API timestamps but compared against GraphQL entity IDs | GUARD-IMPL-11 |
| 2 | Extraction filtered at API level incorrectly | API endpoint IDs did not match the IDs used in the change detection set | GUARD-PRE-09 |
| 3 | Typed batching returned None for missing types | Code used `None` to represent "no entities of this type changed," but downstream interpreted `None` as "no filter = fetch all" | GUARD-IMPL-12 |
| 4 | Field-extract files exceeded 100 MB gRPC limit | Large Tableau sites produced field-extract files > 100 MB, hitting the Dapr gRPC ceiling | GUARD-IMPL-05, GUARD-IMPL-06 |
| 5 | DS ID format mismatch (LUID vs GraphQL ID) | Datasource REST API returns LUIDs, but GraphQL metadata API uses different node IDs; comparing them yielded 100% "changed" | GUARD-IMPL-11 |
| 6 | Cache exceeded gRPC limit | Monolithic metadata cache grew beyond 100 MB on large sites | GUARD-IMPL-06 |
| 7 | Temporal 2 MB payload limit | Changed entity ID lists were passed as Temporal activity arguments instead of via file I/O | GUARD-IMPL-03 |

**Pattern:** Failures 1, 2, and 5 are all ID/surface mismatches. Failures 4
and 6 are size limit violations. Failure 3 is a semantic null vs empty
confusion. Failure 7 is a Temporal-specific payload limit.

When implementing incremental extraction for a new connector, review this
table and proactively check whether analogous conditions exist in the target
connector's API surface.
