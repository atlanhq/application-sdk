# Daft, DuckDB, and RocksDB Patterns

This reference covers how the SDK uses Daft (lazy DataFrames), DuckDB (file-backed SQL),
and RocksDB (disk-backed key-value store) internally, and what app developers need to know.

## Overview

| Technology | Purpose | Used By | App Developer Action |
|------------|---------|---------|---------------------|
| **Daft** | Lazy DataFrame analysis of table metadata | `prepare_column_extraction_queries` | None - SDK handles |
| **DuckDB** | File-backed SQL queries on JSON files | `get_backfill_tables`, `get_table_qns_from_columns` | None - SDK handles |
| **RocksDB** | Disk-backed key-value state storage | `TableScope.table_states` | None - SDK handles |

App developers do NOT need to write Daft, DuckDB, or RocksDB code.
The SDK handles all of this internally. This reference is for understanding
how it works under the hood.

## Daft: Lazy DataFrame Analysis

### What It Does

Daft is used in `prepare_column_extraction_queries` to lazily analyze table
metadata and identify which tables need column extraction:

```python
# SDK internal: application_sdk/common/incremental/column_extraction/analysis.py
import daft

def get_tables_needing_column_extraction(transformed_dir, backfill_qns):
    """Use Daft to identify tables needing column extraction."""
    # Read all table JSON files lazily (no full memory load)
    df = daft.read_json(str(transformed_dir / "table" / "*.json"))

    # Filter to CREATED/UPDATED tables (changed since last run)
    changed = df.where(
        (df["attributes"].struct.get("incremental_state") == "CREATED") |
        (df["attributes"].struct.get("incremental_state") == "UPDATED")
    )

    # Also include backfill tables (detected by DuckDB comparison)
    if backfill_qns:
        backfill = df.where(
            df["attributes"].struct.get("qualifiedName").is_in(backfill_qns)
        )
        filtered = changed.concat(backfill)
    else:
        filtered = changed

    return filtered, changed_count, backfill_count, no_change_count
```

### Why Daft?

- **Lazy execution**: Doesn't load all JSON files into memory
- **Struct field access**: Can query nested JSON fields (e.g., `attributes.incremental_state`)
- **Memory efficient**: Processes large datasets without OOM
- **Parallel**: Automatically parallelizes reads across files

### Daft Struct Field Access Pattern

```python
# Access nested struct fields in Daft DataFrames
df["attributes"].struct.get("qualifiedName")     # Get field from struct
df["attributes"].struct.get("incremental_state")  # Get incremental state
```

**Important**: Daft API changed in 0.7.2+. Use `.struct.get("field")` not `.struct["field"]`.

## DuckDB: File-Backed SQL Queries

### What It Does

DuckDB is used for two key operations:

1. **Backfill Detection**: Compare current vs previous state to find tables
   that exist now but weren't in the previous extraction

2. **Column Table Extraction**: Query column JSON files to find which tables
   have extracted columns

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

def get_duckdb_connection(db_path=None):
    """Create file-backed DuckDB connection with memory limits."""
    import duckdb

    if db_path is None:
        db_path = os.path.join(DUCKDB_COMMON_TEMP_FOLDER, "incremental.duckdb")

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = duckdb.connect(db_path)
    conn.execute(f"SET memory_limit = '{DUCKDB_DEFAULT_MEMORY_LIMIT}'")
    conn.execute(f"SET temp_directory = '{DUCKDB_COMMON_TEMP_FOLDER}'")

    return conn
```

Key points:
- **File-backed**: DuckDB uses a file instead of memory for large datasets
- **Memory limited**: Default 2GB, configurable
- **Temp directory**: Uses `/tmp/incremental_duckdb`

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

# GOOD: Stream with Daft lazy execution
df = daft.read_json("table/*.json")  # Lazy - no memory until .collect()
for row in df.iter_rows():  # Stream rows one at a time
    process(row)
```

### 2. File-Backed over In-Memory

```python
# BAD: In-memory DuckDB
conn = duckdb.connect()  # In-memory, will OOM on large datasets

# GOOD: File-backed DuckDB with memory limits
conn = duckdb.connect("/tmp/incremental.duckdb")
conn.execute("SET memory_limit = '2GB'")
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
    "atlan-application-sdk[daft,iam-auth,sqlalchemy,tests,workflows,pandas]==X.Y.Z",
    "rocksdict>=0.3.0",
]
```

The `[daft]` extra brings in:
- `daft` - Lazy DataFrame library
- `duckdb` - File-backed SQL engine (Daft dependency)

`rocksdict` must be listed separately as it's not part of the SDK extras (yet).
