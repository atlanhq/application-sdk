---
name: implement-incremental-extraction
description: >
  Expert guidance for implementing incremental metadata extraction in a new
  SQL-based connector app using the Application SDK. Covers the full
  implementation: activities class (build_incremental_column_sql, SQL
  placeholders, fetch overrides), workflow class, SQL templates
  (extract_table_incremental.sql, extract_column_incremental.sql), Pydantic
  models, Daft/DuckDB integration, state management, and testing patterns.
  Use when adding incremental extraction to a new database connector or
  understanding the SDK's incremental extraction architecture.
metadata:
  author: platform
  version: "1.0.0"
  category: sdk
  keywords:
    - incremental-extraction
    - application-sdk
    - metadata-extraction
    - temporal-workflows
    - daft
    - duckdb
    - rocksdb
    - sql-connector
    - oracle
    - clickhouse
---

# Implement Incremental Extraction in a New Connector

You are an expert in implementing incremental metadata extraction using the Atlan
Application SDK. You have deep knowledge of the SDK's single inheritance chain
pattern, Temporal workflows, Daft lazy DataFrames, DuckDB file-backed queries,
and RocksDB disk-backed state storage.

## When to Use This Skill

- Adding incremental extraction to a new SQL-based connector app
- Understanding the SDK's incremental extraction architecture
- Debugging incremental extraction issues (state management, column batching, ancestral merge)
- Extending incremental extraction to support new entity types
- Reviewing PRs that modify incremental extraction logic

## When NOT to Use This Skill

- Building a non-SQL connector (REST-based, file-based)
- Working on full extraction only (no incremental support needed)
- Modifying the SDK's core incremental framework (see SDK source directly)

## Architecture Overview

### Single Inheritance Chain

```
BaseSQLMetadataExtractionActivities (SDK)
    └── IncrementalSQLMetadataExtractionActivities (SDK)
            └── YourDatabaseActivities (App)
```

```
BaseSQLMetadataExtractionWorkflow (SDK)
    └── IncrementalSQLMetadataExtractionWorkflow (SDK)
            └── YourDatabaseWorkflow (App)
```

### SDK vs App Responsibilities

| Component | SDK Handles | App Provides |
|-----------|-------------|--------------|
| **Workflow orchestration** | 4-phase execution, parallel batching, retry policies | Nothing (inherited) |
| **Marker management** | S3 fetch/persist, timestamp normalization, prepone logic | Nothing (inherited) |
| **State management** | Current-state read/write, S3 upload/download, ancestral merge | Nothing (inherited) |
| **Table extraction** | Switching between full/incremental SQL, placeholder resolution | `incremental_table_sql` template, `resolve_database_placeholders()` |
| **Column extraction** | Table analysis (Daft), backfill detection (DuckDB), batching, parallel execution | `build_incremental_column_sql()` - the SQL building strategy |
| **SQL execution** | Query execution, result counting, output path management | Nothing (inherited) |

### Workflow 4-Phase Execution

```
Phase 1: Setup
  get_workflow_args → fetch_incremental_marker → read_current_state → save_state

Phase 2: Base Extraction (inherited from BaseSQLMetadataExtractionWorkflow)
  fetch_databases → fetch_schemas → fetch_tables → fetch_columns (skipped if incremental)
  → fetch_procedures → transform_data → upload_to_atlan

Phase 3: Incremental Column Extraction (if prerequisites met)
  prepare_column_extraction_queries → execute_single_column_batch (parallel) → transform_data

Phase 4: Finalization
  write_current_state (ancestral merge + upload) → update_incremental_marker
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
│   ├── activities/
│   │   └── metadata_extraction/
│   │       └── your_db.py          # Activities class (MAIN FILE)
│   ├── workflows/
│   │   └── metadata_extraction/
│   │       └── your_db.py          # Workflow class (minimal)
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

1. **`references/activities-implementation.md`** - Activities class with all overrides
2. **`references/sql-templates.md`** - SQL template patterns for incremental queries
3. **`references/workflow-implementation.md`** - Workflow class setup
4. **`references/testing-patterns.md`** - Unit test patterns
5. **`references/daft-duckdb-patterns.md`** - Daft and DuckDB usage patterns

## Quick Start: Minimal Implementation

```python
# app/activities/metadata_extraction/your_db.py

from application_sdk.activities.metadata_extraction.incremental import (
    IncrementalSQLMetadataExtractionActivities,
)

# Load SQL templates from app/sql/ directory
queries = load_queries("app/sql")

class YourDBActivities(IncrementalSQLMetadataExtractionActivities):
    sql_client_class = YourDBClient

    # Full extraction SQL
    fetch_database_sql = queries.get("EXTRACT_DATABASE")
    fetch_schema_sql = queries.get("EXTRACT_SCHEMA")
    fetch_table_sql = queries.get("EXTRACT_TABLE")
    fetch_column_sql = queries.get("EXTRACT_COLUMN")

    # Incremental SQL (NEW - only this is needed for incremental)
    incremental_table_sql = queries.get("EXTRACT_TABLE_INCREMENTAL")

    def build_incremental_column_sql(self, table_ids, workflow_args):
        """Build SQL for incremental column extraction."""
        # Your database-specific SQL building logic here
        # See references/activities-implementation.md for patterns
        ...

    def resolve_database_placeholders(self, sql, workflow_args):
        """Replace database-specific placeholders (optional override)."""
        # Only needed if your SQL has custom placeholders beyond {marker_timestamp}
        ...
```

```python
# app/workflows/metadata_extraction/your_db.py

from temporalio import workflow
from application_sdk.workflows.metadata_extraction.incremental_sql import (
    IncrementalSQLMetadataExtractionWorkflow,
)

@workflow.defn
class YourDBWorkflow(IncrementalSQLMetadataExtractionWorkflow):
    activities_cls = YourDBActivities
    # That's it! Everything else is inherited.
```

## Key Patterns and Best Practices

### 1. SQL Template Placeholders

The SDK handles `{marker_timestamp}` automatically. Your app only needs to handle
database-specific placeholders via `resolve_database_placeholders()`:

```python
# SDK resolves automatically:
#   {marker_timestamp} → "2024-01-15T00:00:00Z"

# App resolves via resolve_database_placeholders():
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

Temporal reuses activity instances across workflow runs. Never permanently modify
class attributes with resolved SQL:

```python
# BAD - mutates class attribute permanently
self.fetch_table_sql = resolved_sql  # Breaks on next run!

# GOOD - SDK saves originals internally via _original_fetch_table_sql
# The SDK's fetch_tables() and fetch_columns() handle this automatically
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
    "atlan-application-sdk[daft,iam-auth,sqlalchemy,tests,workflows,pandas]==X.Y.Z",
    "rocksdict>=0.3.0",
]
```

The `[daft]` extra is required for incremental extraction (Daft lazy DataFrames).
`rocksdict` is required for disk-backed table state storage.

## Common Pitfalls

1. **Missing `incremental_table_sql`**: If not set, incremental mode falls back to full table extraction
2. **Not escaping quotes in table IDs**: Table names with special characters (e.g., `O'Brien`) must be escaped in SQL
3. **Empty table_ids list**: `build_incremental_column_sql` should raise `ValueError` for empty lists
4. **Forgetting `resolve_database_placeholders`**: If your SQL has custom placeholders, they won't be replaced
5. **Testing with wrong method name**: Tests must call `build_incremental_column_sql` (not a private method name)
6. **Overriding `execute_column_batch`**: Don't override it - it's concrete in the SDK. Only implement `build_incremental_column_sql`
