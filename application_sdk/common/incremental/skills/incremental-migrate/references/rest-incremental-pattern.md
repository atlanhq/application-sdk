# REST/GraphQL Incremental Extraction Pattern

Reusable template distilled from the Tableau incremental extraction implementation.
Any REST or GraphQL connector can follow this pattern to add incremental extraction
support with minimal structural changes to an existing full-extraction workflow.

---

## Section 1: Detection Logic Template

### 1.1 Published / Parent Entity Detection (Direct Comparison)

```python
def detect_updated_entities(
    records: list[dict],
    cutoff_iso: str,
    apply_false_positive_filter: bool = True,
) -> tuple[set[str], dict]:
    """
    For each entity record from the full extraction:
      1. Compare updatedAt / createdAt against cutoff_iso
      2. Apply false-positive filter if applicable
      3. Return (changed_entity_ids, stats_dict)
    """
    changed = set()
    stats = {"total": 0, "changed": 0, "false_positive_skipped": 0}

    for rec in records:
        stats["total"] += 1
        updated_at = rec.get("updatedAt") or rec.get("createdAt")
        if not updated_at:
            # No timestamp — treat as changed (safe default)
            changed.add(rec["id"])
            stats["changed"] += 1
            continue

        if updated_at > cutoff_iso:
            if apply_false_positive_filter and _is_false_positive(rec):
                stats["false_positive_skipped"] += 1
                continue
            changed.add(rec["id"])
            stats["changed"] += 1

    return changed, stats
```

### 1.2 False-Positive Filter (R4 Equivalent)

Some APIs bump `updatedAt` without any metadata actually changing (e.g., Tableau
bumps workbook `updatedAt` on every data-refresh even when schema is identical).

```python
def _is_false_positive(rec: dict) -> bool:
    """
    R4 filter: if updatedAt == lastRefreshTime and no other field changed,
    the entity metadata is unchanged — skip it.
    Adapt the field names to match your API's semantics.
    """
    return (
        rec.get("updatedAt") == rec.get("lastRefreshTime")
        and rec.get("contentVersion") == rec.get("_prev_contentVersion")
    )
```

### 1.3 Embedded / Child Entity Detection (Cascade from Parent)

Child entities (columns, fields, sheets) may not carry their own `updatedAt`.
Detect them by cascading from their parent.

```python
def detect_updated_children(
    parent_changed_ids: set[str],
    child_records: list[dict],
    parent_key: str = "parentId",
) -> set[str]:
    """
    Any child whose parent is in parent_changed_ids is considered changed.
    """
    return {
        child["id"]
        for child in child_records
        if child.get(parent_key) in parent_changed_ids
    }
```

### 1.4 Delete Detection

```python
def detect_deleted_entities(
    previous_ids: set[str],
    current_ids: set[str],
) -> set[str]:
    """
    Entities present in the previous run but absent now are deletes.
    previous_ids is loaded from filtered-<entities>.json (Section 2).
    """
    return previous_ids - current_ids
```

### 1.5 Cascade Detection (Parent Changed -> Re-extract Children)

```python
def cascade_to_children(
    parent_changed_ids: set[str],
    parent_to_children: dict[str, list[str]],
) -> set[str]:
    """
    Given a mapping of parent_id -> [child_ids], return all child IDs
    that need re-extraction because their parent changed.
    """
    result = set()
    for pid in parent_changed_ids:
        result.update(parent_to_children.get(pid, []))
    return result
```

---

## Section 2: State Management Template

### 2.1 Persistent Artifact Layout

```
persistent-artifacts/apps/{app_name}/connection/{connection_id}/
+-- marker.txt                           # SDK standard marker (singular "connection")
+-- filtered-<entities>.json             # Previous run's entity scope (ID lists)
+-- <entity>_cache.json                  # Per-entity snapshots (for delete detection)
+-- <entity>-extracts/                   # Backfill data (chunked NDJSON)
    +-- _index.json                      # entity -> chunk + line-range index (v2)
    +-- <type>/result-0.json             # Chunked NDJSON files (~50MB each)
    +-- <type>/result-1.json
```

> **SDK quirk**: The SDK marker helper uses singular `connection/` in its S3 path
> (e.g., `persistent-artifacts/apps/tableau/connection/abc123/marker.txt`).
> App-level custom state (entity lists, caches, extracts) should also use singular
> `connection/` for consistency. Some older connectors used plural `connections/` --
> if migrating, standardize on singular to match the SDK convention.

### 2.2 Marker Lifecycle

```
READ (with prepone)        EXTRACT                   PERSIST (after publish)
       |                       |                            |
       v                       v                            v
  fetch_marker()        run extraction            persist_marker(now_iso)
  prepone by N min      using cutoff              only after upload_to_atlan
  to handle clock       from marker               succeeds
  skew / overlap
```

- **Prepone**: Subtract a safety window (e.g., 5 minutes) from the stored marker
  to handle clock skew between your system and the upstream API.
