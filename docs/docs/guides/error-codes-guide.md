# Error Codes Guide

This guide explains how to work with error codes in the application-sdk and provides a reference of all defined error codes.

## Error Code Structure

Error codes follow a 6-digit format:
- First 3 digits: Category code
- Last 3 digits: Specific error code within category

### Categories

| Category Code | Name | Description |
|--------------|------|-------------|
| 000 | System/Common | System-wide and common errors |
| 100 | Input/Output | File and data I/O related errors |
| 200 | SQL/Database | Database and SQL operation errors |
| 300 | Workflow/Activity | Workflow and activity execution errors |
| 400 | Client/Connection | Client and connection related errors |
| 500 | State/Storage | State management and storage errors |
| 600 | Server/API | Server and API related errors |

## How to Add a New Error Code

1. **Choose the appropriate category** from the ErrorCategory enum in `error_codes.py`:
```python
class ErrorCategory(Enum):
    SYSTEM = "000"
    IO = "100"
    SQL = "200"
    WORKFLOW = "300"
    CLIENT = "400"
    STATE = "500"
    SERVER = "600"
```

2. **Create a new error code** in the appropriate dictionary:
```python
CATEGORY_ERRORS = {
    "ERROR_NAME": ErrorCode(
        "category_code + sequential_number", 
        "Description of the error"
    ),
}
```

Example:
```python
SQL_ERRORS = {
    "SQL_AUTH_ERROR": ErrorCode("200001", "Error authenticating with SQL database"),
}
```

3. **Use the error code** in your logging:
```python
from application_sdk.common.error_codes import SQL_ERRORS

logger.error(
    f"Authentication failed: {str(exc)}",
    error_code=SQL_ERRORS["SQL_AUTH_ERROR"].code
)
```

## Current Error Codes

### System Errors (000)
| Code | Name | Description |
|------|------|-------------|
| 000001 | OTLP_SETUP_FAILED | Failed to setup OTLP logging |
| 000002 | OTLP_PARSE_FAILED | Failed to parse OTLP resource attributes |
| 000003 | LOG_PROCESSING_ERROR | Error processing log record |
| 000004 | QUERY_PREP_ERROR | Error preparing query |
| 000999 | UNKNOWN_ERROR | Unknown system error |

### Input/Output Errors (100)
| Code | Name | Description |
|------|------|-------------|
| 100001 | JSON_READ_ERROR | Error reading data from JSON |
| 100002 | JSON_BATCH_READ_ERROR | Error reading batched data from JSON |
| 100003 | JSON_DAFT_READ_ERROR | Error reading data from JSON using daft |
| 100004 | JSON_WRITE_ERROR | Error writing dataframe to JSON |
| 100005 | JSON_BATCH_WRITE_ERROR | Error writing batched dataframe to JSON |
| 100101 | PARQUET_READ_ERROR | Error reading data from parquet file(s) |
| 100102 | PARQUET_WRITE_ERROR | Error writing dataframe to parquet |
| 100103 | PARQUET_DAFT_WRITE_ERROR | Error writing daft dataframe to parquet |
| 100201 | ICEBERG_READ_ERROR | Error reading data from Iceberg table |
| 100202 | ICEBERG_DAFT_READ_ERROR | Error reading data from Iceberg table using daft |
| 100203 | ICEBERG_WRITE_ERROR | Error writing pandas dataframe to iceberg table |
| 100204 | ICEBERG_DAFT_WRITE_ERROR | Error writing daft dataframe to iceberg table |
| 100301 | OBJSTORE_READ_ERROR | Error reading file from object store |
| 100302 | OBJSTORE_WRITE_ERROR | Error writing file to object store |
| 100303 | OBJSTORE_DOWNLOAD_ERROR | Error downloading files from object store |

### SQL/Database Errors (200)
| Code | Name | Description |
|------|------|-------------|
| 200001 | SQL_AUTH_ERROR | Error authenticating with SQL database |
| 200002 | SQL_PREFLIGHT_CHECK_ERROR | Error during preflight check |
| 200003 | SQL_SCHEMA_CHECK_ERROR | Error during schema and database check |
| 200004 | SQL_TABLES_CHECK_ERROR | Error during tables check |
| 200005 | SQL_CLIENT_VERSION_ERROR | Error during client version check |
| 200006 | SQL_READ_ERROR | Error reading data from SQL |
| 200007 | SQL_BATCH_READ_ERROR | Error reading batched data from SQL |
| 200008 | SQL_DAFT_READ_ERROR | Error reading data from SQL using daft |
| 200009 | SQL_QUERY_EXEC_ERROR | Error executing query |
| 200010 | SQL_CONNECTION_ERROR | Error establishing database connection |
| 200011 | SQL_CLIENT_LOAD_ERROR | Error loading SQL client |
| 200012 | SQL_ENGINE_NOT_SET | SQL engine is not set |
| 200013 | SQL_CLIENT_NOT_INIT | SQL client or engine not initialized |

### Workflow/Activity Errors (300)
| Code | Name | Description |
|------|------|-------------|
| 300001 | WORKFLOW_EXEC_ERROR | Workflow execution failed |
| 300002 | WORKFLOW_TERMINATE_ERROR | Error terminating workflow |
| 300003 | WORKFLOW_STATUS_ERROR | Error getting workflow status |
| 300004 | WORKFLOW_MONITOR_ERROR | Error monitoring workflow |
| 300005 | ACTIVITY_STATE_ERROR | Error getting state |
| 300006 | ACTIVITY_PREFLIGHT_ERROR | Preflight check failed |
| 300007 | ACTIVITY_WORKFLOW_ID_ERROR | Failed to get workflow id |
| 300008 | ACTIVITY_PARALLEL_ERROR | Failed to parallelize queries |

### Client/Connection Errors (400)
| Code | Name | Description |
|------|------|-------------|
| 400001 | CLIENT_INIT_ERROR | Error initializing client |
| 400002 | CLIENT_CONNECTION_ERROR | Error establishing connection |
| 400003 | CLIENT_QUERY_ERROR | Error executing client query |
| 400004 | CLIENT_BATCH_ERROR | Error running query in batch |

### State/Storage Errors (500)
| Code | Name | Description |
|------|------|-------------|
| 500001 | STATE_STORE_ERROR | Failed to store state |
| 500002 | STATE_EXTRACT_ERROR | Failed to extract state |
| 500003 | STATE_CLEAN_ERROR | Failed to clean state |
| 500004 | STATS_GET_ERROR | Error getting statistics |
| 500005 | STATS_WRITE_ERROR | Error writing statistics |

### Server/API Errors (600)
| Code | Name | Description |
|------|------|-------------|
| 600001 | SERVER_TASK_CANCEL_ERROR | Error during task cancellation |
| 600002 | SERVER_MIDDLEWARE_ERROR | Error in server middleware |
| 600003 | SERVER_WORKER_ERROR | Error starting worker |
| 600004 | SERVER_DATA_GEN_ERROR | Error generating data |

## Best Practices

1. **Use Descriptive Names**: Error code names should be clear and descriptive
2. **Maintain Category Boundaries**: Keep errors in their appropriate categories
3. **Sequential Numbering**: Use sequential numbers within each category
4. **Document All Codes**: Always add new codes to this documentation
5. **Include Context**: When logging errors, include relevant context with the error code

## Example Usage

```python
from application_sdk.common.error_codes import SQL_ERRORS
from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)

try:
    # Some database operation
    result = db.execute(query)
except Exception as e:
    logger.error(
        f"Failed to execute query: {str(e)}",
        error_code=SQL_ERRORS["SQL_QUERY_EXEC_ERROR"].code
    )
``` 