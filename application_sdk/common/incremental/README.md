# Incremental Metadata Extraction Framework

This directory contains the core utilities for incremental SQL metadata extraction in the Application SDK.

## Overview

The incremental extraction framework enables efficient metadata extraction by:
1. Tracking changes via marker timestamps
2. Extracting only modified tables/columns instead of full extractions
3. Preserving ancestral state for unchanged entities
4. Generating incremental diffs for efficient publishing

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Incremental Extraction Flow                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Phase 1: Setup                                                     │
│   ┌──────────────┐    ┌─────────────────┐    ┌────────────────┐     │
│   │ Fetch Marker │───▶│ Read Current    │───▶│ Preflight      │     │
│   │ Timestamp    │    │ State from S3   │    │ Check          │     │
│   └──────────────┘    └─────────────────┘    └────────────────┘     │
│                                                                      │
│   Phase 2: Base Extraction                                           │
│   ┌──────────────┐    ┌─────────────────┐    ┌────────────────┐     │
│   │ Fetch DBs    │───▶│ Fetch Schemas   │───▶│ Fetch Tables   │     │
│   └──────────────┘    └─────────────────┘    └────────────────┘     │
│                                                                      │
│   Phase 3: Incremental Columns                                       │
│   ┌──────────────────┐    ┌─────────────────────────────────────┐   │
│   │ Prepare Column   │───▶│ Execute Column Batches (parallel)   │   │
│   │ Queries          │    │ - Changed tables: Fresh extraction  │   │
│   └──────────────────┘    │ - Backfill tables: Full extraction  │   │
│                           └─────────────────────────────────────┘   │
│                                                                      │
│   Phase 4: Finalization                                              │
│   ┌──────────────────┐    ┌─────────────────┐                       │
│   │ Write Current    │───▶│ Update Marker   │                       │
│   │ State + Diff     │    │ Timestamp       │                       │
│   └──────────────────┘    └─────────────────┘                       │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
application_sdk/common/incremental/
├── README.md                  # This file
├── models.py                  # Pydantic models for workflow args and metadata
├── helpers.py                 # S3 path management, file utilities
├── table_scope.py             # Table scope detection and state management
├── ancestral_merge.py         # Column merging for unchanged tables
├── incremental_diff.py        # Diff generation for changed entities
├── state/                     # State management (marker + current state)
│   ├── marker.py              # Marker timestamp fetch/persist helpers
│   ├── state_reader.py        # Download current state from S3
│   └── state_writer.py        # Create and upload current state snapshot
└── storage/                   # Storage backends
    ├── duckdb_utils.py        # DuckDB connection management
    └── rocksdb_utils.py       # RocksDB disk-backed state storage
