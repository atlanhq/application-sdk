# Outputs (I/O Writers)

This module provides a standardized way to write data to various destinations within the Application SDK framework. It defines a common `Writer` interface and offers concrete implementations for common formats like JSON Lines and Parquet.

## Core Concepts

1.  **`Writer` Interface (`application_sdk.io.Writer`)**:
    *   **Purpose:** An abstract base class defining the contract for writing data.
    *   **Key Methods:** Requires subclasses to implement methods for writing Pandas or Daft DataFrames:
        *   `write(data)`: Write a DataFrame, dict, or list of dicts.
        *   `write_batches(dataframe: Union[AsyncGenerator, Generator])`: Write batched DataFrames.
        *   `close()`: Flush buffers, upload files, and return statistics.
    *   **Context Manager:** Writers support `async with` for automatic cleanup.
    *   **Usage:** Activities typically instantiate a specific `Writer` subclass and use its write methods to persist data fetched or generated during the activity.

2.  **Concrete Implementations:** The SDK provides several writer classes:

    *   **`JsonFileWriter` (`application_sdk.io.json`)**: Writes DataFrames to JSON Lines files (`.json`).
    *   **`ParquetFileWriter` (`application_sdk.io.parquet`)**: Writes DataFrames to Parquet files (`.parquet`).

## Object Store Integration (Automatic Upload)

**All file-based writers automatically handle object store uploads**, making data persistence seamless:

### How It Works

1. **Write Locally**: Writer first writes files to the specified local `output_path`
2. **Auto-Upload**: After writing completes, automatically uploads files to object store
3. **Optional Cleanup**: Can optionally retain or delete local copies after upload
4. **Transparent Persistence**: Your code simply calls `write()` - uploads happen automatically

This means you never need to manually upload files to object storage - the writers handle it for you!

### Complete Data Flow
```
Activity calls write() → Writer writes to local path →
  → Uploads to object store → Optionally cleans up local files →
  → Returns statistics
```

## Naming Convention

Writer classes follow a clear naming pattern that indicates what they work with:

- **`*FileWriter`**: Work with file formats stored on disk
  - Write to Parquet, JSON, or other file formats
  - Automatically upload to object store after writing
  - Support chunking and compression
  - Examples: `ParquetFileWriter`, `JsonFileWriter`

## Writer API Patterns

Writers follow Python's familiar file I/O pattern with `write()` and `close()` methods:

### Basic Usage with close()

```python
from application_sdk.io.json import JsonFileWriter

writer = JsonFileWriter(path="/data/output")

# Write DataFrames
await writer.write(dataframe)

# Write dicts directly (converted to single-row DataFrame)
await writer.write({"name": "example", "value": 123})

# Write list of dicts (converted to multi-row DataFrame)
await writer.write([{"name": "a"}, {"name": "b"}])

# Close and get statistics
stats = await writer.close()
print(f"Wrote {stats.total_record_count} records")
```

### Context Manager (Recommended)

Using `async with` ensures the writer is always properly closed:

```python
async with JsonFileWriter(path="/data/output") as writer:
    await writer.write(dataframe)
    await writer.write({"key": "value"})
# close() is called automatically on exit
```

### Key Behaviors

- **Idempotent close()**: Calling `close()` multiple times is safe
- **Write after close**: Raises `ValueError("Cannot write to a closed writer")`
- **Statistics property**: `writer.statistics` returns current stats without closing

## `JsonFileWriter` (`application_sdk.io.json`)

Writes Pandas or Daft DataFrames to one or more JSON Lines files locally, optionally uploading them to an object store.

### Features

*   **DataFrame Support:** Can write both Pandas and Daft DataFrames. Daft DataFrames are processed row-by-row using `orjson` for memory efficiency.
*   **Chunking:** Automatically splits large DataFrames into multiple output files based on the `chunk_size` parameter.
*   **Buffering (Pandas):** For Pandas DataFrames, uses an internal buffer to accumulate data before writing chunks, controlled by `buffer_size`.
*   **File Naming:** Uses a `path_gen` function to name output files, typically incorporating chunk numbers (e.g., `1.json`, `2-100.json`). Can be customized.
*   **Object Store Integration:** After writing files locally to the specified `path`, it uploads the generated files to object storage.
*   **Statistics:** Tracks `total_record_count` and `chunk_count` and saves them via `write_statistics`.

### Initialization

`JsonFileWriter(path, typename=..., chunk_start=..., chunk_size=..., ...)`

*   `path` (str): The full path where files will be written (e.g., `/data/workflow_run_123/transformed`). The caller should construct this path explicitly.
*   `typename` (str, optional): A subdirectory name added under `path` (e.g., `tables`, `columns`). Helps organize output.
*   `chunk_start` (int, optional): Starting index for chunk numbering in filenames.
*   `chunk_size` (int, optional): Maximum number of records per output file chunk (default: 50,000).

### Common Usage

`JsonFileWriter` (and similarly `ParquetFileWriter`) is typically used within activities that fetch data and need to persist it for subsequent steps, like a transformation activity.

```python
# Within an Activity method (e.g., query_executor in SQL extraction/query activities)
import os
from application_sdk.io.json import JsonFileWriter
# ... other imports ...

async def query_executor(
    self,
    sql_client: Any,
    sql_query: Optional[str],
    workflow_args: Dict[str, Any],
    output_path: str,   # Full path, e.g., "/data/workflow_run_123/raw/table"
    typename: str,      # e.g., "table", "column"
) -> Optional[Dict[str, Any]]:

    # ... (validate inputs, prepare query) ...

    # Instantiate JsonFileWriter with the full output path
    json_writer = JsonFileWriter(
        path=output_path,  # Full path provided by caller
        typename=typename,
        # chunk_size=... (optional)
    )

    try:
        # Get data using the SQL client (e.g., fetch results)
        results = []
        async for batch in sql_client.run_query(prepared_query):
            results.extend(batch)

        # Write the data using the Writer class
        # This writes locally then uploads to object store
        # Convert results to DataFrame first if needed
        import pandas as pd
        df = pd.DataFrame(results)
        await json_writer.write(df)

        # Close writer and get statistics (record count, chunk count)
        # typename is automatically taken from the writer's typename attribute
        stats = await json_writer.close()
        return stats.model_dump()

    except Exception as e:
        logger.error(f"Error executing query and writing output for {typename}: {e}", exc_info=True)
        raise

# Example: Constructing the output path in the caller
base_output_path = workflow_args.get("output_path", "")
full_output_path = os.path.join(base_output_path, "raw", "table")
await query_executor(sql_client, sql_query, workflow_args, full_output_path, "table")
```

## Other Writer Handlers

*   **`ParquetFileWriter`:** Similar to `JsonFileWriter` but writes DataFrames to Parquet format files. Uses `daft.DataFrame.write_parquet()` or `pandas.DataFrame.to_parquet()`. Also uploads files to object storage after local processing. Supports consolidation mode for efficient writing of large datasets.

## Summary

The I/O module provides both reader and writer classes for data persistence. `JsonFileWriter` and `ParquetFileWriter` are commonly used for saving intermediate DataFrames to local files (and then uploading them to object storage), making the data available for subsequent activities like transformations.