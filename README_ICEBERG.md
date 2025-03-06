# Iceberg Client and Handler for Application SDK

This implementation provides an Iceberg client and handler for the Application SDK, enabling data extraction from Iceberg tables using the Daft engine.

## Overview

The Iceberg integration follows the same patterns as the existing SQL implementation in the Application SDK, with a focus on data extraction rather than complex metadata operations. It leverages the PyIceberg library for Iceberg catalog connections and the Daft engine for efficient data processing.

### Key Components

- **IcebergClient**: Extends `ClientInterface` to provide connectivity to Iceberg catalogs and data extraction capabilities
- **IcebergHandler**: Extends `HandlerInterface` to orchestrate operations on Iceberg tables
- **IcebergInput**: Used to read data from Iceberg tables, supports batch processing
- **IcebergOutput**: Used to write data to Iceberg tables with configurable modes

## Setup

### Dependencies

The implementation requires the following dependencies:

```
pyiceberg>=0.6.0
daft>=0.2.19
```

### Connection Configuration

The IcebergClient supports the following connection parameters:

- `catalog_uri`: URI for connecting to the Iceberg catalog (e.g., `sqlite:///samplelake/catalog.db`)
- `warehouse_path`: Path to the Iceberg warehouse (e.g., `file:///path/to/samplelake`)
- `namespace`: Iceberg namespace (e.g., `lake`)

## Usage

### Basic Connection

```python
from application_sdk.clients.iceberg import IcebergClient

# Create and initialize an Iceberg client
iceberg_client = IcebergClient()
await iceberg_client.load({
    "catalog_uri": "sqlite:///samplelake/catalog.db",
    "warehouse_path": "file:///path/to/samplelake",
    "namespace": "lake"
})

# Run a simple query to extract data
async for batch in iceberg_client.run_query("lake.entities"):
    # Process each batch of data
    print(f"Retrieved {len(batch)} records")

# Close the connection
await iceberg_client.close()
```

### Advanced Querying with Filtering

The `run_query` method supports two formats:

1. **Simple string format**: Just the table identifier
   ```python
   query = "lake.entities"
   ```

2. **Dictionary format with filtering**:
   ```python
   query = {
       "table": "lake.entities",
       "select": ["name", "qualifiedName", "type"],  # Optional: select specific columns
       "where": {"field": "type", "op": "=", "value": "table"},  # Optional: filtering
       "limit": 1000  # Optional: limit results
   }
   ```

Example with complex filtering:

```python
# Query with multiple conditions
query = {
    "table": "lake.entities",
    "select": ["name", "qualifiedName", "description"],
    "where": {
        "and": [
            {"field": "type", "op": "=", "value": "table"},
            {"field": "name", "op": "starts with", "value": "customer"}
        ]
    },
    "limit": 500
}

async for batch in iceberg_client.run_query(query):
    # Process filtered data
    print(f"Retrieved {len(batch)} filtered records")
```

### Using IcebergInput and IcebergOutput

```python
from pyiceberg.catalog.sql import SQLCatalog
from application_sdk.inputs.iceberg import IcebergInput
from application_sdk.outputs.iceberg import IcebergOutput

# Set up a catalog
catalog = SQLCatalog("iceberg", uri="sqlite:///samplelake/catalog.db", warehouse="file:///path/to/samplelake")

# Load a table
table = catalog.load_table("lake.entities")

# Read data using IcebergInput
input_obj = IcebergInput(table=table)
df = input_obj.get_dataframe()

# Write data using IcebergOutput
output = IcebergOutput(
    iceberg_catalog=catalog,
    iceberg_namespace="lake",
    iceberg_table="entities_copy",
    mode="overwrite"
)
await output.write_dataframe(df)
```

### Using the Transform Decorator

The Iceberg handler implements the `@transform` decorator pattern used throughout the SDK:

