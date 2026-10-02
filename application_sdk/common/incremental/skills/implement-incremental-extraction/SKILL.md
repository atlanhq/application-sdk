---
name: implement-incremental-extraction
description: >
  Expert guidance for implementing incremental metadata extraction in a new
  SQL-based connector app using the Application SDK. Covers the full
  implementation: the IncrementalSqlMetadataExtractor App class
  (build_incremental_column_sql, execute_column_sql, SQL placeholders, fetch
  overrides), the inherited run() orchestration, SQL templates
  (extract_table_incremental.sql, extract_column_incremental.sql), Pydantic
  models, DuckDB integration, state management, and testing patterns.
  Use when adding incremental extraction to a new database connector or
  understanding the SDK's incremental extraction architecture.
metadata:
  author: platform
  version: "2.0.0"
  category: sdk
  keywords:
    - incremental-extraction
    - application-sdk
    - metadata-extraction
    - temporal-workflows
    - duckdb
    - rocksdb
    - sql-connector
    - oracle
    - clickhouse
---

# Implement Incremental Extraction in a New Connector

You are an expert in implementing incremental metadata extraction using the Atlan
Application SDK. You have deep knowledge of the SDK's v3 App/task model,
the `CurrentStateStore` snapshot commit, DuckDB file-backed queries, and
RocksDB disk-backed state storage.

## When to Use This Skill

- Adding incremental extraction to a new SQL-based connector app
- Understanding the SDK's incremental extraction architecture
- Debugging incremental extraction issues (current-state commit, column batching, deletion detection)
- Extending incremental extraction to support new entity types
- Reviewing PRs that modify incremental extraction logic

## When NOT to Use This Skill

- Building a non-SQL connector (REST-based, file-based)
- Working on full extraction only (no incremental support needed)
- Modifying the SDK's core incremental framework (see SDK source directly)

## Architecture Overview

### Class Chain

```
SqlMetadataExtractor (SDK)
    └── IncrementalSqlMetadataExtractor (SDK, deprecated — removed in v4.0.0)
            └── YourDBExtractor (App)
```

One class holds every `@task` and inherits the orchestrating `run()`; v3 has
no separate activities or workflow class. `IncrementalSqlMetadataExtractor`
is deprecated in favour of `application_sdk.templates.SqlApp` with a custom
`run()`, but it is still the SDK's only built-in incremental orchestration.

### SDK vs App Responsibilities

| Component | SDK Handles | App Provides |
|-----------|-------------|--------------|
| **Orchestration** | 5-phase `run()`, parallel batching, retries | Nothing (inherited) |
| **Marker management** | S3 fetch/persist, timestamp normalization, prepone logic | Nothing (inherited) |
| **State management** | `CurrentStateStore` probe/materialize/commit, run-scoped directories, diff + deletion detection | Nothing (inherited) |
| **Table extraction** | Passes `marker_timestamp` and `current_state_available` to `fetch_tables` | `fetch_tables()` choosing full vs `incremental_table_sql`, `resolve_database_placeholders()` (optional) |
| **Column extraction** | Table analysis (DuckDB), backfill detection (DuckDB), batching, parallel execution | `build_incremental_column_sql()` - the SQL building strategy |
| **SQL execution** | Batch download, output path management | `execute_column_sql()` - run the batch SQL, return the record count |

### 5-Phase `run()`

```
Phase 1: Prerequisites
  fetch_incremental_marker → read_current_state (CurrentStateStore.probe; no download)

Phase 2: Base Extraction
  fetch_databases + fetch_schemas (parallel) → fetch_tables → fetch_columns (skipped if incremental)

Phase 3: Incremental Column Extraction (if prerequisites met)
  prepare_column_extraction_queries → execute_single_column_batch (10 at a time)

Phase 4: Write State
  write_current_state (build → diff → upload diff → CurrentStateStore.commit)

Phase 5: Update Marker
  update_incremental_marker (only after the commit)
```

### Incremental Prerequisites (all must be true)

1. `incremental-extraction` parameter is `"true"`
2. `marker_timestamp` exists (fetched from S3 marker.txt from a previous run)
3. `current_state_available` is true (previous state snapshot exists in S3)

If any prerequisite is not met, the workflow runs a full extraction instead.

## Implementation Checklist

### Files You Need to Create/Modify

```
your-database-app/
├── app/
│   ├── your_db.py                  # App class (MAIN FILE)
│   └── sql/
│       ├── extract_table.sql              # Full table extraction
│       ├── extract_table_incremental.sql  # Incremental table extraction (NEW)
│       ├── extract_column.sql             # Full column extraction
│       └── extract_column_incremental.sql # Incremental column extraction (NEW)
├── tests/
│   └── unit/
│       └── test_column_utils.py    # Tests for build_incremental_column_sql
└── pyproject.toml                  # SDK dependency with [incremental] extra
```

### Step-by-Step Implementation

See the reference files for detailed implementation of each step:

1. **`references/activities-implementation.md`** - App class with all overrides
2. **`references/sql-templates.md`** - SQL template patterns for incremental queries
3. **`references/workflow-implementation.md`** - The inherited `run()` orchestration
4. **`references/testing-patterns.md`** - Unit test patterns
5. **`references/duckdb-patterns.md`** - DuckDB usage patterns

## Quick Start: Minimal Implementation

