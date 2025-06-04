# Inputs

This module provides a standardized way to read data from various sources within the Application SDK framework. It defines a common `Input` interface and offers concrete implementations for common data sources like SQL databases, Parquet files, JSON files, and configuration stores.

## Core Concepts

1.  **`Input` Interface (`application_sdk.inputs.__init__.py`)**:
    *   **Purpose:** An abstract base class defining the contract for reading data.
    *   **Key Methods:** Requires subclasses to implement methods for retrieving data, primarily focused on returning results as Pandas or Daft DataFrames, either entirely or in batches:
        *   `get_dataframe()`: Returns a single Pandas DataFrame.
        *   `get_batched_dataframe()`: Returns an iterator/async iterator of Pandas DataFrames.
        *   `get_daft_dataframe()`: Returns a single Daft DataFrame.
        *   `get_batched_daft_dataframe()`: Returns an iterator/async iterator of Daft DataFrames.
    *   **Usage:** Activities typically instantiate a specific `Input` subclass and use one of these methods to retrieve the data they need to process.

2.  **Concrete Implementations:** The SDK provides several input classes:

    *   **`SQLQueryInput` (`sql_query.py`)**: Reads data from a SQL database by executing a query.
    *   **`ParquetInput` (`parquet.py`)**: Reads data from Parquet files (single file or directory).
    *   **`JsonInput` (`json.py`)**: Reads data from JSON files (specifically JSON Lines format).
    *   **`ObjectStoreInput` (`objectstore.py`)**: Downloads files from a configured Dapr object store binding. Used *internally* by other inputs like `ParquetInput` and `JsonInput`.
    *   **`SecretStoreInput` (`secretstore.py`)**: Retrieves sensitive data (like credentials) stored securely, typically via the StateStore.
    *   **`StateStoreInput` (`statestore.py`)**: Retrieves configuration or state data from the configured Dapr state store binding.
    *   **`IcebergInput` (`iceberg.py`)**: Reads data from Apache Iceberg tables.

## Usage Patterns and Examples

Inputs are primarily used within **Activities** to fetch the data required for a specific workflow step.

### `SQLQueryInput`

Used to execute a SQL query and retrieve results as a DataFrame.

*   **Initialization:** `SQLQueryInput(engine, query, chunk_size=...)`
    *   `engine`: An initialized SQLAlchemy engine, typically obtained from the `SQLClient` stored in the activity state (`state.sql_client.engine`).
    *   `query`: The SQL query string to execute.
    *   `chunk_size` (optional): How many rows to fetch per batch when using batched methods.
*   **Common Usage:** Within SQL-based activities (like `BaseSQLMetadataExtractionActivities`) to fetch databases, schemas, tables, columns, etc.

```python
# Within an Activity method (e.g., fetch_tables in BaseSQLMetadataExtractionActivities)
from application_sdk.inputs.sql_query import SQLQueryInput
# ... other imports ...

async def fetch_tables(self, workflow_args: Dict[str, Any]):
    # ... get state, prepare query ...
    state: BaseSQLMetadataExtractionActivitiesState = await self._get_state(workflow_args)
    prepared_query = prepare_query(self.fetch_table_sql, workflow_args, ...) # Prepare query string

    if not prepared_query or not state.sql_client or not state.sql_client.engine:
        logger.warning("Missing SQL client engine or query for fetching tables.")
        return None

    # Instantiate SQLQueryInput with the client's engine and the specific query
    sql_input = SQLQueryInput(engine=state.sql_client.engine, query=prepared_query)

    # Get results as a Daft DataFrame
    try:
        daft_df = await sql_input.get_daft_dataframe()
        # Process the daft_df (e.g., write to ParquetOutput)
        # ...
        return {"typename": "table", "total_record_count": len(daft_df), ...}

    except Exception as e:
        logger.error(f"Failed to fetch tables: {e}", exc_info=True)
        raise
```

### `SecretStoreInput` & `StateStoreInput`

Used to retrieve configuration and secrets, often during the initialization phase of workflows or activities. These are class-based and use Dapr client internally.

*   **`SecretStoreInput.extract_credentials(credential_guid)`**: Retrieves credentials associated with a GUID from the state store (key format: `credential_{guid}`).
*   **`StateStoreInput.extract_configuration(config_id)`**: Retrieves workflow configuration associated with an ID from the state store (key format: `config_{id}`).
*   **`StateStoreInput.get_state(key)`**: Retrieves arbitrary data for a given key from the state store.
*   **Common Usage:**
    *   `extract_configuration` is used by the base `Workflow.run` method to load arguments.
    *   `extract_credentials` is used by activities (e.g., in `_set_state`) to get credentials needed to load clients.

