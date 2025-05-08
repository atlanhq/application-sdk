"""
Error codes for the application-sdk.

This module defines standardized error codes used throughout the application-sdk.
Error codes are organized by category and follow the format:
- First 3 digits: Category code
- Last 3 digits: Specific error code within category

Categories:
- 000: System/Common errors
- 100: Input/Output errors
- 200: SQL/Database errors
- 300: Workflow/Activity errors
- 400: Client/Connection errors
- 500: State/Storage errors
- 600: Server/API errors
"""

from enum import Enum, auto
from typing import Dict, Optional


class ErrorCategory(Enum):
    """Categories of errors in the system."""
    SYSTEM = "000"
    IO = "100"
    SQL = "200"
    WORKFLOW = "300"
    CLIENT = "400"
    STATE = "500"
    SERVER = "600"

 
class ErrorCode:
    """Error code with category and description."""
    def __init__(self, code: str, description: str):
        self.code = code
        self.description = description

    def __str__(self) -> str:
        return f"{self.code}: {self.description}"


# System/Common Errors (000)
SYSTEM_ERRORS = {
    "OTLP_SETUP_FAILED": ErrorCode("000001", "Failed to setup OTLP logging"),
    "OTLP_PARSE_FAILED": ErrorCode("000002", "Failed to parse OTLP resource attributes"),
    "LOG_PROCESSING_ERROR": ErrorCode("000003", "Error processing log record"),
    "QUERY_PREP_ERROR": ErrorCode("000004", "Error preparing query"),
    "UNKNOWN_ERROR": ErrorCode("000999", "Unknown system error"),
}

# Input/Output Errors (100)
IO_ERRORS = {
    # JSON related errors
    "JSON_READ_ERROR": ErrorCode("100001", "Error reading data from JSON"),
    "JSON_BATCH_READ_ERROR": ErrorCode("100002", "Error reading batched data from JSON"),
    "JSON_DAFT_READ_ERROR": ErrorCode("100003", "Error reading data from JSON using daft"),
    "JSON_WRITE_ERROR": ErrorCode("100004", "Error writing dataframe to JSON"),
    "JSON_BATCH_WRITE_ERROR": ErrorCode("100005", "Error writing batched dataframe to JSON"),
    
    # Parquet related errors
    "PARQUET_READ_ERROR": ErrorCode("100101", "Error reading data from parquet file(s)"),
    "PARQUET_WRITE_ERROR": ErrorCode("100102", "Error writing dataframe to parquet"),
    "PARQUET_DAFT_WRITE_ERROR": ErrorCode("100103", "Error writing daft dataframe to parquet"),
    
    # Iceberg related errors
    "ICEBERG_READ_ERROR": ErrorCode("100201", "Error reading data from Iceberg table"),
    "ICEBERG_DAFT_READ_ERROR": ErrorCode("100202", "Error reading data from Iceberg table using daft"),
    "ICEBERG_WRITE_ERROR": ErrorCode("100203", "Error writing pandas dataframe to iceberg table"),
    "ICEBERG_DAFT_WRITE_ERROR": ErrorCode("100204", "Error writing daft dataframe to iceberg table"),
    
    # Object Store related errors
    "OBJSTORE_READ_ERROR": ErrorCode("100301", "Error reading file from object store"),
    "OBJSTORE_WRITE_ERROR": ErrorCode("100302", "Error writing file to object store"),
    "OBJSTORE_DOWNLOAD_ERROR": ErrorCode("100303", "Error downloading files from object store"),
}

