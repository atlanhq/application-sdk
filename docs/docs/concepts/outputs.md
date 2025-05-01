# Outputs

This module provides a standardized way to write data to various destinations within the Application SDK framework. It mirrors the `inputs` module by defining a common `Output` interface and offering concrete implementations for common formats like JSON Lines and Parquet, along with helpers for interacting with object stores, state stores, and secret stores.

## Core Concepts

1.  **`Output` Interface (`application_sdk.outputs.__init__.py`)**:
    *   **Purpose:** An abstract base class defining the contract for writing data.
    *   **Key Methods:** Requires subclasses to implement methods for writing Pandas or Daft DataFrames:
        *   `write_dataframe(dataframe: pd.DataFrame)`: Write a single Pandas DataFrame.
        *   `write_daft_dataframe(dataframe: daft.DataFrame)`: Write a single Daft DataFrame.
    *   **Helper Methods:** Provides base implementations for writing *batched* DataFrames (`write_batched_dataframe`, `write_batched_daft_dataframe`) which iterate over input generators/async generators and call the corresponding single DataFrame write methods.
    *   **Statistics:** Includes methods (`get_statistics`, `write_statistics`) to track and save metadata about the output (record count, chunk count) to a `statistics.json.ignore` file, typically alongside the data output.
    *   **Usage:** Activities typically instantiate a specific `Output` subclass and use its write methods to persist data fetched or generated during the activity.

2.  **Concrete Implementations:** The SDK provides several output classes:

    *   **`JsonOutput` (`json.py`)**: Writes DataFrames to JSON Lines files (`.json`).
    *   **`ParquetOutput` (`parquet.py`)**: Writes DataFrames to Parquet files (`.parquet`).
    *   **`ObjectStoreOutput` (`objectstore.py`)**: Uploads local files or directories to a configured Dapr object store binding. Used *internally* by other outputs like `JsonOutput` and `ParquetOutput`.
    *   **`SecretStoreOutput` (`secretstore.py`)**: Saves sensitive data (like credentials) securely, typically via the StateStore using a Dapr binding.
    *   **`StateStoreOutput` (`statestore.py`)**: Saves configuration or state data to the configured Dapr state store binding.
    *   **`IcebergOutput` (`iceberg.py`)**: Writes DataFrames to Apache Iceberg tables.
    *   **`EventStore` (`eventstore.py`)**: Publishes events (like workflow/activity start/end) to a configured Dapr pub/sub binding.

## `JsonOutput` (`json.py`)

Writes Pandas or Daft DataFrames to one or more JSON Lines files locally, optionally uploading them to an object store.

### Features

*   **DataFrame Support:** Can write both Pandas (`write_dataframe`) and Daft (`write_daft_dataframe`) DataFrames. Daft DataFrames are processed row-by-row using `orjson` for memory efficiency.
*   **Chunking:** Automatically splits large DataFrames into multiple output files based on the `chunk_size` parameter.
*   **Buffering (Pandas):** For Pandas DataFrames, uses an internal buffer to accumulate data before writing chunks, controlled by `buffer_size`.
*   **File Naming:** Uses a `path_gen` function to name output files, typically incorporating chunk numbers (e.g., `1.json`, `2-100.json`). Can be customized.
*   **Object Store Integration:** After writing files locally to the specified `output_path`, it uses `ObjectStoreOutput` internally to upload the generated files to the location specified by `output_prefix`.
*   **Statistics:** Tracks `total_record_count` and `chunk_count` and saves them via `write_statistics`.

### Initialization

`JsonOutput(output_suffix, output_path=..., output_prefix=..., typename=..., chunk_start=..., chunk_size=..., ...)`

*   `output_suffix` (str): A suffix added to the base `output_path`. Often used for specific runs or data types.
*   `output_path` (str): The base *local* directory where files will be temporarily written (e.g., `/data/workflow_run_123`). The final local path becomes `{output_path}/{output_suffix}/{typename}`.
*   `output_prefix` (str): The prefix/path in the **object store** where the locally written files will be uploaded.
*   `typename` (str, optional): A subdirectory name added under `{output_path}/{output_suffix}` (e.g., `tables`, `columns`). Helps organize output.
*   `chunk_start` (int, optional): Starting index for chunk numbering in filenames.
*   `chunk_size` (int, optional): Maximum number of records per output file chunk (default: 100,000).

### Common Usage

`JsonOutput` (and similarly `ParquetOutput`) is typically used within activities that fetch data and need to persist it for subsequent steps, like a transformation activity.

```python
# Within an Activity method (e.g., query_executor in SQL extraction/query activities)
from application_sdk.outputs.json import JsonOutput
# ... other imports, including SQLQueryInput etc ...

async def query_executor(
    self,
    sql_engine: Any,
    sql_query: Optional[str],
    workflow_args: Dict[str, Any],
    output_suffix: str, # e.g., workflow_run_id
    typename: str,      # e.g., "table", "column"
) -> Optional[Dict[str, Any]]:

    # ... (validate inputs, prepare query) ...

    sql_input = SQLQueryInput(engine=sql_engine, query=prepared_query)

    # Get output path details from workflow_args
    output_prefix = workflow_args.get("output_prefix") # Object store path
    output_path = workflow_args.get("output_path")     # Base local path

    if not output_prefix or not output_path:
        raise ValueError("output_prefix and output_path are required in workflow_args")

    # Instantiate JsonOutput
    json_output = JsonOutput(
        output_suffix=output_suffix,
        output_path=output_path,         # Local base path
        output_prefix=output_prefix,     # Object store base path
        typename=typename,
        # chunk_size=... (optional)
    )

    try:
        # Get data using the Input class (e.g., Daft DataFrame)
        daft_df = await sql_input.get_daft_dataframe()

        # Write the DataFrame using the Output class
        # This writes locally then uploads to object store path: {output_prefix}/{output_suffix}/{typename}/
        await json_output.write_daft_dataframe(daft_df)

        # Get statistics (record count, chunk count) after writing
        stats = await json_output.get_statistics(typename=typename)
        return stats.model_dump()

    except Exception as e:
        logger.error(f"Error executing query and writing output for {typename}: {e}", exc_info=True)
        raise
```

## Other Output Handlers

*   **`ParquetOutput`:** Similar to `JsonOutput` but writes DataFrames to Parquet format files. Uses `daft.DataFrame.write_parquet()` or `pandas.DataFrame.to_parquet()`. Also integrates with `ObjectStoreOutput` for uploading.
*   **`ObjectStoreOutput`:** Provides class methods (`push_file_to_object_store`, `push_files_to_object_store`) to upload local files/directories to the configured Dapr object store binding. Used by `JsonOutput` and `ParquetOutput`.
*   **`StateStoreOutput` / `SecretStoreOutput`:** Save workflow configuration and credentials using the Dapr state store binding. Used internally by `TemporalWorkflowClient` when starting workflows.
*   **`IcebergOutput`:** Writes DataFrames directly to an Iceberg table using `pyiceberg`.
*   **`EventStore`:** Publishes structured events (defined in `eventstore.py`) to a Dapr pub/sub binding. Used by `EventInterceptor` in `temporal.py` to track workflow/activity start and end events.

## Summary

The `outputs` module complements the `inputs` module by providing classes to write data processed within activities. `JsonOutput` and `ParquetOutput` are commonly used for saving intermediate DataFrames to local files (and then uploading them to an object store via `ObjectStoreOutput`), making the data available for subsequent activities like transformations. Other outputs handle specific tasks like saving state/secrets, writing to Iceberg, or publishing events.