```python
# Within an Activity's _set_state method
from application_sdk.inputs.secretstore import SecretStoreInput
# ... other imports ...

async def _set_state(self, workflow_args: Dict[str, Any]):
    # ... initialize state object ...
    sql_client = self.sql_client_class()
    handler = self.handler_class(sql_client)
    self._state[workflow_id].handler = handler

    # Get credentials using SecretStoreInput
    if "credential_guid" in workflow_args:
        credentials = SecretStoreInput.extract_credentials(workflow_args["credential_guid"])
        # Load the client using the retrieved credentials
        await sql_client.load(credentials)

    self._state[workflow_id].sql_client = sql_client
    # ... setup transformer ...
```

```python
# Within a Workflow's run method (simplified from base class)
from application_sdk.inputs.statestore import StateStoreInput
# ... other imports ...

@workflow.run
async def run(self, workflow_config: Dict[str, Any]) -> None:
    workflow_id = workflow_config["workflow_id"]
    # Get the workflow configuration from the state store
    workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
        self.activities_cls.get_workflow_args,
        workflow_config,  # Pass the whole config containing workflow_id
        retry_policy=RetryPolicy(maximum_attempts=3, backoff_coefficient=2),
        start_to_close_timeout=self.default_start_to_close_timeout,
        heartbeat_timeout=self.default_heartbeat_timeout,
    )
    # ... proceed with workflow logic using workflow_args ...
```

### `ParquetInput` & `JsonInput`

Used to read data from local Parquet or JSON Lines files, often downloaded from an object store first.

*   **Initialization:**
    *   `ParquetInput(path, chunk_size=..., input_prefix=..., file_names=...)`
    *   `JsonInput(path, file_names=..., download_file_prefix=..., chunk_size=...)`
    *   `path`: Local directory containing the files.
    *   `file_names`: Specific list of files within `path` to read.
    *   `input_prefix` / `download_file_prefix`: If provided, the input class will use `ObjectStoreInput` internally to download the specified `file_names` (or all files if `file_names` is None for Parquet) from this object store prefix into the local `path` before reading.
    *   `chunk_size` (optional): Rows per batch for batched reading methods.
*   **Common Usage:** Often used in `transform_data` activities where data fetched in a previous step was saved as Parquet/JSON by an `Output` class. The activity reads this intermediate data for transformation.

```python
# Within a transform_data Activity method
from application_sdk.inputs.parquet import ParquetInput
# ... other imports ...

@activity.defn
@auto_heartbeater
async def transform_data(self, workflow_args: Dict[str, Any]):
    output_prefix, output_path, typename, workflow_id, workflow_run_id = self._validate_output_args(workflow_args)
    file_names = workflow_args.get("file_names", []) # List of files to process

    # Path where files were likely written by a previous activity's Output
    local_input_path = f"{output_path}/{typename}"

    # Instantiate ParquetInput to read the files written earlier
    # input_prefix=output_prefix ensures files are downloaded from object store if not local
    parquet_input = ParquetInput(
        path=local_input_path,
        input_prefix=output_prefix,
        file_names=file_names
    )

    try:
        # Read the data (example: get batched daft dataframes)
        async for batch_df in parquet_input.get_batched_daft_dataframe():
            # Process each batch_df (e.g., transform using state.transformer)
            # ...
            pass
        # ... handle results ...
    except Exception as e:
        logger.error(f"Error transforming data from Parquet: {e}", exc_info=True)
        raise
```

### Other Inputs

*   **`ObjectStoreInput`:** Primarily used internally by other inputs (`ParquetInput`, `JsonInput`) to handle downloading from Dapr object store bindings. Less common for direct use in activities unless dealing with non-standard file types.
*   **`IcebergInput`:** Used for reading directly from Iceberg tables. Requires a `pyiceberg.table.Table` object during initialization.

## Summary

The `inputs` module provides convenient classes for reading data from diverse sources (SQL, Parquet, JSON, State/Secret Stores, Object Stores, Iceberg) within activities. They abstract the underlying read logic and often provide results as Pandas or Daft DataFrames, integrating seamlessly with the SDK's activity patterns and other components like Clients and Outputs. `StateStoreInput` and `SecretStoreInput` are crucial for accessing workflow configuration and credentials managed via Dapr.