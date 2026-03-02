# Activities Implementation Guide

This reference covers the detailed implementation of the Activities class for incremental extraction.

## Class Structure

```python
from typing import Any, Dict, List, Optional

from temporalio import activity

from application_sdk.activities.metadata_extraction.incremental import (
    IncrementalSQLMetadataExtractionActivities,
)
from application_sdk.common.incremental.models import IncrementalWorkflowArgs

class YourDBActivities(IncrementalSQLMetadataExtractionActivities):
    """Activities for YourDB incremental metadata extraction."""

    sql_client_class = YourDBClient

    # All SQL queries are auto-loaded from app/sql/ by the SDK:
    #   fetch_database_sql          ← extract_database.sql
    #   fetch_schema_sql            ← extract_schema.sql
    #   fetch_table_sql             ← extract_table.sql
    #   fetch_column_sql            ← extract_column.sql
    #   incremental_table_sql       ← extract_table_incremental.sql
    #   incremental_column_sql      ← extract_column_incremental.sql
    #
    # No need to set these manually — just place the SQL files in app/sql/.
```

## Required Method: `build_incremental_column_sql()`

This is the **only abstract method** you must implement. The SDK calls this method
with a list of table_ids and expects back a fully rendered SQL query string.

### Oracle Pattern (FROM dual CTE)

Oracle has a 1000-element IN clause limit, so we use a CTE with UNION ALL:

```python
def build_incremental_column_sql(
    self,
    table_ids: List[str],
    workflow_args: Dict[str, Any],
) -> str:
    """Build column SQL using Oracle FROM dual CTE syntax."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    args = IncrementalWorkflowArgs.model_validate(workflow_args)
    system_schema = args.metadata.system_schema_name or "SYS"

    # Build CTE: WITH table_filter AS (SELECT 'ID1' AS TABLE_ID FROM dual UNION ALL ...)
    first_id = table_ids[0].replace("'", "''")
    cte_lines = [f"SELECT '{first_id}' AS TABLE_ID FROM dual"]
    for tid in table_ids[1:]:
        safe_tid = tid.replace("'", "''")
        cte_lines.append(f"SELECT '{safe_tid}' FROM dual")

    cte_sql = "WITH table_filter AS (\n" + "\nUNION ALL ".join(cte_lines) + "\n)"

    # Replace --TABLE_FILTER_CTE-- placeholder in template
    sql = self.incremental_column_sql
    sql = sql.replace("--TABLE_FILTER_CTE--", cte_sql)

    # Replace database-specific placeholders
    sql = sql.replace("{system_schema}", system_schema)
    sql = sql.replace(":schema_name", f"'{system_schema}'")
    sql = sql.replace(":marker_timestamp", f"'{args.metadata.marker_timestamp}'")

    return sql
```

### ClickHouse Pattern (WHERE IN clause)

ClickHouse has no IN clause limit, so we use a simple WHERE IN:

```python
def build_incremental_column_sql(
    self,
    table_ids: List[str],
    workflow_args: Dict[str, Any],
) -> str:
    """Build column SQL using WHERE IN clause."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    # Build IN clause: ('id1', 'id2', 'id3')
    safe_ids = [f"'{tid.replace(chr(39), chr(39)*2)}'" for tid in table_ids]
    in_clause = ", ".join(safe_ids)

    sql = self.incremental_column_sql
    sql = sql.replace("{table_ids_in_clause}", in_clause)

    return sql
```

### PostgreSQL Pattern (ANY(ARRAY[...]))

```python
def build_incremental_column_sql(
    self,
    table_ids: List[str],
    workflow_args: Dict[str, Any],
) -> str:
    """Build column SQL using PostgreSQL ARRAY syntax."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    safe_ids = [f"'{tid.replace(chr(39), chr(39)*2)}'" for tid in table_ids]
    array_literal = "ARRAY[" + ", ".join(safe_ids) + "]"

    sql = self.incremental_column_sql
    sql = sql.replace("{table_ids_array}", array_literal)

    return sql
```

## Optional Override: `resolve_database_placeholders()`

Override this only if your SQL templates have database-specific placeholders
beyond `{marker_timestamp}` (which the SDK handles automatically).