```python
# connector/app.py

from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.app import task
from application_sdk.templates.contracts.incremental_sql import (
    FetchColumnsIncrementalInput,
    FetchTablesIncrementalInput,
    IncrementalRunContext,
)
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsOutput,
    FetchDatabasesInput, FetchDatabasesOutput,
    FetchSchemasInput, FetchSchemasOutput,
    FetchTablesOutput,
    TransformInput, TransformOutput,
)

class YourDBExtractor(IncrementalSqlMetadataExtractor):
    sql_client_class = YourDBClient

    # Plain class attributes — the SDK does not load app/sql/ for you.
    fetch_database_sql = _read_sql("extract_database.sql")
    fetch_schema_sql = _read_sql("extract_schema.sql")
    fetch_table_sql = _read_sql("extract_table.sql")
    fetch_column_sql = _read_sql("extract_column.sql")
    incremental_table_sql = _read_sql("extract_table_incremental.sql")
    incremental_column_sql = _read_sql("extract_column_incremental.sql")

    @task(timeout_seconds=3600)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        ...

    @task(timeout_seconds=3600)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        ...

    @task(timeout_seconds=3600)
    async def fetch_tables(self, input: FetchTablesIncrementalInput) -> FetchTablesOutput:
        ...  # full vs incremental SQL

    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: FetchColumnsIncrementalInput) -> FetchColumnsOutput:
        ...  # FetchColumnsOutput() when incremental; full SQL otherwise

    @task(timeout_seconds=3600)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        ...

    def build_incremental_column_sql(
        self, table_ids: list[str], ctx: IncrementalRunContext
    ) -> str:
        """Build SQL for incremental column extraction."""
        # Your database-specific SQL building logic here
        ...

    async def execute_column_sql(self, sql, input, ctx) -> int:
        """Run one batch's column SQL; return the record count."""
        ...
```

## Key Patterns and Best Practices

### 1. SQL Template Placeholders

The SDK substitutes nothing into your templates. It hands you a validated
`marker_timestamp` (on the task input, and on `IncrementalRunContext`), and
you substitute it:

```python
# Your fetch_tables() / build_incremental_column_sql():
#   {marker_timestamp} → input.marker_timestamp / ctx.marker_timestamp

# Your resolve_database_placeholders(sql, input):
#   {system_schema} → "SYS" (Oracle)
#   Any other database-specific placeholders
```

### 2. Column Extraction SQL Strategies

Each database has a different way to pass table IDs to the column query:

| Database | Strategy | Reason |
|----------|----------|--------|
| Oracle | `FROM dual` CTE with `UNION ALL` | 1000-element IN clause limit |
| ClickHouse | `WHERE ... IN (...)` clause | No element limit |
| PostgreSQL | `ANY(ARRAY[...])` | PostgreSQL array syntax |

### 3. State Mutation Prevention

Workers reuse App instances across runs. Never assign resolved SQL back to a
class attribute:

```python
# BAD - mutates the attribute for every later run
self.fetch_table_sql = resolved_sql  # Breaks on next run!

# GOOD - resolve into a local variable
sql = self.incremental_table_sql.replace("{marker_timestamp}", input.marker_timestamp)
```

### 4. Incremental Table SQL Labeling

Your `extract_table_incremental.sql` must include an `incremental_state` column:

```sql
SELECT ...,
  CASE
    WHEN created_time > '{marker_timestamp}' THEN 'CREATED'
    WHEN modified_time > '{marker_timestamp}' THEN 'UPDATED'
    ELSE 'NO CHANGE'
  END AS incremental_state
FROM ...
```

### 5. Incremental Column SQL Template

Your `extract_column_incremental.sql` must include a placeholder for table IDs
that your `build_incremental_column_sql()` method will replace:

```sql
-- Oracle pattern: --TABLE_FILTER_CTE-- placeholder
--TABLE_FILTER_CTE--
SELECT ... FROM ... JOIN table_filter ...

-- ClickHouse pattern: {table_ids_in_clause} placeholder
SELECT ... FROM ... WHERE ... IN ({table_ids_in_clause})
```

### 6. pyproject.toml Configuration

```toml
[project]
dependencies = [
    "atlan-application-sdk[incremental]==X.Y.Z",
]
```

The `[incremental]` extra brings in DuckDB, pyarrow, pandas, sqlalchemy, and
(via `[storage]`) `rocksdict` for disk-backed table state storage — no
separate `rocksdict` pin is needed.

## Common Pitfalls

1. **Missing `incremental_table_sql`**: Your `fetch_tables()` should fall back to `fetch_table_sql` when it is `None`
2. **Not escaping quotes in table IDs**: Table names with special characters (e.g., `O'Brien`) must be escaped in SQL
3. **Empty table_ids list**: `build_incremental_column_sql` should raise `ValueError` for empty lists
4. **Forgetting `resolve_database_placeholders`**: If your SQL has custom placeholders, they won't be replaced
5. **Testing with wrong method name**: Tests must call `build_incremental_column_sql` (not a private method name)
6. **Overriding `execute_single_column_batch`**: Don't override it - it's concrete in the SDK. Implement `build_incremental_column_sql` and `execute_column_sql`
7. **Overriding `read_current_state`**: Deprecated. Override the `after_current_state_read(snapshot, local_dir)` hook instead
