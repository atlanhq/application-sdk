# Inputs (I/O Readers)

This module provides a standardized way to read data from various sources within the Application SDK framework. It defines a common `Reader` interface and offers concrete implementations for reading from Parquet files, JSON files, and Iceberg tables.

## Core Concepts

1.  **`Reader` Interface (`application_sdk.io.Reader`)**:
    *   **Purpose:** An abstract base class defining the contract for reading data.
    *   **Key Methods:** Requires subclasses to implement methods for retrieving data as Pandas or Daft DataFrames:
        *   `read()`: Returns a single DataFrame (Pandas or Daft depending on `dataframe_type`).
        *   `read_batches()`: Returns an async iterator of DataFrames for memory-efficient processing.
    *   **Usage:** Activities instantiate a specific `Reader` subclass and use these methods to retrieve data for processing.

2.  **Concrete Implementations:** The SDK provides several reader classes:

    *   **`ParquetFileReader` (`application_sdk.io.parquet`)**: Reads data from Parquet files.
    *   **`JsonFileReader` (`application_sdk.io.json`)**: Reads data from JSON Lines files.
    *   **`IcebergTableReader` (`application_sdk.io.iceberg`)**: Reads data from Apache Iceberg tables.

## Object Store Integration (Automatic Download)

**All file-based readers automatically handle object store downloads**, making data access seamless:

### How It Works

1. **Check Local Files**: Reader first checks if files exist at the specified local `path`
2. **Auto-Download**: If files are not found locally, automatically downloads from object store
3. **Caching**: Downloads are cached locally for subsequent reads
4. **Transparent Access**: Your code simply calls `read()` - downloads happen automatically

This means you never need to manually download files from object storage - the readers handle it for you!

### Example Flow
```
Activity calls read() → Reader checks local path → Files missing?
  → Downloads from object store → Caches locally → Returns data
```

## Naming Convention

Reader classes follow a clear naming pattern that indicates what they work with:

- **`*FileReader`**: Work with file formats stored on disk
  - Read from Parquet, JSON, or other file formats
  - Automatically download from object store if needed
  - Examples: `ParquetFileReader`, `JsonFileReader`

- **`*TableReader`**: Work with managed table storage systems
  - Read directly from table engines like Apache Iceberg
  - Handle table-specific features (schema evolution, partitioning, time travel)
  - Examples: `IcebergTableReader`

## Usage Patterns and Examples

Readers are primarily used within **Activities** to fetch data for processing.

### ParquetFileReader & JsonFileReader

Used to read data from Parquet or JSON Lines files, with automatic object store download support.

**Initialization:**
```python
ParquetFileReader(
    path="local/path/to/data",           # Local path where files are or should be
    file_names=["file1.parquet", ...],    # Optional: specific files to read
    chunk_size=100000,                    # Optional: rows per batch
    dataframe_type=DataframeType.pandas          # or DataframeType.daft
)

JsonFileReader(
    path="local/path/to/data",
    file_names=["file1.json", ...],
    chunk_size=100000,
    dataframe_type=DataframeType.pandas
)
```

**Common Usage in Activities:**

```python
# Within a transform_data Activity method
from application_sdk.io.parquet import ParquetFileReader
from application_sdk.io import DataframeType

@activity.defn
@auto_heartbeater
async def transform_data(self, workflow_args: Dict[str, Any]):
    output_path = workflow_args.get("output_path")
    typename = workflow_args.get("typename", "data")
    file_names = workflow_args.get("file_names", [])

    # Path where files were written by a previous activity
    local_input_path = f"{output_path}/raw/{typename}"

    # Instantiate ParquetFileReader
    # If files aren't local, they'll be automatically downloaded from object store
    parquet_reader = ParquetFileReader(
        path=local_input_path,
        file_names=file_names,
        dataframe_type=DataframeType.daft  # Use Daft for better performance with large datasets
    )

    try:
        # Read data in batches for memory efficiency
        async for batch_df in parquet_reader.read_batches():
            # Process each batch (e.g., transform using state.transformer)
            transformed = await self.transformer.transform(batch_df)
            # Write transformed data...

    except Exception as e:
        logger.error(f"Error reading data: {e}", exc_info=True)
        raise
```

**Reading All Data at Once:**

```python
# For smaller datasets, read everything into a single DataFrame
from application_sdk.io.json import JsonFileReader
from application_sdk.io import DataframeType

json_reader = JsonFileReader(
    path="local/data/output",
    dataframe_type=DataframeType.pandas
)

# Read all data at once
df = await json_reader.read()
print(f"Read {len(df)} records")
```

### IcebergTableReader

Used for reading directly from Apache Iceberg tables (requires PyIceberg).

```python
from application_sdk.io.iceberg import IcebergTableReader
from application_sdk.io import DataframeType
from pyiceberg.catalog import load_catalog

# Load Iceberg catalog and table
catalog = load_catalog("my_catalog")
table = catalog.load_table("my_database.my_table")

# Create reader
iceberg_reader = IcebergTableReader(
    table=table,
    chunk_size=100000,
    dataframe_type=DataframeType.daft  # Iceberg works best with Daft
)

# Read data
df = await iceberg_reader.read()
```

## Advanced Features

### Batched Reading for Large Datasets

For memory-efficient processing of large datasets, use `read_batches()`:

```python
reader = ParquetFileReader(
    path="/data/large_dataset",
    chunk_size=50000,  # Process 50K rows at a time
    dataframe_type=DataframeType.daft
)

total_records = 0
async for batch in reader.read_batches():
    # Process each batch independently
    processed = process_batch(batch)
    total_records += batch.count_rows()

print(f"Processed {total_records} records")
```

### File Filtering

Read only specific files from a directory:

```python
reader = ParquetFileReader(
    path="/data/partitioned",
    file_names=[
        "chunk-0-0.parquet",
        "chunk-0-1.parquet"
    ]  # Only read these specific files
)
```

### DataFrame Type Selection

Choose between Pandas and Daft based on your use case:

```python
# Pandas - Better for small datasets, rich API
pandas_reader = JsonFileReader(
    path="/data",
    dataframe_type=DataframeType.pandas
)

# Daft - Better for large datasets, distributed processing
daft_reader = JsonFileReader(
    path="/data",
    dataframe_type=DataframeType.daft
)
```

## Summary

The readers module provides convenient classes for reading data from diverse sources (Parquet, JSON, Iceberg). Key features include:

- **Automatic object store downloads** - no manual file management needed
- **Memory-efficient batched reading** - process large datasets without loading everything into memory
- **Flexible DataFrame support** - choose Pandas or Daft based on your needs
- **Transparent caching** - downloaded files are cached locally for performance

These readers integrate seamlessly with the SDK's activity patterns and work hand-in-hand with Writers for complete data pipeline workflows.
