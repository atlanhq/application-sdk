# App Class Implementation Guide

This reference covers the connector's App class for incremental extraction.
In v3 there is no separate activities class and no workflow class: one class
subclassing `IncrementalSqlMetadataExtractor` holds the `@task` methods and
inherits the orchestrating `run()`. (The file keeps its old name so existing
links still resolve.)

> **Deprecation.** `IncrementalSqlMetadataExtractor` is deprecated and will be
> removed in v4.0.0; the replacement is `application_sdk.templates.SqlApp`
> with a custom `run()`. It is still the SDK's only built-in incremental
> orchestration, and subclassing it emits a `DeprecationWarning`.

> **SQL injection note (BLDX-518).** Any value substituted into a SQL
> template via `str.replace` / f-string must pass through
> `application_sdk.common.sql_filters.validate_filter_no_sql_injection`
> first, and the template must wrap the substitution in single quotes
> (e.g. `WHERE last_modified > '{marker_timestamp}'`). The SDK contracts
> apply this guard automatically for `marker_timestamp`,
> `include_filter`, `exclude_filter`, and `temp_table_regex`; if your
> connector introduces a new substitution placeholder, validate the
> value yourself before the `replace`.

## Class Structure

```python
from application_sdk.app import task
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts.incremental_sql import (
    ExecuteColumnBatchInput,
    FetchColumnsIncrementalInput,
    FetchTablesIncrementalInput,
    IncrementalRunContext,
    marker_to_wire,
)
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesOutput,
    TransformInput,
    TransformOutput,
)


class YourDBExtractor(IncrementalSqlMetadataExtractor):
    """Incremental metadata extraction for YourDB."""

    sql_client_class = YourDBClient

    # Plain class attributes. The SDK does not load app/sql/ for you:
    # read the files at import time (or inline the SQL) and assign them.
    fetch_database_sql = _read_sql("extract_database.sql")
    fetch_schema_sql = _read_sql("extract_schema.sql")
    fetch_table_sql = _read_sql("extract_table.sql")
    fetch_column_sql = _read_sql("extract_column.sql")
    incremental_table_sql = _read_sql("extract_table_incremental.sql")
    incremental_column_sql = _read_sql("extract_column_incremental.sql")
```

## What You Implement

| Member | Kind | Purpose |
|--------|------|---------|
| `fetch_databases` / `fetch_schemas` | `@task` | Full extraction, every run |
| `fetch_tables(input: FetchTablesIncrementalInput)` | `@task` | Switch between full and incremental SQL |
| `fetch_columns(input: FetchColumnsIncrementalInput)` | `@task` | Full-extraction columns; return `FetchColumnsOutput()` when incremental |
| `transform_data(input: TransformInput)` | `@task` | Raw → transformed; handles the batch path via `input.file_names` |
| `build_incremental_column_sql(table_ids, ctx)` | method (abstract) | The column SQL for one batch of table IDs |
| `execute_column_sql(sql, input, ctx)` | async method | Run that SQL, write output, return the record count |
| `resolve_database_placeholders(sql, input)` | method (optional) | Database-specific placeholders such as `{system_schema}` |

## `fetch_tables()` — switching SQL

Incremental mode is on when `input.marker_timestamp` is non-empty **and**
`input.current_state_available` is true. The SDK does not substitute
`{marker_timestamp}` for you here; resolve it into a local variable.

```python
@task(timeout_seconds=1800)
async def fetch_tables(self, input: FetchTablesIncrementalInput) -> FetchTablesOutput:
    is_incremental = bool(input.marker_timestamp) and input.current_state_available
    if is_incremental and self.incremental_table_sql:
        sql = self.incremental_table_sql.replace(
            "{marker_timestamp}", input.marker_timestamp
        )
        sql = self.resolve_database_placeholders(sql, input)
    else:
        sql = self.fetch_table_sql
    ...
```

Never assign the resolved SQL back to `self.incremental_table_sql` or
`self.fetch_table_sql`: workers reuse App instances across runs, so a mutated
class attribute leaks into the next run.

## Required Method: `build_incremental_column_sql()`

The SDK calls this once per batch with the batch's table IDs and the run's
`IncrementalRunContext` (marker, connection, chunk size, …) and expects a
fully rendered SQL string back.

### Oracle Pattern (FROM dual CTE)

Oracle has a 1000-element IN clause limit, so use a CTE with UNION ALL:

