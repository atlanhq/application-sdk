# SQL Template Patterns for Incremental Extraction

This reference covers the SQL templates needed for incremental extraction.

## Required SQL Files

| File | Purpose | Key Requirement |
|------|---------|-----------------|
| `extract_table.sql` | Full table extraction | Standard - already exists |
| `extract_table_incremental.sql` | Incremental table extraction | Must include `incremental_state` column |
| `extract_column.sql` | Full column extraction | Standard - already exists |
| `extract_column_incremental.sql` | Incremental column extraction | Must include table_id placeholder |

## extract_table_incremental.sql

This SQL must label each table with its incremental state based on comparison
with the marker timestamp.

### Required Output Columns

Your incremental table SQL must produce these columns (in addition to all standard
table metadata columns from `extract_table.sql`):

| Column | Type | Values | Purpose |
|--------|------|--------|---------|
| `incremental_state` | VARCHAR | `CREATED`, `UPDATED`, `NO CHANGE` | Table's change state |

### Oracle Example

```sql
SELECT
    t.*,
    CASE
        WHEN t.CREATED > TO_TIMESTAMP('{marker_timestamp}', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            THEN 'CREATED'
        WHEN t.LAST_DDL_TIME > TO_TIMESTAMP('{marker_timestamp}', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            THEN 'UPDATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM {system_schema}.DBA_TABLES t
WHERE ...
```

Key points:
- Oracle uses `CREATED` and `LAST_DDL_TIME` columns for change detection
- Uses `TO_TIMESTAMP()` for proper timestamp comparison
- `{marker_timestamp}` is resolved by SDK's `_resolve_common_placeholders()`
- `{system_schema}` is resolved by app's `resolve_database_placeholders()`

### ClickHouse Example

```sql
SELECT
    d.name AS database_name,
    t.name AS table_name,
    t.metadata_modification_time,
    CASE
        WHEN t.metadata_modification_time > parseDateTimeBestEffort('{marker_timestamp}')
            THEN 'CREATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM system.tables t
JOIN system.databases d ON t.database = d.name
WHERE ...
```

Key points:
- ClickHouse uses `metadata_modification_time` from `system.tables`
- Uses `parseDateTimeBestEffort()` for flexible timestamp parsing
- ClickHouse doesn't distinguish CREATED vs UPDATED at database level
  (the SDK's backfill detection handles UPDATED via DuckDB comparison)

### General Template

```sql
SELECT
    <all standard columns>,
    CASE
        WHEN <created_column> > <parse_timestamp>('{marker_timestamp}')
            THEN 'CREATED'
        WHEN <modified_column> > <parse_timestamp>('{marker_timestamp}')
            THEN 'UPDATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM <tables_system_view>
WHERE <standard_filters>
```

## extract_column_incremental.sql

This SQL extracts columns for a specific set of tables identified by their
table IDs. The table IDs are injected by `build_incremental_column_sql()`.

### Oracle Pattern: CTE with FROM dual

Oracle uses a CTE placeholder `--TABLE_FILTER_CTE--` that gets replaced with
a `WITH table_filter AS (...)` clause:

```sql
--TABLE_FILTER_CTE--
SELECT
    c.OWNER || '.' || c.TABLE_NAME AS table_qualified_name,
    c.COLUMN_NAME,
    c.DATA_TYPE,
    c.DATA_LENGTH,
    c.NULLABLE,
    c.COLUMN_ID,
    CASE
        WHEN c.LAST_DDL_TIME > TO_TIMESTAMP('{marker_timestamp}', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            THEN 'CREATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM {system_schema}.DBA_TAB_COLUMNS c
JOIN table_filter tf ON tf.TABLE_ID = c.OWNER || '.' || c.TABLE_NAME
WHERE ...
ORDER BY c.OWNER, c.TABLE_NAME, c.COLUMN_ID
```

After replacement by `build_incremental_column_sql()`:

```sql
WITH table_filter AS (
    SELECT 'MYDB.SCHEMA1.TABLE1' AS TABLE_ID FROM dual
    UNION ALL SELECT 'MYDB.SCHEMA1.TABLE2' FROM dual
    UNION ALL SELECT 'MYDB.SCHEMA2.TABLE3' FROM dual
)
SELECT ...
FROM {system_schema}.DBA_TAB_COLUMNS c
JOIN table_filter tf ON tf.TABLE_ID = c.OWNER || '.' || c.TABLE_NAME
...
```

### ClickHouse Pattern: WHERE IN

ClickHouse uses a simple `{table_ids_in_clause}` placeholder:

```sql
SELECT
    concat(d.name, '.', t.name) AS table_qualified_name,
    c.name AS column_name,
    c.type AS data_type,
    c.is_in_primary_key,
    c.is_in_sorting_key,
    CASE
        WHEN t.metadata_modification_time > parseDateTimeBestEffort('{marker_timestamp}')
            THEN 'CREATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM system.columns c
JOIN system.tables t ON c.database = t.database AND c.table = t.name
JOIN system.databases d ON t.database = d.name
WHERE concat('default', '.', d.name, '.', t.name) IN ({table_ids_in_clause})
```

After replacement by `build_incremental_column_sql()`:

```sql
...
WHERE concat('default', '.', d.name, '.', t.name) IN ('default.db1.table1', 'default.db1.table2')
```

### PostgreSQL Pattern: ANY(ARRAY[...])

```sql
SELECT
    n.nspname || '.' || c.relname AS table_qualified_name,
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull AS is_nullable,
    CASE
        WHEN c.reltuples > 0 THEN 'CREATED'
        ELSE 'NO CHANGE'
    END AS incremental_state
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
JOIN pg_catalog.pg_namespace n ON c.relnamespace = n.oid
WHERE (current_database() || '.' || n.nspname || '.' || c.relname) = ANY({table_ids_array})
  AND a.attnum > 0
  AND NOT a.attisdropped
```

## Placeholder Resolution Order

The SDK resolves placeholders in this order:

1. **`_resolve_common_placeholders()`** (SDK automatic): `{marker_timestamp}`
2. **`resolve_database_placeholders()`** (app override): `{system_schema}`, etc.
3. **`build_incremental_column_sql()`** (app method): `--TABLE_FILTER_CTE--`, `{table_ids_in_clause}`, etc.

Note: `_resolve_common_placeholders()` is called automatically by the SDK inside
`run_column_query()`, so your `build_incremental_column_sql()` does NOT need to
replace `{marker_timestamp}` - but it can if needed (the replacement is idempotent).

## Table ID Format

Table IDs follow the format: `{catalog}.{schema}.{table_name}`

Examples:
- Oracle: `MYDB.SCHEMA1.EMPLOYEES`
- ClickHouse: `default.my_database.my_table`
- PostgreSQL: `my_database.public.users`

The format must match how your SQL joins/filters reference tables.

## SQL Escaping Best Practices

Always escape single quotes in table IDs to prevent SQL injection:

```python
# Escape single quotes by doubling them (SQL standard)
safe_id = table_id.replace("'", "''")
```

This handles table names with special characters like `O'Brien` or `TEST'TABLE`.
