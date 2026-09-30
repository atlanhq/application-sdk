# State Management Deep Dive

This reference covers how the SDK manages incremental extraction state internally.
App developers don't need to implement any of this, but understanding it helps
with debugging and extending the framework.

## State Lifecycle

```
Run 1 (Full Extraction):
  1. No marker.txt exists → full extraction runs
  2. All tables/columns extracted normally
  3. write_current_state: Copies transformed output → current-state/
  4. update_marker: Writes next_marker_timestamp → marker.txt
  5. State: marker.txt ✓, current-state/ ✓

Run 2 (Incremental Extraction):
  1. fetch_marker: Reads marker.txt → marker_timestamp ✓
  2. read_current_state: Probes current-state/ (one listing + the manifest,
     no download) → current_state_available ✓
  3. All prerequisites met → incremental mode activates
  4. fetch_tables: Uses incremental_table_sql (labels CREATED/UPDATED/NO CHANGE)
  5. fetch_columns: SKIPPED (handled by incremental pipeline)
  6. prepare_column_extraction_queries:
     - Downloads transformed table JSONs
     - Materializes the committed snapshot into
       {output_path}/incremental/previous-state/
     - DuckDB analyzes incremental_state labels
     - DuckDB detects backfill tables
     - Batches table_ids into JSON files
  7. execute_single_column_batch (parallel):
     - Downloads batch JSON
     - Calls build_incremental_column_sql() → app provides SQL
     - Executes SQL → extracts columns for changed tables only
  8. write_current_state:
     a. Download current run's transformed output
     b. Materialize the committed snapshot into
        {output_path}/incremental/previous-state/ (synced: a same-run retry
        resumes, a partial tree is completed and pruned, never trusted)
     c. Detect table scope (which tables are CREATED/UPDATED/NO CHANGE)
     d. Copy non-column entities (table, schema, database) to
        {output_path}/incremental/current-state/
     e. Lightweight column copy:
        - Copy columns from current run's transformed output only
        - NO CHANGE table columns are NOT carried forward
        - Publish-cache is the source of truth for complete column state
     f. Create incremental-diff (only changed assets, with deletion detection):
        - delete/table/: Tables in previous state but absent from current scope
        - delete/column/: Cascade from deleted tables + columns missing from UPDATED tables
     g. Upload the diff, then commit current-state/ via CurrentStateStore:
        upload (run-stamped file names) → write .sdk-manifest LAST → prune
        every key the manifest does not name
  9. update_marker: Writes next_marker_timestamp → marker.txt
```

## Marker Management

### Marker File

```
S3: persistent-artifacts/apps/{app}/connection/{conn_id}/marker.txt
Content: "2024-06-15T00:00:00Z" (single line, UTC ISO 8601)
```

### Marker Flow

```python
# fetch_marker() in marker.py

1. Download marker.txt from S3 (via ObjectStore)
2. If not found → first run, marker = None
3. If found → parse timestamp
4. If prepone_marker_timestamp enabled:
   marker = marker - prepone_marker_hours  # Default: 3 hours back
5. Create next_marker = current UTC time
6. Return (marker, next_marker)
```

### Prepone Logic

The marker is moved back by `prepone_marker_hours` (default: 3) to handle:
- Clock drift between extraction and database
- Transactions committed after marker was set but before extraction started
- Database replication lag

```
Actual marker: 2024-06-15T12:00:00Z
Preponed by 3h: 2024-06-15T09:00:00Z  ← This is what SQL queries use
```

## Current State Structure

```
persistent-artifacts/apps/{app}/connection/{conn_id}/current-state/
├── .sdk-manifest            # names every key of the snapshot; written LAST
├── database/
│   └── {stamp}--chunk-0.json, ...
├── schema/
│   └── {stamp}--chunk-0.json, ...
├── table/
│   └── {stamp}--chunk-0.json, ...
└── column/
    └── {stamp}--chunk-0.json, ...
```

`{stamp}` is 12 hex characters derived from the committing run ID, so a new
commit never overwrites a key the previous manifest names. Read entity files
with a `{entity}/*.json` glob — never by file name. This layout is a
cross-repo contract (`docs/standards/cross-repo-contracts.md`).

### Working with the snapshot in code

```python
from application_sdk.common.incremental.state.store import (
    CurrentStateStore,
    RunStateDirs,
)

store = CurrentStateStore.for_connection(connection_qualified_name, application_name)
snapshot = await store.probe()          # one listing + manifest; downloads nothing
dirs = RunStateDirs.for_output_path(output_path)
if snapshot.exists:
    await store.materialize(snapshot, dirs.previous_state)   # exact mirror, synced
# ... build this run's snapshot under dirs.current_state ...
await store.commit(dirs.current_state, run_id)   # upload → manifest → prune
```

Inside `IncrementalSqlMetadataExtractor` you do not call these yourself: the
template's `read_current_state`, `prepare_column_extraction_queries` and
`write_current_state` tasks do. To run connector logic over the snapshot at
read time, override the `after_current_state_read(snapshot, local_dir)` hook.

Each JSON file contains Atlas-format entities:

```json
{
  "typeName": "Table",
  "status": "ACTIVE",
  "attributes": {
    "qualifiedName": "default/oracle/1234567890/MYDB/SCHEMA1/TABLE1",
    "name": "TABLE1",
    "incremental_state": "CREATED"
  }
}
```

## Lightweight Column Copy

Columns are copied directly from the current run's transformed output. There is
no ancestral merge — NO CHANGE table columns are not carried forward into
current-state. Publish-cache is the source of truth for complete column state
across all runs.

### Algorithm

`create_current_state_snapshot()` in `state_writer.py` builds the run's
snapshot in `{output_path}/incremental/current-state/`:

1. Reset that directory (a same-run retry reuses it).
2. `copy_non_column_entities()` — copy `table/`, `schema/` and `database/`
   from this run's transformed output.
3. Copy `column/` from this run's transformed output — nothing else.
4. Build the diff in `{output_path}/incremental/diff/` and upload it.
5. `CurrentStateStore.commit()` — only after the diff is durable.

Every step is offloaded with `run_in_thread`; do not call these helpers
inline from connector code.

### Column Handling by Table State

| Table State | Columns in transformed output? | Action |
|-------------|-------------------------------|--------|
| CREATED | Yes | Copied to current-state |
| UPDATED | Yes | Copied to current-state |
| BACKFILL | Yes | Copied to current-state |
| NO CHANGE | No | Not copied (not re-extracted) |
| Deleted | N/A | Emitted to incremental-diff delete/column/ |

### Deletion Detection

`create_incremental_diff()` in `incremental_diff.py` detects two deletion
scenarios using DuckDB set-difference operations:

1. **Table-level deletions**: Tables present in the previous current-state but
   absent from the current extraction scope → written to `delete/table/`
2. **Column-level cascade**: All columns of deleted tables → written to
   `delete/column/`
3. **Column-level diff for UPDATED tables**: Columns in previous state but
   missing from the current extraction of a re-extracted (UPDATED) table →
   written to `delete/column/`

## Incremental Diff

The incremental diff contains only changed assets from this specific run:

```
incremental-diff/
├── table/          # Only CREATED/UPDATED/BACKFILL tables
├── column/         # Only columns for CREATED/UPDATED/BACKFILL tables
├── schema/         # All schemas (always included)
├── database/       # All databases (always included)
├── delete/
│   ├── table/      # Tables deleted since previous run
│   └── column/     # Columns deleted (cascade from table + UPDATED table diffs)
└── metadata.json   # Entity counts for Argo routing
```

### Purpose

1. **Debugging**: See exactly what changed in this run
2. **Efficient publishing**: Publish only changed assets (future)
3. **Audit trail**: Track changes per run

### S3 Path

```
persistent-artifacts/apps/{app}/connection/{conn_id}/runs/{run_id}/incremental-diff/
```

## Table Scope Detection

`get_current_table_scope(transformed_dir, conn=None)` in `table_scope.py`
scans `{transformed_dir}/table/*.json` with DuckDB and returns a `TableScope`:
each table's qualified name and its `incremental_state` (defaulting to
`INCREMENTAL_DEFAULT_STATE`, `NO CHANGE`). The states are kept in a
disk-backed RocksDB store, so release it with `close_scope(scope)` when done.

```python
from application_sdk.common.incremental.state.table_scope import (
    close_scope,
    get_current_table_scope,
    get_scope_length,
    get_table_state,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)

with DuckDBConnectionManager() as manager:
    scope = get_current_table_scope(transformed_dir, conn=manager.connection)
    try:
        print(get_scope_length(scope), get_table_state(scope, some_table_qn))
    finally:
        close_scope(scope)
```

## Commit Safety and Local Directories

### Nothing to clean up

Every local directory is run-scoped under `{output_path}/incremental/`
(`previous-state/`, `current-state/`, `diff/`). Two runs of one connection on a
worker never share one, and there is no per-connection directory to clear or
remove — no `finally` cleanup, no `rmtree` of a shared path.

### Stale state cannot leak in

`CurrentStateStore.materialize` mirrors exactly the manifest's keys into the
directory with sync semantics: files already current are skipped, and anything
else in the directory (a killed attempt's partial download, a stray file) is
deleted. So a leftover tree is never mistaken for the previous state.

### A commit that fails before its manifest changes nothing

`CurrentStateStore.commit` writes the manifest only after every file is
uploaded, and file names carry a stamp of the committing run, so the upload
never overwrites a key the previous manifest names. A commit that dies before
its manifest leaves the previous snapshot committed and whole, and the marker
is only advanced after `write_current_state` succeeds.

The manifest write is the commit point. The prune runs after it, so a commit
that fails *in the prune* has already committed: readers see the new snapshot,
and the keys it did not get to delete are left for a later commit's prune.
Every reader that goes through the manifest ignores them already.

## Configuration Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `incremental-extraction` | `false` | Enable/disable incremental mode |
| `column-batch-size` | `25000` | Tables per batch for column extraction |
| `column-chunk-size` | `100000` | Column records per output JSON file |
| `copy-workers` | `3` | Parallel workers for file copy operations |
| `prepone-marker-timestamp` | `true` | Move marker back to handle clock drift |
| `prepone-marker-hours` | `3` | Hours to move marker back |
| `system-schema-name` | `SYS` | System schema for metadata queries (Oracle) |