```python
def resolve_database_placeholders(
    self, sql: str, workflow_args: Dict[str, Any]
) -> str:
    """Replace Oracle-specific placeholders."""
    args = IncrementalWorkflowArgs.model_validate(workflow_args)
    metadata = args.metadata

    # Replace system schema placeholder
    system_schema = getattr(metadata, "system_schema_name", None) or "SYS"
    sql = sql.replace("{system_schema}", system_schema)

    return sql
```

**Important**: Do NOT replace `{marker_timestamp}` here - the SDK does it automatically
via `_resolve_common_placeholders()`.

## Optional Override: `fetch_databases()` / `fetch_schemas()`

Override these only if your database needs special handling:

```python
# Oracle example: fetch_databases needs write_to_file=False, concatenate=True
# because Oracle treats databases as catalogs with multidb mode

@activity.defn
@auto_heartbeater
async def fetch_databases(self, workflow_args):
    """Override to use multidb mode for Oracle's database-as-catalog pattern."""
    workflow_args_copy = dict(workflow_args)
    workflow_args_copy["write_to_file"] = False
    workflow_args_copy["concatenate"] = True
    return await super().fetch_databases(workflow_args_copy)


# Oracle example: fetch_schemas needs {system_schema} resolution
@activity.defn
@auto_heartbeater
async def fetch_schemas(self, workflow_args):
    """Override to resolve {system_schema} placeholder in schema SQL."""
    args = IncrementalWorkflowArgs.model_validate(workflow_args)
    system_schema = getattr(args.metadata, "system_schema_name", None) or "SYS"

    original_sql = self.fetch_schema_sql
    self.fetch_schema_sql = original_sql.replace("{system_schema}", system_schema)

    try:
        return await super().fetch_schemas(workflow_args)
    finally:
        self.fetch_schema_sql = original_sql
```

## Do NOT Override

The following methods are **concrete in the SDK** and should NOT be overridden:

| Method | Why Not Override |
|--------|-----------------|
| `execute_column_batch()` | Concrete - calls `build_incremental_column_sql()` + `run_column_query()` |
| `run_column_query()` | Handles SQL execution, chunking, output paths |
| `fetch_tables()` | Handles incremental/full SQL switching + state mutation prevention |
| `fetch_columns()` | Handles incremental skip logic + state mutation prevention |
| `fetch_incremental_marker()` | Generic marker S3 management |
| `update_incremental_marker()` | Generic marker S3 persistence |
| `read_current_state()` | Generic current-state S3 download |
| `write_current_state()` | Generic ancestral merge + S3 upload |
| `prepare_column_extraction_queries()` | Generic Daft analysis + batching |
| `execute_single_column_batch()` | Generic batch download + delegation |

## ClickHouse-Specific: Filter Transformation

ClickHouse maps databases to schemas under a virtual "default" catalog, requiring
filter transformation:

```python
@staticmethod
def _transform_to_schema_only_filters(filters):
    """Transform {catalog}.{schema} filters to schema-only for ClickHouse."""
    if not filters:
        return filters
    transformed = {}
    for key, value in filters.items():
        parts = key.split(".")
        if len(parts) == 2:
            transformed[parts[1]] = value  # Drop catalog prefix
        else:
            transformed[key] = value
    return transformed
```

## Workflow Registration

The workflow needs to register all incremental activities:

```python
@staticmethod
def get_activities(activities):
    return [
        activities.preflight_check,
        activities.get_workflow_args,
        activities.fetch_incremental_marker,
        activities.read_current_state,
        activities.fetch_databases,
        activities.fetch_schemas,
        activities.fetch_tables,
        activities.fetch_columns,
        activities.fetch_procedures,
        activities.transform_data,
        activities.prepare_column_extraction_queries,
        activities.execute_single_column_batch,
        activities.write_current_state,
        activities.upload_to_atlan,
        activities.update_incremental_marker,
        activities.save_workflow_state,
    ]
```

If your database doesn't support certain entities, exclude them:

```python
# ClickHouse: no stored procedures
@staticmethod
def get_activities(activities):
    base = IncrementalSQLMetadataExtractionWorkflow.get_activities(activities)
    return [a for a in base if a != activities.fetch_procedures]
```