```

## File Descriptions

### Core Files

| File | Purpose | Used By |
|------|---------|---------|
| `models.py` | Pydantic models for `IncrementalWorkflowArgs`, `EntityType`, `TableScope`, merge results | Activities, workflows |
| `helpers.py` | S3 path generation, file operations | Activities |
| `table_scope.py` | Detect table incremental states (CREATED/UPDATED/NO CHANGE) via DuckDB queries | `write_current_state` |
| `ancestral_merge.py` | Merge current columns with ancestral data for NO CHANGE tables | `write_current_state` |
| `incremental_diff.py` | Generate folder with only changed assets for efficient publishing | `write_current_state` |

### State Management (state/)

| File | Purpose |
|------|---------|
| `marker.py` | Marker timestamp management: fetch from S3, validate, prepone, and persist |
| `state_reader.py` | Download previous run's current-state snapshot from S3 |
| `state_writer.py` | Create new current-state snapshot with ancestral merge and upload to S3 |

#### Key Functions

**marker.py:**
- `fetch_marker_from_storage()` - Download and process existing marker
- `persist_marker_to_storage()` - Upload marker after successful extraction
- `create_next_marker()` - Generate timestamp for current run
- `process_marker_timestamp()` - Normalize and optionally prepone marker

**state_reader.py:**
- `download_current_state()` - Download current-state folder from S3

**state_writer.py:**
- `create_current_state_snapshot()` - High-level orchestrator for state creation
- `download_transformed_data()` - Download current run's transformed output
- `prepare_previous_state()` - Download previous state for comparison
- `copy_non_column_entities()` - Copy tables, schemas, databases
- `upload_current_state()` - Upload final snapshot to S3
- `cleanup_previous_state()` - Clean up temporary files

### Storage Backends (storage/)

| File | Purpose |
|------|---------|
| `duckdb_utils.py` | DuckDB connection manager for efficient JSON file querying |
| `rocksdb_utils.py` | RocksDB (Rdict) for disk-backed table state storage |

## Key Concepts

### Marker Timestamp

The marker timestamp tracks when the last successful extraction occurred. It's stored in S3 at:
```
persistent-artifacts/apps/{app}/connection/{connection_id}/marker.txt
```

During extraction, queries use this timestamp to filter for changed tables:
```sql
WHERE last_modified_time > '{marker_timestamp}'
```

### Current State

The current state is a snapshot of all extracted metadata, stored in S3 at:
```
persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/
├── database/
├── schema/
├── table/
└── column/
```

### Table Incremental States

Tables are classified based on comparison with previous state:

| State | Description | Column Handling |
|-------|-------------|-----------------|
| `CREATED` | New table (not in previous state) | Extract fresh columns |
| `UPDATED` | Modified table (DDL changed) | Extract fresh columns |
| `NO CHANGE` | Unchanged table | Use ancestral columns |
| `BACKFILL` | Needs backfill (custom logic) | Extract fresh columns |

### Ancestral Column Merge

For unchanged tables, we preserve the previous run's column data instead of re-extracting:

```
Current Columns (CREATED/UPDATED tables)  +  Ancestral Columns (NO CHANGE tables)
                      ↓                                        ↓
                                 New Current State
```

### Incremental Diff

The incremental diff contains only changed assets from the current run:
```
persistent-artifacts/apps/{app}/connection/{connection_id}/runs/{run_id}/incremental-diff/
├── table/      # Only CREATED/UPDATED/BACKFILL tables
├── column/     # Only columns from CREATED/UPDATED/BACKFILL tables
└── ...
```

This enables efficient publishing where only changes are processed.

## Usage

### Implementing Database-Specific Extraction

1. **Extend `IncrementalSQLMetadataExtractionActivities`**:
   ```python
   class OracleMetadataExtractionActivities(IncrementalSQLMetadataExtractionActivities):
       async def resolve_database_placeholders(self, query, workflow_args):
           # Replace {marker_timestamp}, {system_schema} with Oracle-specific values
           pass
       
       async def prepare_column_extraction_queries(self, workflow_args):
           # Generate Oracle-specific column queries for batched extraction
           pass
   ```

2. **Extend `IncrementalSQLMetadataExtractionWorkflow`**:
   ```python
   @workflow.defn
   class OracleMetadataExtractionWorkflow(IncrementalSQLMetadataExtractionWorkflow):
       activities_cls = OracleMetadataExtractionActivities
   ```

### Constants (in `application_sdk/constants.py`)

| Constant | Purpose | Default |
|----------|---------|---------|
| `PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE` | S3 path for persistent artifacts | `persistent-artifacts/apps/{application_name}/connection/{connection_id}` |
| `MAX_CONCURRENT_COLUMN_BATCHES` | Parallel column extraction batches | `3` |
| `MARKER_TIMESTAMP_FORMAT` | Timestamp format for markers | `%Y-%m-%dT%H:%M:%SZ` |
| `INCREMENTAL_DEFAULT_STATE` | Default state for first run | `NO CHANGE` |
| `DUCKDB_COMMON_TEMP_FOLDER` | Temp folder for DuckDB files | `/tmp/incremental_duckdb` |
| `DUCKDB_DEFAULT_MEMORY_LIMIT` | DuckDB memory limit | `2GB` |

## Testing

See `tests/unit/common/test_incremental_*.py` for unit tests of the utilities.

## Related Files

- `application_sdk/activities/metadata_extraction/incremental.py` - Incremental activities
- `application_sdk/workflows/metadata_extraction/incremental_sql.py` - Incremental workflow
