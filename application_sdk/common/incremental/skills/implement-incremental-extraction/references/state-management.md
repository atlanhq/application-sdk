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
  2. read_current_state: Downloads current-state/ → current_state_available ✓
  3. All prerequisites met → incremental mode activates
  4. fetch_tables: Uses incremental_table_sql (labels CREATED/UPDATED/NO CHANGE)
  5. fetch_columns: SKIPPED (handled by incremental pipeline)
  6. prepare_column_extraction_queries:
     - Downloads transformed table JSONs
     - Daft analyzes incremental_state labels
     - DuckDB detects backfill tables
     - Batches table_ids into JSON files
  7. execute_single_column_batch (parallel):
     - Downloads batch JSON
     - Calls build_incremental_column_sql() → app provides SQL
     - Executes SQL → extracts columns for changed tables only
  8. write_current_state:
     a. Download current run's transformed output
     b. Download previous current-state (becomes "previous state")
     c. Detect table scope (which tables are CREATED/UPDATED/NO CHANGE)
     d. Copy non-column entities (table, schema, database) to new current-state
     e. Ancestral column merge:
        - CREATED/UPDATED tables: Use current run's fresh columns
        - NO CHANGE tables: Carry forward ancestral columns from previous state
        - Tables removed from scope: Drop ancestral columns
     f. Create incremental-diff (only changed assets)
     g. Upload new current-state/ to S3
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
# fetch_marker_from_storage() in marker.py

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
current-state/
├── database/
│   └── database_0.json, database_1.json, ...
├── schema/
│   └── schema_0.json, schema_1.json, ...
├── table/
│   └── table_0.json, table_1.json, ...
└── column/
    └── column_0.json, column_1.json, ...
```

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

## Ancestral Column Merge

The most complex part of state management. Ensures unchanged tables keep their
columns from the previous run without re-extracting.

### Algorithm

```python
# Simplified from ancestral_merge.py

def merge_ancestral_columns(
    current_column_dir,        # Columns from this run (CREATED/UPDATED tables)
    previous_column_dir,       # Columns from previous state
    current_state_column_dir,  # Output directory
    table_scope,               # Which tables are in scope and their states
    column_chunk_size,         # Output file chunk size
):
    # Step 1: Copy current columns (from CREATED/UPDATED tables)
    for json_file in current_column_dir.glob("*.json"):
        for entity in read_json(json_file):
            table_qn = extract_table_qn(entity)
            if table_qn in table_scope.table_qualified_names:
                write_to_output(entity)
                columns_from_current += 1

    # Step 2: Carry forward ancestral columns (from NO CHANGE tables)
    for json_file in previous_column_dir.glob("*.json"):
        for entity in read_json(json_file):
            table_qn = extract_table_qn(entity)

            # Skip if table was extracted this run (fresh columns exist)
            if table_qn in table_scope.tables_with_extracted_columns:
                excluded_already_extracted += 1
                continue

            # Skip if table no longer in extraction scope
            if table_qn not in table_scope.table_qualified_names:
                excluded_table_removed += 1
                continue

            # Carry forward this ancestral column
            write_to_output(entity)
            columns_from_ancestral += 1
```

### Merge Decision Matrix

| Table State | Has Current Columns? | Action |
|-------------|---------------------|--------|
| CREATED | Yes | Use current columns |
| UPDATED | Yes | Use current columns |
| BACKFILL | Yes | Use current columns |
| NO CHANGE | No | Carry ancestral columns |
| Removed from scope | N/A | Drop ancestral columns |

## Incremental Diff

The incremental diff contains only changed assets from this specific run:

```
incremental-diff/
├── table/     # Only CREATED/UPDATED/BACKFILL tables
├── column/    # Only columns for CREATED/UPDATED/BACKFILL tables
├── schema/    # All schemas (always included)
└── database/  # All databases (always included)
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

```python
# table_scope.py uses DuckDB to classify tables

def get_current_table_scope(transformed_dir, column_chunk_size):
    conn = get_duckdb_connection()

    # Read all table JSONs and extract qualified names + states
    result = conn.sql(f"""
        SELECT
            json_extract_string(attributes, '$.qualifiedName') AS qn,
            json_extract_string(attributes, '$.incremental_state') AS state
        FROM read_json_auto('{transformed_dir}/table/*.json')
    """).fetchall()

    scope = TableScope()
    for qn, state in result:
        scope.table_qualified_names.add(qn)
        scope.table_states[qn] = state or "NO CHANGE"

    return scope
```

## Cleanup and Error Handling

### Previous State Cleanup

Previous state is downloaded to a temp directory for comparison. This temp
directory is cleaned up in a `finally` block to ensure no leaks:

```python
# In write_current_state activity
previous_state_dir = None
try:
    previous_state_dir = await prepare_previous_state(...)
    result = await create_current_state_snapshot(...)
except Exception as e:
    raise
finally:
    cleanup_previous_state(previous_state_dir)  # Always cleanup
```

### Stale State Prevention

Before downloading current state from S3, existing local files are cleared:

```python
# In state_reader.py
import shutil

if current_state_dir.exists():
    shutil.rmtree(current_state_dir)  # Clear stale files from prior runs
current_state_dir.mkdir(parents=True, exist_ok=True)
```

This prevents leftover JSON files from a previous run's state from contaminating
the current run's state comparison.

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
