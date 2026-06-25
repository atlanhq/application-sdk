# DuckDB and RocksDB Patterns

This reference covers how the SDK uses DuckDB (file-backed SQL) and RocksDB (disk-backed
key-value store) internally, and what app developers need to know.

## Overview

| Technology | Purpose | Used By | App Developer Action |
|------------|---------|---------|---------------------|
| **DuckDB** | File-backed SQL analysis and queries on JSON files | `prepare_column_extraction_queries`, `get_backfill_tables`, `get_table_qns_from_columns` | None - SDK handles |
| **RocksDB** | Disk-backed key-value state storage | `TableScope.table_states` | None - SDK handles |

App developers do NOT need to write DuckDB or RocksDB code.
The SDK handles all of this internally. This reference is for understanding
how it works under the hood.

## DuckDB: File-Backed SQL Queries

### What It Does

DuckDB is used for two key operations:

1. **Table State Analysis**: Identify which tables need column extraction by reading
   transformed JSON files and filtering by incremental state (CREATED/UPDATED/BACKFILL).

2. **Backfill Detection**: Compare current vs previous state to find tables
   that exist now but weren't in the previous extraction.

3. **Column Table Extraction**: Query column JSON files to find which tables
   have extracted columns.

### Table State Analysis

```python
# SDK internal: application_sdk/common/incremental/column_extraction/analysis.py

def get_tables_needing_column_extraction(transformed_dir, backfill_qns):
    """Use DuckDB to identify tables needing column extraction."""
    from application_sdk.common.incremental.storage.duckdb_utils import (
        DuckDBConnectionManager, json_scan,
    )
    json_files = list((transformed_dir / "table").glob("*.json"))
    json_source = json_scan(json_files)

    with DuckDBConnectionManager() as db:
        conn = db.connection
        # State counts
        state_counts = conn.execute(f"""
            SELECT incremental_state, COUNT(*) AS cnt
            FROM (
                SELECT COALESCE(
                    json_extract_string(to_json(customAttributes), '$.incremental_state'),
                    'NO CHANGE'
                ) AS incremental_state
                FROM {json_source}
            )
            GROUP BY incremental_state
        """).fetchall()
        ...
```

### Backfill Detection

```python
# SDK internal: application_sdk/common/incremental/column_extraction/backfill.py

def get_backfill_tables(current_transformed_dir, previous_current_state_dir):
    """Find tables needing backfill via DuckDB set comparison."""
    if not previous_current_state_dir:
        return None

    conn = get_duckdb_connection()  # File-backed DuckDB

    # Current tables from transformed output
    current_tables = conn.sql(f"""
        SELECT DISTINCT json_extract_string(attributes, '$.qualifiedName') AS qn
        FROM read_json_auto('{current_transformed_dir}/table/*.json')
    """).fetchall()

    # Previous tables from current-state
    previous_tables = conn.sql(f"""
        SELECT DISTINCT json_extract_string(attributes, '$.qualifiedName') AS qn
        FROM read_json_auto('{previous_current_state_dir}/table/*.json')
    """).fetchall()

    current_set = {row[0] for row in current_tables}
    previous_set = {row[0] for row in previous_tables}

    # Tables in current but not in previous = need backfill
    return current_set - previous_set
```

### DuckDB Connection Management

```python
# SDK internal: application_sdk/common/incremental/storage/duckdb_utils.py

# File-backed, memory-limited connection (use as context manager)
with DuckDBConnectionManager() as db:
    conn = db.connection
    result = conn.execute("SELECT ...").fetchall()
# DuckDB temp files cleaned up automatically on exit
```

Key points:
- **File-backed**: DuckDB uses a file instead of memory for large datasets
- **Memory limited**: Default 2GB, configurable
- **Temp directory**: Uses `/tmp/incremental_duckdb`
- **`json_scan(files)`**: Helper that builds a DuckDB SQL fragment for reading
  JSONL files with `read_json_auto` and normalises `customAttributes` as JSON

