# Tools

Debugging and exploration utilities for working with Iceberg workflow logs.

## DuckDB CLI (`duckdb_cli.py`)

Interactive SQL interface for querying Iceberg tables via DuckDB.

```bash
python3 duckdb_cli.py
```

### Commands

| Command | Description |
|---------|-------------|
| `tables` | List available tables |
| `schema <table>` | Show table schema |
| `samples` | Show sample queries |
| `1-10` | Run predefined sample query |
| `<SQL>` | Run custom SQL query |
| `quit` | Exit |

### Sample Queries

```
1. First 20 rows
2. Count by log level
3. Recent logs
4. Error logs
5. Logs with workflow ID
...
```

## DuckDB Web UI (`duckdb_ui.py`)

Streamlit-based web interface for exploring logs.

```bash
pip install streamlit
streamlit run duckdb_ui.py
# Opens at http://localhost:8501
```

### Features

- **P0 Tab:** Query by workflow name with predicate pushdown
- **SQL Tab:** Run custom SQL on any table
- **Export:** Download results as CSV

### Screenshot

```
┌─────────────────────────────────────────────────────┐
│ Workflow Log Explorer                               │
├─────────────────────────────────────────────────────┤
│ Table: workflow_logs_wf_month                       │
│ Workflow: [dropdown]                                │
│ Node Filter: [optional]                             │
│ [Fetch Logs]                                        │
├─────────────────────────────────────────────────────┤
│ Rows Returned: 1,000  | Query Time: 1.2s            │
│ Files Scanned: 9                                    │
└─────────────────────────────────────────────────────┘
```

## Quick Verify (`quick_verify.py`)

Fast verification that data was ingested correctly.

```bash
python3 quick_verify.py
```

**Output:**
- Lists tables in namespace
- Shows row counts
- Previews first 5 rows
- Summarizes non-null column values
- Shows checkpointed files

## Inspect Parquet (`inspect_parquet.py`)

Examines Parquet file schema from S3.

```bash
python3 inspect_parquet.py
```

**Output:**
- File schema
- Column types and nullability
- Data presence check
- Searches for 'atlan' references in field names/values

Useful for debugging schema mismatches before ingestion.

## Read with DuckDB (`read_with_duckdb.py`)

Alternative way to read Iceberg tables using DuckDB's native Iceberg support.

```bash
python3 read_with_duckdb.py
```

**Note:** Requires DuckDB Iceberg extension. Uses PyIceberg to get metadata location, then DuckDB for querying.

## Common Workflows

### Debug a workflow's logs

```bash
# 1. Start the UI
streamlit run duckdb_ui.py

# 2. Select workflow from dropdown
# 3. Click "Fetch Logs"
# 4. Use SQL tab for custom analysis
```

### Verify ingestion worked

```bash
python3 quick_verify.py
# Check: row counts, non-null values, latest checkpoints
```

### Inspect source data schema

```bash
python3 inspect_parquet.py
# Check: field names match Iceberg schema
```

### Ad-hoc SQL analysis

```bash
python3 duckdb_cli.py
> SELECT level, COUNT(*) FROM workflow_logs GROUP BY level
> SELECT * FROM workflow_logs WHERE level = 'ERROR' LIMIT 10
```