- **First run**: If no marker exists, treat as full extraction (all entities changed).
- **Persist timing**: ALWAYS persist the new marker AFTER `upload_to_atlan` succeeds.
  If upload fails, the next run re-extracts the same window — idempotent and safe.

### 2.3 Entity List (Filter-Change & Delete Detection)

Store the filtered entity IDs from each run:

```python
# After filtering, persist:
{
    "workbooks": ["wb-1", "wb-2", "wb-3"],
    "datasources": ["ds-10", "ds-20"],
    "run_timestamp": "2024-03-15T10:00:00Z"
}
# -> uploaded to filtered-<entities>.json
```

On next run, compare previous IDs vs. current IDs to detect:
- **Deletes**: `previous_ids - current_ids`
- **Filter changes**: If a project/folder was added or removed from the filter config,
  new entities appear and old ones disappear. Treat new entities as changed.

### 2.4 Index-Based Backfill (v2 Chunked Index)

The `_index.json` maps each entity to its chunk file and line offsets:

```json
{
    "ds-10": {
        "datasource": {
            "chunk": "datasource/result-0.json",
            "start": 0,
            "count": 45
        }
    },
    "ds-20": {
        "datasource": {
            "chunk": "datasource/result-0.json",
            "start": 45,
            "count": 30
        }
    },
    "ds-99": {
        "datasource": {
            "chunk": "datasource/result-1.json",
            "start": 0,
            "count": 60
        }
    }
}
```

### 2.5 Chunked Storage Rules

- **Target**: ~50MB per chunk file (stays within gRPC / S3 transfer limits).
- **Never split**: A single entity's NDJSON lines must NOT span two chunks.
  If adding an entity would exceed 50MB, start a new chunk.
- **Naming**: `<type>/result-N.json` where N is zero-indexed.
- **Format**: NDJSON (one JSON object per line) for streaming reads.

---

## Section 3: Backfill Template

### 3.1 Core Backfill Algorithm

```python
def backfill_unchanged_entities(
    changed_ids: set[str],
    deleted_ids: set[str],
    current_all_ids: set[str],
    index: dict,
    download_chunk_fn,
    output_dir: str,
):
    """
    Reconstruct complete output from fresh extraction + cached data.

    1. Download _index.json from S3 (small, always fits in memory)
    2. Determine unchanged = current_all - changed - deleted
    3. Group unchanged entities by chunk file
    4. Download only needed chunks (each <=50MB, within gRPC limit)
    5. Extract lines using index start/count offsets
    6. Write to output alongside fresh extraction results
    7. Free memory after each chunk (del content, lines)
    """
    unchanged = current_all_ids - changed_ids - deleted_ids

    # Group by chunk to minimize downloads
    chunk_to_entities: dict[str, list[str]] = {}
    for eid in unchanged:
        if eid not in index:
            continue  # New entity not in cache — will be in fresh extraction
        for entity_type, loc in index[eid].items():
            chunk = loc["chunk"]
            chunk_to_entities.setdefault(chunk, []).append(
                (eid, entity_type, loc["start"], loc["count"])
            )

    # Process one chunk at a time to bound memory
    for chunk_path, entities in chunk_to_entities.items():
        content = download_chunk_fn(chunk_path)
        lines = content.split("\n")

        for eid, entity_type, start, count in entities:
            extracted = lines[start : start + count]
            _append_to_output(output_dir, entity_type, extracted)

        # Explicit cleanup — chunks can be large
        del content, lines
```

### 3.2 v1 -> v2 Backward Compatibility

```python
def load_index(download_fn, base_path: str) -> dict | None:
    """
    Try v2 index first. If _index.json is missing (pre-v2 run),
    fall back to full scan (treat all entities as changed).
    """
    try:
        raw = download_fn(f"{base_path}/_index.json")
        return json.loads(raw)
    except FileNotFoundError:
        # v1 legacy: no index exists, force full extraction this run.
        # Next persist will create the v2 index going forward.
        return None
```

When `load_index` returns `None`, the caller should treat every entity as changed
(equivalent to a full run). The persist step at the end will write the v2 index,
so all subsequent runs benefit from incremental backfill.

---

## Section 4: S3 Artifact Layout Template

```
persistent-artifacts/apps/{app_name}/connection/{connection_id}/
|
|-- marker.txt
|   # ISO timestamp of last successful extraction completion.
|   # Written ONLY after upload_to_atlan succeeds.
|   # Read at start of next run, preponed by safety window.
|   # Lifecycle: created on first run, overwritten every run.
|
|-- filtered-workbooks.json
|   # Array of workbook IDs that passed the project/site filter
|   # on the previous run. Used for delete detection and
|   # filter-change detection on the next run.
|   # Lifecycle: overwritten every run after upload_to_atlan.
|
|-- filtered-datasources.json
|   # Same pattern for datasources (one file per entity type).
|
|-- workbook_cache.json
|   # Snapshot of workbook metadata from previous run.
|   # Used for field-level diffing if needed (optional).
|   # Lifecycle: overwritten every run.
|
|-- datasource-extracts/
|   |-- _index.json
|   |   # v2 index: maps each datasource ID to its chunk file,
|   |   # start line, and line count. Small file (~1KB per entity).
|   |   # Lifecycle: overwritten every run after upload_to_atlan.
|   |
|   |-- datasource/
|   |   |-- result-0.json    # NDJSON chunk (~50MB max)
|   |   |-- result-1.json    # Next chunk if result-0 exceeded limit
|   |   # Lifecycle: overwritten every run. Old chunks deleted
|   |   # before new ones are written to avoid stale data.
|   |
|   |-- column/
|       |-- result-0.json    # Column NDJSON, same chunking rules
|
|-- workbook-extracts/
    |-- _index.json
    |-- workbook/
    |   |-- result-0.json
    |-- sheet/
        |-- result-0.json
```