## RocksDB: Disk-Backed State Storage

### What It Does

RocksDB (via `rocksdict` / `Rdict`) provides disk-backed key-value storage for
table states during ancestral merge. This is important for connectors with
millions of tables where keeping all states in memory would cause OOM.

```python
# SDK internal: application_sdk/common/incremental/storage/rocksdb_utils.py

def create_states_db(db_path=None):
    """Create RocksDB-backed dictionary for table states."""
    from rocksdict import Rdict, Options

    if db_path is None:
        db_path = tempfile.mkdtemp(prefix="incremental_states_")

    opts = Options()
    opts.set_bloom_filter(10, False)  # Bloom filter for fast key lookups
    opts.set_write_buffer_size(64 * 1024 * 1024)  # 64MB write buffer

    return Rdict(os.path.join(db_path, "states.db"), options=opts)
```

### Usage in TableScope

```python
# SDK internal: application_sdk/common/incremental/models.py

class TableScope(BaseModel):
    table_qualified_names: Set[str] = Field(default_factory=set)
    table_states: Any = Field(  # Rdict type (RocksDB)
        default_factory=create_states_db,
        exclude=True,
    )

# Usage in table_scope.py:
scope.table_states[qualified_name] = "CREATED"  # Write to RocksDB
state = scope.table_states.get(qualified_name)    # Read from RocksDB
```

Key points:
- **Bloom filter**: Fast negative lookups (skip tables not in scope)
- **Disk-backed**: Can handle millions of entries without memory issues
- **Automatic cleanup**: Temp directory cleaned when scope is garbage collected

## S3 Path Conventions

The SDK uses structured S3 paths for state management:

```
persistent-artifacts/
└── apps/
    └── {application_name}/
        └── connection/
            └── {connection_id}/
                ├── marker.txt                    # Marker timestamp
                ├── current-state/                # Latest state snapshot
                │   ├── database/
                │   │   └── *.json
                │   ├── schema/
                │   │   └── *.json
                │   ├── table/
                │   │   └── *.json
                │   └── column/
                │       └── *.json
                └── runs/
                    └── {run_id}/
                        └── incremental-diff/     # This run's changes only
                            ├── table/
                            ├── column/
                            └── ...
```

## Memory Optimization Patterns

### 1. Streaming over Loading

```python
# BAD: Load all data into memory
all_tables = json.loads(Path("table/").read_text())

# GOOD: Stream with DuckDB + row-by-row fetch
with DuckDBConnectionManager() as db:
    for row in db.connection.execute("SELECT ... FROM json_scan(...)").fetchmany(1000):
        process(row)
```

### 2. File-Backed over In-Memory

```python
# BAD: In-memory DuckDB
conn = duckdb.connect()  # In-memory, will OOM on large datasets

# GOOD: File-backed DuckDB with memory limits (use DuckDBConnectionManager)
with DuckDBConnectionManager() as db:
    conn = db.connection  # File-backed, 2GB limit, temp files auto-cleaned
```

### 3. Disk-Backed State

```python
# BAD: Python dict for table states
table_states = {}  # OOM with millions of tables

# GOOD: RocksDB disk-backed dict
table_states = Rdict("/tmp/states.db")  # Disk-backed, handles millions
```

## pyproject.toml Dependencies

```toml
[project]
dependencies = [
    "atlan-application-sdk[incremental,iam-auth,tests,workflows]==X.Y.Z",
    "rocksdict>=0.3.0",
]
```

The `[incremental]` extra brings in:
- `duckdb` + `duckdb-engine` — file-backed SQL engine
- `pyarrow` — columnar data I/O
- `pandas` — tabular helpers
- `sqlalchemy[asyncio]` — async DB connectivity
- `rocksdict` (via `[storage]`) — disk-backed state

`rocksdict` is also available as a standalone `[storage]` extra for non-incremental connectors.
