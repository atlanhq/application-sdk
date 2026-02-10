# Ingestion Pipeline

Scripts for loading workflow logs from S3 Parquet files into Iceberg tables on Polaris.

> **Note:** This is POC code. In production, MDLH will provide native ingestion capabilities.

## Overview

```
S3 Parquet Files                    Iceberg Table (Polaris)
─────────────────                   ───────────────────────
year=2025/                          workflow_logs_wf_month
  month=12/                         ├── Partitioned by:
    day=29/                         │   ├── atlan_argo_workflow_name
      chunk-0-part*.parquet  ──────▶│   └── month
      chunk-1-part*.parquet         ├── Sorted by: timestamp DESC
      ...                           └── Checkpointed: tracks processed files
```

## Scripts

### `s3_to_iceberg.py` (Main Ingestion)

Parallel S3 → Iceberg ingestion with:
- **Checkpointing:** Tracks processed files to enable resumable ingestion
- **Parallel download:** ThreadPoolExecutor for concurrent S3 reads
- **Batch commits:** Reduces Iceberg commit conflicts
- **Schema normalization:** Extracts fields from nested structs

```bash
# Run ingestion for a specific date
python3 s3_to_iceberg.py
# Edit the script to change: ingest_partition(2025, 12, 29)
```

### `schema.py` (Iceberg Configurations)

Defines 3 partition strategies that were benchmarked:

| Config | Partition Scheme | Result |
|--------|-----------------|--------|
| `wf_month_partition` | identity(workflow_name) + identity(month) | **Winner** |
| `wf_bucket` | bucket(workflow_name, 16) | More files scanned |
| `wf_bucket_month` | bucket(workflow_name, 16) + identity(month) | Slight overhead |

### `ingest_multiple_configs.py`

Loads the same Parquet files into all 3 partition configurations for benchmarking.

```bash
python3 ingest_multiple_configs.py --parquet-dir ./data/parquet_files
```

### `clickhouse_migration.py`

Converts ClickHouse CSV exports to Parquet format matching the Iceberg schema.

```bash
python3 clickhouse_migration.py --input logs.csv --output-dir ./parquet_files
```

## Schema

```python
# Top-level fields (for predicate pushdown)
timestamp: float64           # Unix timestamp
level: string               # INFO, WARNING, ERROR
message: string             # Log message
logger_name: string         # Logger name
atlan_argo_workflow_name: string  # Workflow name (PARTITION KEY)
atlan_argo_workflow_node: string  # Workflow node
trace_id: string            # Correlation ID
month: int64                # Month number (PARTITION KEY)

# Nested struct (for additional metadata)
extra: struct<
  activity_id, activity_type, request_id, ...
>
```

## Key Design Decisions

### 1. Why extract fields to top-level?

Iceberg predicate pushdown only works on top-level columns, not nested struct fields. By extracting `atlan_argo_workflow_name` to the top level, we enable partition pruning.

**Before:** Scan all files, filter in memory  
**After:** Skip 99% of files via partition metadata

### 2. Why workflow_name + month partition?

- **workflow_name:** Primary query predicate (P0 queries filter by workflow)
- **month:** Prevents partition explosion for long-running workflows
- **No bucket:** Identity partition provides exact match pruning

### 3. Why checkpointing?

S3 has thousands of Parquet files. Checkpointing allows:
- Resumable ingestion after failures
- Incremental updates (only process new files)
- Idempotent runs (safe to re-run)

## Configuration

See `../config.py` for:
- `POLARIS_CATALOG_URI` - Catalog endpoint
- `S3_BUCKET` / `S3_PREFIX` - Source data location
- `NAMESPACE` / `TABLE_NAME` - Target table