All files under this tree are **overwritten** on every successful run. There is no
append-only log. If a run fails before `upload_to_atlan`, the old state is preserved
and the next run retries cleanly.

---

## Section 5: Workflow Wiring Template

### 5.1 Activity Insertion Points

```
STEP  ACTIVITY                             INCREMENTAL?   NOTES
----  --------                             ------------   -----
1.    get_workflow_args                     existing       Parse connection config
1a.   read_marker                          << NEW >>      SDK fetch_marker_from_storage
1b.   read_previous_entity_list            << NEW >>      Download filtered-*.json from S3
2.    preflight_check                      existing       Validate credentials / permissions
3.    extract_sites                        existing       FULL always (cheap, parent entity)
4.    extract_projects                     existing       FULL always (cheap, parent entity)
5.    extract_workbooks                    existing       FULL always (needed for detection)
5a.   detect_updated_workbooks             << NEW >>      Marker comparison + false-positive filter
5b.   detect_deleted_workbooks             << NEW >>      previous_ids - current_ids
6.    extract_datasources                  existing       FULL always (needed for detection)
6a.   detect_updated_datasources           << NEW >>      Marker comparison + cascade from workbooks
6b.   detect_deleted_datasources           << NEW >>      previous_ids - current_ids
7.    extract_workbook_details             MODIFIED       Only changed workbook IDs (expensive)
7a.   backfill_unchanged_workbooks         << NEW >>      From S3 extracts via index
8.    extract_datasource_details           MODIFIED       Only changed datasource IDs (expensive)
8a.   backfill_unchanged_datasources       << NEW >>      From S3 extracts via index
9.    filter_data                          existing       Apply project/site filters
10.   transform                            existing       YAML transformers (unchanged)
11.   upload_to_atlan                      existing       Publish to Atlan (unchanged)
11a.  persist_incremental_state            << NEW >>      Marker + entity lists + cache + extracts
```

### 5.2 Key Design Rules

1. **Parent entities are ALWAYS extracted fully.** Sites, projects, workbooks,
   datasources — these are cheap list-API calls. Full extraction is required
   so that detection logic can compare `updatedAt` against the marker.

2. **Only expensive child/detail extractions are incremental.** Workbook details
   (sheets, dashboards, fields) and datasource details (columns, connections)
   are the expensive calls. These are the ones we skip for unchanged entities.

3. **State persistence is ALWAYS after `upload_to_atlan`.** If upload fails,
   the marker is not advanced and entity lists are not updated. The next run
   retries the exact same window. This guarantees no data loss.

4. **First run is always full.** When no marker exists, every entity is treated
   as changed. The persist step at the end writes the marker and index so
   subsequent runs are incremental.

5. **Backfill produces identical output.** The combination of fresh extraction
   (for changed entities) plus backfill (for unchanged entities) must produce
   output byte-identical to what a full extraction would produce. Downstream
   transform and upload steps see no difference.

### 5.3 Temporal Activity Registration

```python
# In workflows.py — register incremental activities alongside existing ones:

@workflow.defn
class ExtractionWorkflow:
    @workflow.run
    async def run(self, args: WorkflowArgs) -> WorkflowResult:
        # Phase 1: State loading (new)
        marker = await workflow.execute_activity(
            read_marker, args.connection_id, ...
        )
        prev_entities = await workflow.execute_activity(
            read_previous_entity_list, args.connection_id, ...
        )

        # Phase 2: Full parent extraction (existing, unchanged)
        workbooks = await workflow.execute_activity(extract_workbooks, ...)
        datasources = await workflow.execute_activity(extract_datasources, ...)

        # Phase 3: Detection (new)
        changed_wb, deleted_wb = await workflow.execute_activity(
            detect_changes, workbooks, marker, prev_entities, ...
        )

        # Phase 4: Selective detail extraction (modified)
        if changed_wb:
            await workflow.execute_activity(
                extract_workbook_details, list(changed_wb), ...
            )
        await workflow.execute_activity(
            backfill_unchanged, changed_wb, deleted_wb, ...
        )

        # Phase 5: Transform + Upload (existing, unchanged)
        await workflow.execute_activity(transform, ...)
        await workflow.execute_activity(upload_to_atlan, ...)

        # Phase 6: State persistence (new, MUST be after upload)
        await workflow.execute_activity(
            persist_incremental_state, marker_now, workbooks, datasources, ...
        )
```
