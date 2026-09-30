# Incremental Metadata Extraction

This package owns a connection's cross-run incremental state: the marker
(watermark), the committed current-state snapshot, and the per-run incremental
diff. Its centre is `CurrentStateStore`, which probes, materializes and commits
the snapshot, and `RunStateDirs`, which gives each run its own local
directories.

## Overview

An incremental run:

1. Reads the marker to learn when the last successful extraction happened.
2. Probes the committed current-state snapshot (no download).
3. Extracts only the tables changed since the marker, then fresh columns for
   just those tables (plus backfill tables newly entering the filter).
4. Builds a new snapshot from this run's transformed output, diffs it against
   the previous one (with deletion detection), uploads the diff, and commits
   the snapshot.
5. Advances the marker, only after the commit succeeded.

A missing marker or a missing snapshot means a full extraction.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     Incremental Extraction Flow                      │
├──────────────────────────────────────────────────────────────────────┤
│  Phase 1: Prerequisites                                              │
│    fetch_incremental_marker ──▶ read_current_state                   │
│    (marker.txt, in memory)      (CurrentStateStore.probe)            │
│                                                                      │
│  Phase 2: Base extraction                                            │
│    fetch_databases ┐                                                 │
│    fetch_schemas   ┴─▶ fetch_tables ──▶ fetch_columns                │
│                                         (skipped when incremental)   │
│                                                                      │
│  Phase 3: Incremental columns (only when incremental-ready)          │
│    prepare_column_extraction_queries ──▶ execute_single_column_batch │
│    (materialize previous-state,          × N, MAX_CONCURRENT_COLUMN_ │
│     DuckDB change + backfill analysis)   BATCHES (10) at a time      │
│                                                                      │
│  Phase 4: Write state                                                │
│    write_current_state                                               │
│    (materialize → build → diff → upload diff → CurrentStateStore.    │
│     commit)                                                          │
│                                                                      │
│  Phase 5: Update marker                                              │
│    update_incremental_marker                                         │
└──────────────────────────────────────────────────────────────────────┘
```

The phases are the `run()` of
`application_sdk.templates.IncrementalSqlMetadataExtractor` (see
[Usage](#usage)); the helpers in this package are usable without it.

## Directory Structure

```
application_sdk/common/incremental/
├── README.md                  # This file
├── __init__.py                # Supported surface: prefix + marker helpers
├── models.py                  # EntityType, TableScope, IncrementalDiffResult, ...
├── helpers.py                 # Persistent-artifacts prefix/path, file utilities
├── marker.py                  # Marker fetch/persist helpers
├── incremental_errors.py      # Typed errors (StateDownloadError, CurrentStateManifestError, ...)
├── column_extraction/         # DuckDB analysis of which tables need columns
│   ├── analysis.py            # get_tables_needing_column_extraction
│   └── backfill.py            # get_backfill_tables
├── state/
│   ├── store.py               # CurrentStateStore, CurrentStateSnapshot, RunStateDirs
│   ├── state_writer.py        # Build, diff and commit a run's snapshot
│   ├── state_reader.py        # Deprecated download_current_state shim
│   ├── table_scope.py         # Table scope detection (CREATED/UPDATED/NO CHANGE)
│   └── incremental_diff.py    # Diff generation with deletion detection
├── storage/
│   ├── duckdb_utils.py        # DuckDBConnectionManager, JSON-scan helpers
│   └── rocksdb_utils.py       # RocksDB (Rdict) disk-backed table state
└── skills/                    # Agent skills for implementing/migrating incremental apps
```

## Key Components

### `CurrentStateStore` and `RunStateDirs` (`state/store.py`)

| Symbol | Purpose |
|--------|---------|
| `CurrentStateStore(s3_prefix, store=None)` | The store for one current-state prefix (`.../connection/{id}/current-state`) |
| `CurrentStateStore.for_connection(connection_qualified_name, application_name="")` | The store for a connection, via `get_persistent_s3_prefix` |
| `CurrentStateStore.probe()` | One listing plus the manifest read: returns a `CurrentStateSnapshot` (`exists`, `json_count`, `total_bytes`, `committed_run_id`, `keys`). Downloads nothing. |
| `CurrentStateStore.materialize(snapshot, dest)` | Mirror exactly the snapshot's keys into `dest` with sync semantics (already-current files skipped, anything else in `dest` deleted), under a per-directory lock |
| `CurrentStateStore.commit(local_dir, run_id)` | Stamp file names with the run, upload, write the manifest (the commit point), then prune every key the manifest does not name, and re-upload any named key the store lost. Assumes one run per connection at a time |
| `RunStateDirs.for_output_path(output_path)` | `{output_path}/incremental/` with `.previous_state`, `.current_state` and `.diff` |

### State writer (`state/state_writer.py`)

- `materialize_previous_state(store, dest, snapshot=None)` — probe (unless
  given a snapshot) and materialize; any failure is raised as
  `StateDownloadError`, because a diff without the previous state cannot
  detect deletions.
- `download_transformed_data(output_path)` — download this run's
  `transformed/` output.
- `create_current_state_snapshot(...)` — table scope, copy entities into the
  run's `current-state/`, build and upload the diff, then
  `CurrentStateStore.commit`.
- `copy_non_column_entities(transformed_dir, current_state_dir)` — copy
  table, schema and database files.

### Marker (`marker.py`)

- `fetch_marker()` — read and process the stored marker; returns a
  `MarkerPair(marker, next_marker)`, with `marker=None` on the first run.
- `persist_marker()` — write the marker after a successful run; returns a
  `MarkerPersistResult(marker_timestamp, s3_key)`.
- `fetch_marker_from_storage()` / `persist_marker_to_storage()` — deprecated
  (removal in v4.0.0); the same calls returning the old `(marker, next_marker)`
  tuple and `{"marker_written", "marker_timestamp", "local_path", "s3_key"}`
  dict.
- "No marker" is `None` in Python and `""` on the task contracts;
  `marker_to_wire()` / `marker_from_wire()` in
  `application_sdk.templates.contracts.incremental_sql` convert between them.
- `create_next_marker()` — the timestamp for the current run.
- `process_marker_timestamp()` — normalize and optionally prepone a marker.

### Table scope and diff

- `get_current_table_scope(transformed_dir, conn=None)` — table qualified
  names and incremental states from the transformed output.
- `get_table_qns_from_columns(column_dir, conn=None)` — tables that have
  extracted columns.
- `create_incremental_diff(...)` — the diff folder, with deletion detection.

### Deprecated (removed in v4.0.0)

Each still works and emits a `DeprecationWarning`.

| Deprecated | Use instead |
|------------|-------------|
| `state_reader.download_current_state()` | `CurrentStateStore.probe()`, then `materialize()` only when the files are needed |
| `state_writer.prepare_previous_state()` | `materialize_previous_state()` into `RunStateDirs.previous_state` |
| `state_writer.upload_current_state()` | `CurrentStateStore.commit()` |
| `state_writer.prepare_current_state_directory()` | Nothing: `create_current_state_snapshot` resets its own build directory |
| `state_writer.cleanup_previous_state()` | Nothing: run-scoped directories need no cleanup |
| Overriding `read_current_state` on the template | Override `after_current_state_read(snapshot, local_dir)` |
| `marker.fetch_marker_from_storage()` (tuple) | `marker.fetch_marker()` (`MarkerPair`) |
| `marker.persist_marker_to_storage()` (dict) | `marker.persist_marker()` (`MarkerPersistResult`) |
| `IncrementalSqlMetadataExtractor` (the template itself) | `application_sdk.templates.SqlApp` with a custom `run()` |

## Key Concepts

### Marker Timestamp

The marker records when the last successful extraction started. It is stored
at:

```
persistent-artifacts/apps/{app}/connection/{connection_id}/marker.txt
```

A missing marker means a first run and triggers a full extraction. Any other
storage error while reading the marker or the current state raises, so the
task retries instead of silently running a full extraction. The marker is
read into memory and uploaded from memory; there is no local `marker.txt`.

By default the marker is preponed by 3 hours (`prepone_marker_timestamp`,
`prepone_marker_hours`) to absorb clock drift and late-committed
transactions. Queries then filter on it:

```sql
WHERE last_modified_time > '{marker_timestamp}'
-- and label each row: incremental_state = 'CREATED' | 'UPDATED' | 'NO CHANGE'
```

### Current State

The current state is the snapshot the next run diffs against, stored at:

```
persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/
├── .sdk-manifest     # names every key of the committed snapshot; written last
├── database/
├── schema/
├── table/            # {run-stamp}--chunk-0.json, ...
└── column/
```

It is lightweight: it holds only what this run extracted. `NO CHANGE` tables'
columns are not carried forward; publish-cache is the source of truth for
complete state.

The layout is public — it is a cross-repo contract (see
`docs/standards/cross-repo-contracts.md`): Argo publish passes this prefix as
`transformed-input-path` and globs it with `**/*.json`, and connector apps
read `current-state/{entity}/` directly. So:

- **The manifest is the commit.** `CurrentStateStore.commit` uploads the new
  snapshot, then writes `.sdk-manifest`, then deletes every key it does not
  name. A reader trusts the manifest over the listing, so a commit that dies
  before its manifest leaves the previous snapshot committed and whole. The
  name is dot-prefixed and has no `.json` suffix so the publish glob never
  picks it up.
- **File names carry a run stamp** (`{12 hex}--chunk-0.json`, derived from
  the committing run ID). A new commit therefore never overwrites a key the
  previous manifest names, which is what makes a partial upload harmless; a
  retry of the same run derives the same stamp and overwrites only its own
  keys. Readers glob `{entity}/*.json` and do not depend on file names.
- **A pre-manifest snapshot** (written by an older SDK) is read from its
  listing, ignoring any run-stamped key. Its stale keys are pruned by the first
  commit after upgrade — so that run's publish may emit deletions for assets
  already gone at source, which is correct.
- **A retry after the commit point** finds `committed_run_id` equal to its own
  run and reports that commit instead of rebuilding (which would diff the
  snapshot against itself).

### Run-scoped Local Directories

All local state work is under `{output_path}/incremental/`:

```
{output_path}/incremental/
├── previous-state/   # the committed snapshot, materialized
├── current-state/    # this run's snapshot, built before its commit
└── diff/             # this run's incremental diff, built before its upload
```

Two runs of a connection on one worker never share a directory, and a
same-run retry resumes in its own, so nothing needs cleaning up.
`read_current_state` only probes; `prepare_column_extraction_queries` and
`write_current_state` materialize into `previous-state/` when they need the
files. A connector that needs the snapshot on disk at read time overrides
`after_current_state_read(snapshot, local_dir)`.

### Table Incremental States

| State | Description | Column Handling |
|-------|-------------|-----------------|
| `CREATED` | New table (not in previous state) | Extract fresh columns |
| `UPDATED` | Modified table (DDL changed) | Extract fresh columns |
| `NO CHANGE` | Unchanged table | No column extraction |
| `BACKFILL` | In scope now, absent from the previous state (e.g. a filter change) | Extract fresh columns |

### Deletion Detection

DuckDB set-difference operations identify:

- **Deleted tables**: in the previous state but absent from this run's scope.
- **Deleted columns**: every column of a deleted table, plus columns missing
  from a re-extracted (`UPDATED`) table.

### Incremental Diff

The diff holds only this run's changes:

```
persistent-artifacts/apps/{app}/connection/{connection_id}/runs/{run_id}/incremental-diff/
├── table/            # CREATED/UPDATED/BACKFILL tables
├── column/           # columns of those tables
├── schema/           # all schemas
├── database/         # all databases
├── delete/table/     # deleted tables
├── delete/column/    # deleted columns
└── metadata.json     # entity counts for Argo routing
```

`metadata.json` decides the publish mode: diff present with entities → stream
publish; no diff (full extraction) → batch publish; diff present with zero
entities → skip. Its keys are part of the same cross-repo contract. The diff is
uploaded before the snapshot is committed, so a committed snapshot always has
a durable diff behind it.

## Usage

`application_sdk.templates.IncrementalSqlMetadataExtractor` wires all of the
above into a five-phase `run()`. It is **deprecated** (removed in v4.0.0;
the replacement is `application_sdk.templates.SqlApp` with a custom `run()`),
but it is still the SDK's only built-in incremental orchestration.

```python
from application_sdk.app import task
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts.incremental_sql import (
    FetchColumnsIncrementalInput,
    FetchTablesIncrementalInput,
    IncrementalRunContext,
)
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsOutput,
    FetchTablesOutput,
)


class MyExtractor(IncrementalSqlMetadataExtractor):
    sql_client_class = MyClient
    incremental_table_sql = "SELECT ... WHERE last_modified > '{marker_timestamp}'"
    incremental_column_sql = "SELECT ... WHERE table_id IN ({table_ids})"

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesIncrementalInput) -> FetchTablesOutput:
        ...  # full vs incremental SQL — see the task's docstring

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsIncrementalInput) -> FetchColumnsOutput:
        ...  # return FetchColumnsOutput() when incremental; full SQL otherwise

    def build_incremental_column_sql(
        self, table_ids: list[str], ctx: IncrementalRunContext
    ) -> str:
        ...

    async def execute_column_sql(self, sql, input, ctx) -> int:
        ...  # run the batch SQL, write output, return the record count
```

Connectors also implement `fetch_databases`, `fetch_schemas` and
`transform_data`, and may override `resolve_database_placeholders(sql, input)`.
The infrastructure tasks (`fetch_incremental_marker`, `read_current_state`,
`prepare_column_extraction_queries`, `execute_single_column_batch`,
`write_current_state`, `update_incremental_marker`) are concrete; override
the `after_current_state_read` hook rather than `read_current_state`.

## Constants

| Constant | Where | Purpose | Value |
|----------|-------|---------|-------|
| `PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE` | `application_sdk.constants` | Connection-scoped persistent prefix | `persistent-artifacts/apps/{application_name}/connection/{connection_id}` |
| `INCREMENTAL_DIFF_SUBPATH_TEMPLATE` | `application_sdk.constants` | Per-run diff, under that prefix | `runs/{run_id}/incremental-diff` |
| `MANIFEST_NAME` | `...incremental.state.store` | Manifest object under `current-state/` | `.sdk-manifest` |
| `MAX_CONCURRENT_COLUMN_BATCHES` | `application_sdk.templates.incremental_sql_metadata_extractor` | Column batch tasks the template's `run()` fans out at a time | `10` |
| `MARKER_TIMESTAMP_FORMAT` | `application_sdk.constants` | Marker timestamp format | `%Y-%m-%dT%H:%M:%SZ` |
| `INCREMENTAL_DEFAULT_STATE` | `application_sdk.constants` | State assumed when a row has none | `NO CHANGE` |
| `DUCKDB_COMMON_TEMP_FOLDER` | `application_sdk.constants` | Temp folder for DuckDB files | `/tmp/incremental_duckdb` |
| `DUCKDB_DEFAULT_MEMORY_LIMIT` | `application_sdk.constants` | DuckDB memory limit | `2GB` |

`application_sdk.constants` also defines a `MAX_CONCURRENT_COLUMN_BATCHES`
(`3`). Nothing reads it — the template's `run()` uses its own module-level
`10` — so do not tune the constant expecting it to change fan-out.