# SQL/Database Errors (200)
SQL_ERRORS = {
    "SQL_METADATA_FETCH_ERROR": ErrorCode("200001", "Failed to fetch metadata"),
    "SQL_PREFLIGHT_CHECK_ERROR": ErrorCode("200002", "Error during preflight check"),
    "SQL_SCHEMA_CHECK_ERROR": ErrorCode("200003", "Error during schema and database check"),
    "SQL_TABLES_CHECK_ERROR": ErrorCode("200004", "Error during tables check"),
    "SQL_CLIENT_VERSION_ERROR": ErrorCode("200005", "Error during client version check"),
    "SQL_READ_ERROR": ErrorCode("200006", "Error reading data from SQL"),
    "SQL_BATCH_READ_ERROR": ErrorCode("200007", "Error reading batched data from SQL"),
    "SQL_DAFT_READ_ERROR": ErrorCode("200008", "Error reading data from SQL using daft"),
    "SQL_QUERY_EXEC_ERROR": ErrorCode("200009", "Error executing query"),
    "SQL_CONNECTION_ERROR": ErrorCode("200010", "Error establishing database connection"),
    "SQL_CLIENT_LOAD_ERROR": ErrorCode("200011", "Error loading SQL client"),
    "SQL_ENGINE_NOT_SET": ErrorCode("200012", "SQL engine is not set"),
    "SQL_CLIENT_NOT_INIT": ErrorCode("200013", "SQL client or engine not initialized"),
    "SQL_AUTH_ERROR": ErrorCode("200001", "Error authenticating with SQL database"),
}

# Workflow/Activity Errors (300)
WORKFLOW_ERRORS = {
    "WORKFLOW_EXEC_ERROR": ErrorCode("300001", "Workflow execution failed"),
    "WORKFLOW_TERMINATE_ERROR": ErrorCode("300002", "Error terminating workflow"),
    "WORKFLOW_STATUS_ERROR": ErrorCode("300003", "Error getting workflow status"),
    "WORKFLOW_MONITOR_ERROR": ErrorCode("300004", "Error monitoring workflow"),
    "ACTIVITY_STATE_ERROR": ErrorCode("300005", "Error getting state"),
    "ACTIVITY_PREFLIGHT_ERROR": ErrorCode("300006", "Preflight check failed"),
    "ACTIVITY_WORKFLOW_ID_ERROR": ErrorCode("300007", "Failed to get workflow id"),
    "ACTIVITY_PARALLEL_ERROR": ErrorCode("300008", "Failed to parallelize queries"),
}

# Client/Connection Errors (400)
CLIENT_ERRORS = {
    "CLIENT_INIT_ERROR": ErrorCode("400001", "Error initializing client"),
    "CLIENT_CONNECTION_ERROR": ErrorCode("400002", "Error establishing connection"),
    "CLIENT_QUERY_ERROR": ErrorCode("400003", "Error executing client query"),
    "CLIENT_BATCH_ERROR": ErrorCode("400004", "Error running query in batch"),
}

# State/Storage Errors (500)
STATE_ERRORS = {
    "STATE_STORE_ERROR": ErrorCode("500001", "Failed to store state"),
    "STATE_EXTRACT_ERROR": ErrorCode("500002", "Failed to extract state"),
    "STATE_CLEAN_ERROR": ErrorCode("500003", "Failed to clean state"),
    "STATS_GET_ERROR": ErrorCode("500004", "Error getting statistics"),
    "STATS_WRITE_ERROR": ErrorCode("500005", "Error writing statistics"),
}

# Server/API Errors (600)
SERVER_ERRORS = {
    "SERVER_TASK_CANCEL_ERROR": ErrorCode("600001", "Error during task cancellation"),
    "SERVER_MIDDLEWARE_ERROR": ErrorCode("600002", "Error in server middleware"),
    "SERVER_WORKER_ERROR": ErrorCode("600003", "Error starting worker"),
    "SERVER_DATA_GEN_ERROR": ErrorCode("600004", "Error generating data"),
}

# Combined dictionary of all error codes
ERROR_CODES: Dict[str, ErrorCode] = {
    **SYSTEM_ERRORS,
    **IO_ERRORS,
    **SQL_ERRORS,
    **WORKFLOW_ERRORS,
    **CLIENT_ERRORS,
    **STATE_ERRORS,
    **SERVER_ERRORS,
}