```python
from application_sdk.decorators import transform
from application_sdk.inputs.iceberg import IcebergInput
from application_sdk.outputs.json import JsonOutput

class MyIcebergProcessor:
    @transform(
        table_data=IcebergInput(table=None),  # Will be filled by decorator
        json_output=JsonOutput(output_suffix="/data")
    )
    async def process_iceberg_data(
        self,
        table_data: pd.DataFrame,
        json_output: JsonOutput,
        **kwargs
    ):
        # Process the data
        processed_data = transform_data(table_data)
        
        # Write to output
        await json_output.write_dataframe(processed_data)
        
        return {"status": "success", "record_count": len(processed_data)}
```

### Using the Handler

```python
from application_sdk.clients.iceberg import IcebergClient
from application_sdk.handlers.iceberg import IcebergHandler

# Create client and handler
iceberg_client = IcebergClient()
iceberg_handler = IcebergHandler(iceberg_client)

# Load the client with credentials
await iceberg_handler.load({
    "catalog_uri": "sqlite:///samplelake/catalog.db",
    "warehouse_path": "file:///path/to/samplelake",
    "namespace": "lake"
})

# Run a preflight check to ensure table access
result = await iceberg_handler.preflight_check({
    "namespace": "lake",
    "table": "entities"
})

# Fetch metadata about available tables
tables = await iceberg_handler.fetch_metadata(MetadataType.SCHEMA, "lake")
```

### Full Example

See `examples/application_iceberg.py` for a complete example of using the Iceberg client and handler with Temporal workflows for data extraction.

## Key Features

### Advanced Query and Filtering Capabilities

The `IcebergClient.run_query()` method supports:

- Simple table name queries
- Column selection and projection
- Complex filtering expressions:
  - Comparison operators: `=`, `!=`, `>`, `>=`, `<`, `<=`
  - Containment operators: `in`, `not in`
  - String operations: `starts with`, `not starts with`
  - Null checking: `is null`, `is not null`
  - Logical operators: `and`, `or`, `not`
- Result limiting

### Comprehensive Authentication and Access Control

The `IcebergHandler` implements a multi-layered authentication and access control system:

1. **Catalog Access**: Verifies the ability to connect to and list namespaces in the catalog
2. **Namespace Access**: Checks if the specified namespace exists and is accessible
3. **Table Access**: Verifies existence and accessibility of specific tables
4. **Metadata Access**: Ensures metadata can be read from tables

These checks are performed during:
- Authentication testing
- Preflight checks
- Before metadata operations

### Metadata Operations

The `IcebergHandler` provides methods for retrieving metadata about Iceberg catalogs and tables:

- `fetch_metadata()`: Retrieve information about namespaces and tables
- `fetch_table_schema()`: Get the schema of a specific table

### Local Testing

The implementation supports local testing with a sample Iceberg lake:

- SQLite-based catalog at `samplelake/catalog.db`
- Local warehouse at `file:///path/to/samplelake`
- Sample namespace `lake` and table `entities`

## Implementation Details

### IcebergClient

The `IcebergClient` class extends `ClientInterface` and provides methods for:

- Connecting to an Iceberg catalog
- Extracting data from Iceberg tables with advanced filtering
- Batch processing of large datasets

### IcebergHandler

The `IcebergHandler` class extends `HandlerInterface` and provides methods for:

- Testing authentication with the `@transform` decorator
- Comprehensive access control checks at catalog, namespace, and table levels
- Performing preflight checks
- Fetching metadata about namespaces and tables

### IcebergInput

The `IcebergInput` class extends `Input` and provides methods for:

- Reading data from Iceberg tables using Daft
- Converting data to pandas DataFrames
- Supporting both full and batched reading modes

### IcebergOutput

The `IcebergOutput` class extends `Output` and provides methods for:

- Writing pandas DataFrames to Iceberg tables
- Writing Daft DataFrames to Iceberg tables
- Supporting different write modes (append, overwrite, etc.)

## Differences from SQL Implementation

- Connection is to an Iceberg catalog, not a database server
- Query execution uses the Daft engine instead of direct SQL
- Advanced filtering capabilities using Iceberg's expression model
- Comprehensive multi-level permission checks for catalog, namespace, and table access
- Metadata structure uses namespaces instead of schemas and catalogs
- Uses decorators in the same pattern as SQL implementation 