```python
def build_incremental_column_sql(
    self, table_ids: list[str], ctx: IncrementalRunContext
) -> str:
    """Build column SQL using Oracle FROM dual CTE syntax."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    first_id = table_ids[0].replace("'", "''")
    cte_lines = [f"SELECT '{first_id}' AS TABLE_ID FROM dual"]
    for tid in table_ids[1:]:
        safe_tid = tid.replace("'", "''")
        cte_lines.append(f"SELECT '{safe_tid}' FROM dual")
    cte_sql = "WITH table_filter AS (\n" + "\nUNION ALL ".join(cte_lines) + "\n)"

    sql = self.incremental_column_sql.replace("--TABLE_FILTER_CTE--", cte_sql)
    sql = sql.replace("{system_schema}", _sql_identifier(self.system_schema))
    sql = sql.replace("{marker_timestamp}", marker_to_wire(ctx.marker_timestamp))
    return sql
```

`ctx` carries no connector-specific settings; keep values such as the system
schema on the App (as `self.system_schema` above) or derive them from the
connection.

**Validate every identifier before it is substituted.** A schema name is
spliced into the SQL text, so a value derived from the connection is
untrusted input: `SYS.DBA_TABLES t WHERE 1=1 --` would comment out the rest
of the query. Accept only a plain identifier, and fail otherwise:

```python
import re

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")


def _sql_identifier(value: str) -> str:
    """Return *value* if it is a plain, unquoted SQL identifier; else raise."""
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"not a plain SQL identifier: {value!r}")
    return value
```

### ClickHouse Pattern (WHERE IN clause)

```python
def build_incremental_column_sql(
    self, table_ids: list[str], ctx: IncrementalRunContext
) -> str:
    """Build column SQL using a WHERE IN clause."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    safe_ids = [f"'{tid.replace(chr(39), chr(39) * 2)}'" for tid in table_ids]
    return self.incremental_column_sql.replace(
        "{table_ids_in_clause}", ", ".join(safe_ids)
    )
```

### PostgreSQL Pattern (ANY(ARRAY[...]))

```python
def build_incremental_column_sql(
    self, table_ids: list[str], ctx: IncrementalRunContext
) -> str:
    """Build column SQL using PostgreSQL ARRAY syntax."""
    if not table_ids:
        raise ValueError("No table IDs provided for column extraction")

    safe_ids = [f"'{tid.replace(chr(39), chr(39) * 2)}'" for tid in table_ids]
    return self.incremental_column_sql.replace(
        "{table_ids_array}", "ARRAY[" + ", ".join(safe_ids) + "]"
    )
```

## Required Method: `execute_column_sql()`

`execute_single_column_batch` downloads the batch file, calls
`build_incremental_column_sql`, then hands the SQL to `execute_column_sql`.
The default raises; implement it to run the query and write the batch's raw
output under `input.output_path`:

```python
async def execute_column_sql(
    self, sql: str, input: ExecuteColumnBatchInput, ctx: IncrementalRunContext
) -> int:
    client = await self._load_sql_client(input)
    ...  # execute, write raw column output, return the number of records
```

## Optional Override: `resolve_database_placeholders()`

```python
def resolve_database_placeholders(
    self, sql: str, input: FetchTablesIncrementalInput
) -> str:
    """Replace Oracle-specific placeholders."""
    return sql.replace("{system_schema}", _sql_identifier(self.system_schema))
```

The default is a no-op. Your own `fetch_tables` calls it (see above); the SDK
does not call it for you. `_sql_identifier` is the validator shown under
`build_incremental_column_sql()` above.

## Optional Hook: `after_current_state_read()`

`read_current_state` only probes the committed snapshot. If your connector
needs the snapshot on disk at read time, override the hook — overriding it is
what makes the read materialize:

```python
async def after_current_state_read(
    self, snapshot: CurrentStateSnapshot, local_dir: Path
) -> None:
    ...  # local_dir is {output_path}/incremental/previous-state
```

## Do NOT Override

These tasks are concrete in the SDK:

| Method | Why Not Override |
|--------|-----------------|
| `fetch_incremental_marker()` | Marker read and prepone |
| `read_current_state()` | `CurrentStateStore.probe()`; overriding it is deprecated — use `after_current_state_read` |
| `prepare_column_extraction_queries()` | Materialize previous state, DuckDB change + backfill analysis, batch files |
| `execute_single_column_batch()` | Batch download, then `build_incremental_column_sql` + `execute_column_sql` |
| `write_current_state()` | Build, diff, upload diff, `CurrentStateStore.commit()` |
| `update_incremental_marker()` | Marker persistence, only after a successful commit |
| `run()` | The five-phase orchestration (see `workflow-implementation.md`) |

## ClickHouse-Specific: Filter Transformation

ClickHouse maps databases to schemas under a virtual "default" catalog,
requiring filter transformation before the filters reach SQL:

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
