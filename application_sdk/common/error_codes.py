"""
Error codes for the application-sdk.

This module defines standardized error codes used throughout the application-sdk.
Error codes follow the format: Atlan-{Component}-{HTTP_Code}-{Unique_ID}

Components:
- Client: Client-related errors
- FastApi: Server and API errors
- Temporal: Workflow and activity errors
- IO: Input/Output errors
- Common: Common utility errors
- Docgen: Documentation generation errors
- TemporalActivity: Activity-specific errors
"""

from enum import Enum
from typing import Dict


class ErrorComponent(Enum):
    """Components that can generate errors in the system."""

    CLIENT = "Client"
    FASTAPI = "FastApi"
    TEMPORAL = "Temporal"
    TEMPORAL_WORKFLOW = "TemporalWorkflow"
    IO = "IO"
    COMMON = "Common"
    DOCGEN = "Docgen"
    TEMPORAL_ACTIVITY = "TemporalActivity"
    ATLAS_TRANSFORMER = "AtlasTransformer"


class ErrorCode:
    """Error code with component, HTTP code, and description."""

    def __init__(
        self, component: str, http_code: str, unique_id: str, description: str
    ):
        self.code = f"Atlan-{component}-{http_code}-{unique_id}"
        self.description = description

    def __str__(self) -> str:
        return f"{self.code}: {self.description}"


# Client Errors
CLIENT_ERRORS = {
    "REQUEST_VALIDATION_ERROR": ErrorCode(
        "Client", "403", "00", "Request validation failed"
    ),
    "INPUT_VALIDATION_ERROR": ErrorCode(
        "Client", "403", "01", "Input validation failed"
    ),
    "CLIENT_AUTH_ERROR": ErrorCode(
        "Client", "401", "00", "Client authentication failed"
    ),
    "HANDLER_AUTH_ERROR": ErrorCode(
        "Client", "401", "01", "Handler authentication failed"
    ),
    "SQL_CLIENT_AUTH_ERROR": ErrorCode(
        "Client", "401", "02", "SQL client authentication failed"
    ),
}

# FastApi/Server Errors
FASTAPI_ERRORS = {
    "SERVER_START_ERROR": ErrorCode("FastApi", "503", "00", "Server failed to start"),
    "SERVER_SHUTDOWN_ERROR": ErrorCode("FastApi", "503", "01", "Server shutdown error"),
    "SERVER_CONFIG_ERROR": ErrorCode(
        "FastApi", "500", "00", "Server configuration error"
    ),
    "CONFIGURATION_ERROR": ErrorCode(
        "FastApi", "500", "01", "General configuration error"
    ),
    "LOGGER_SETUP_ERROR": ErrorCode("FastApi", "500", "02", "Logger setup failed"),
    "LOGGER_PROCESSING_ERROR": ErrorCode(
        "FastApi", "500", "03", "Error processing log record"
    ),
    "LOGGER_OTLP_ERROR": ErrorCode("FastApi", "500", "04", "OTLP logging error"),
    "LOGGER_RESOURCE_ERROR": ErrorCode("FastApi", "500", "05", "Logger resource error"),
    "UNKNOWN_ERROR": ErrorCode("FastApi", "500", "06", "Unknown system error"),
    "SQL_FILE_ERROR": ErrorCode("FastApi", "500", "07", "SQL file error"),
    "ENDPOINT_ERROR": ErrorCode("FastApi", "500", "08", "Endpoint error"),
    "EVENT_TRIGGER_ERROR": ErrorCode("FastApi", "500", "09", "Event trigger error"),
    "MIDDLEWARE_ERROR": ErrorCode("FastApi", "500", "10", "Middleware error"),
    "ROUTE_HANDLER_ERROR": ErrorCode("FastApi", "500", "11", "Route handler error"),
    "LOG_MIDDLEWARE_ERROR": ErrorCode("FastApi", "500", "12", "Log middleware error"),
}

# Temporal Errors
TEMPORAL_ERRORS = {
    "TEMPORAL_CLIENT_CONNECTION_ERROR": ErrorCode(
        "Temporal", "403", "00", "Temporal client connection error"
    ),
    "TEMPORAL_CLIENT_ACTIVITY_ERROR": ErrorCode(
        "Temporal", "500", "00", "Temporal client activity error"
    ),
    "TEMPORAL_CLIENT_WORKER_ERROR": ErrorCode(
        "Temporal", "500", "01", "Temporal client worker error"
    ),
}

# Temporal Workflow Errors
TEMPORAL_WORKFLOW_ERRORS = {
    "WORKFLOW_EXECUTION_ERROR": ErrorCode(
        "TemporalWorkflow", "500", "02", "Workflow execution error"
    ),
    "WORKFLOW_CONFIG_ERROR": ErrorCode(
        "TemporalWorkflow", "400", "00", "Workflow configuration error"
    ),
    "WORKFLOW_VALIDATION_ERROR": ErrorCode(
        "TemporalWorkflow", "422", "00", "Workflow validation error"
    ),
    "WORKFLOW_CLIENT_START_ERROR": ErrorCode(
        "TemporalWorkflow", "500", "03", "Workflow client start error"
    ),
    "WORKFLOW_CLIENT_STOP_ERROR": ErrorCode(
        "TemporalWorkflow", "500", "04", "Workflow client stop error"
    ),
    "WORKFLOW_CLIENT_STATUS_ERROR": ErrorCode(
        "TemporalWorkflow", "500", "05", "Workflow client status error"
    ),
    "WORKFLOW_CLIENT_WORKER_ERROR": ErrorCode(
        "TemporalWorkflow", "500", "06", "Workflow client worker error"
    ),
    "WORKFLOW_CLIENT_NOT_FOUND_ERROR": ErrorCode(
        "TemporalWorkflow", "404", "00", "Workflow client not found"
    ),
}

# IO Errors
IO_ERRORS = {
    "INPUT_ERROR": ErrorCode("IO", "400", "00", "Input error"),
    "INPUT_LOAD_ERROR": ErrorCode("IO", "500", "00", "Input load error"),
    "INPUT_PROCESSING_ERROR": ErrorCode("IO", "500", "01", "Input processing error"),
    "SQL_QUERY_ERROR": ErrorCode("IO", "400", "00", "SQL query error"),
    "SQL_QUERY_BATCH_ERROR": ErrorCode("IO", "500", "02", "SQL query batch error"),
    "SQL_QUERY_PANDAS_ERROR": ErrorCode("IO", "500", "03", "SQL query pandas error"),
    "SQL_QUERY_DAFT_ERROR": ErrorCode("IO", "500", "04", "SQL query daft error"),
    "JSON_READ_ERROR": ErrorCode("IO", "500", "05", "JSON read error"),
    "JSON_BATCH_ERROR": ErrorCode("IO", "500", "06", "JSON batch error"),
    "JSON_DAFT_ERROR": ErrorCode("IO", "500", "07", "JSON daft error"),
    "JSON_DOWNLOAD_ERROR": ErrorCode("IO", "500", "08", "JSON download error"),
    "PARQUET_READ_ERROR": ErrorCode("IO", "500", "09", "Parquet read error"),
    "PARQUET_BATCH_ERROR": ErrorCode("IO", "500", "10", "Parquet batch error"),
    "PARQUET_DAFT_ERROR": ErrorCode("IO", "500", "11", "Parquet daft error"),
    "PARQUET_VALIDATION_ERROR": ErrorCode(
        "IO", "500", "12", "Parquet validation error"
    ),
    "ICEBERG_READ_ERROR": ErrorCode("IO", "500", "13", "Iceberg read error"),
    "ICEBERG_DAFT_ERROR": ErrorCode("IO", "500", "14", "Iceberg daft error"),
    "ICEBERG_TABLE_ERROR": ErrorCode("IO", "500", "15", "Iceberg table error"),
    "OBJECT_STORE_ERROR": ErrorCode("IO", "500", "16", "Object store error"),
    "OBJECT_STORE_DOWNLOAD_ERROR": ErrorCode(
        "IO", "503", "00", "Object store download error"
    ),
    "OBJECT_STORE_READ_ERROR": ErrorCode("IO", "503", "01", "Object store read error"),
    "STATE_STORE_ERROR": ErrorCode("IO", "500", "17", "State store error"),
    "STATE_STORE_EXTRACT_ERROR": ErrorCode(
        "IO", "500", "18", "State store extract error"
    ),
    "STATE_STORE_VALIDATION_ERROR": ErrorCode(
        "IO", "500", "19", "State store validation error"
    ),
    "OUTPUT_ERROR": ErrorCode("IO", "500", "20", "Output error"),
    "OUTPUT_WRITE_ERROR": ErrorCode("IO", "500", "21", "Output write error"),
    "OUTPUT_STATISTICS_ERROR": ErrorCode("IO", "500", "22", "Output statistics error"),
    "OUTPUT_VALIDATION_ERROR": ErrorCode("IO", "500", "23", "Output validation error"),
    "JSON_WRITE_ERROR": ErrorCode("IO", "500", "24", "JSON write error"),
    "JSON_BATCH_WRITE_ERROR": ErrorCode("IO", "500", "25", "JSON batch write error"),
    "JSON_DAFT_WRITE_ERROR": ErrorCode("IO", "500", "26", "JSON daft write error"),
    "PARQUET_WRITE_ERROR": ErrorCode("IO", "500", "27", "Parquet write error"),
    "PARQUET_DAFT_WRITE_ERROR": ErrorCode(
        "IO", "500", "28", "Parquet daft write error"
    ),
    "ICEBERG_WRITE_ERROR": ErrorCode("IO", "500", "30", "Iceberg write error"),
    "ICEBERG_DAFT_WRITE_ERROR": ErrorCode(
        "IO", "500", "31", "Iceberg daft write error"
    ),
    "ICEBERG_TABLE_ERROR_OUT": ErrorCode(
        "IO", "500", "32", "Iceberg table output error"
    ),
    "OBJECT_STORE_WRITE_ERROR": ErrorCode(
        "IO", "500", "33", "Object store write error"
    ),
    "STATE_STORE_WRITE_ERROR": ErrorCode("IO", "500", "34", "State store write error"),
}

# Common Utility Errors
COMMON_ERRORS = {
    "AWS_REGION_ERROR": ErrorCode("AWS", "400", "00", "AWS region error"),
    "AWS_ROLE_ERROR": ErrorCode("AWS", "401", "00", "AWS role error"),
    "AWS_CREDENTIALS_ERROR": ErrorCode("AWS", "401", "01", "AWS credentials error"),
    "AWS_TOKEN_ERROR": ErrorCode("AWS", "401", "02", "AWS token error"),
    "QUERY_PREPARATION_ERROR": ErrorCode("AWS", "400", "01", "Query preparation error"),
    "FILTER_PREPARATION_ERROR": ErrorCode(
        "Common", "400", "00", "Filter preparation error"
    ),
    "CREDENTIALS_PARSE_ERROR": ErrorCode(
        "Common", "400", "01", "Credentials parse error"
    ),
}

# DocGen Errors
DOCGEN_ERRORS = {
    "DOCGEN_ERROR": ErrorCode("Docgen", "500", "00", "Documentation generation error"),
    "DOCGEN_EXPORT_ERROR": ErrorCode(
        "Docgen", "500", "01", "Documentation export error"
    ),
    "DOCGEN_BUILD_ERROR": ErrorCode("Docgen", "500", "02", "Documentation build error"),
    "MANIFEST_NOT_FOUND_ERROR": ErrorCode("Docgen", "404", "00", "Manifest not found"),
    "MANIFEST_PARSE_ERROR": ErrorCode("Docgen", "500", "03", "Manifest parse error"),
    "MANIFEST_VALIDATION_ERROR": ErrorCode(
        "Docgen", "500", "04", "Manifest validation error"
    ),
    "MANIFEST_YAML_ERROR": ErrorCode("Docgen", "422", "00", "Manifest YAML error"),
    "DIRECTORY_VALIDATION_ERROR": ErrorCode(
        "Docgen", "500", "05", "Directory validation error"
    ),
    "DIRECTORY_CONTENT_ERROR": ErrorCode(
        "Docgen", "422", "01", "Directory content error"
    ),
    "DIRECTORY_STRUCTURE_ERROR": ErrorCode(
        "Docgen", "422", "02", "Directory structure error"
    ),
    "DIRECTORY_FILE_ERROR": ErrorCode("Docgen", "422", "03", "Directory file error"),
    "MKDOCS_CONFIG_ERROR": ErrorCode(
        "Docgen", "422", "04", "MkDocs configuration error"
    ),
    "MKDOCS_EXPORT_ERROR": ErrorCode("Docgen", "500", "06", "MkDocs export error"),
    "MKDOCS_NAV_ERROR": ErrorCode("Docgen", "500", "07", "MkDocs navigation error"),
    "MKDOCS_BUILD_ERROR": ErrorCode("Docgen", "500", "08", "MkDocs build error"),
}

# Activity Errors
ACTIVITY_ERRORS = {
    "ACTIVITY_START_ERROR": ErrorCode(
        "TemporalActivity", "503", "00", "Activity start error"
    ),
    "ACTIVITY_END_ERROR": ErrorCode(
        "TemporalActivity", "500", "00", "Activity end error"
    ),
    "QUERY_EXTRACTION_ERROR": ErrorCode(
        "TemporalActivity", "500", "01", "Query extraction error"
    ),
    "QUERY_EXTRACTION_SQL_ERROR": ErrorCode(
        "TemporalActivity", "500", "02", "Query extraction SQL error"
    ),
    "QUERY_EXTRACTION_PARSE_ERROR": ErrorCode(
        "TemporalActivity", "500", "03", "Query extraction parse error"
    ),
    "QUERY_EXTRACTION_VALIDATION_ERROR": ErrorCode(
        "TemporalActivity", "422", "00", "Query extraction validation error"
    ),
    "METADATA_EXTRACTION_ERROR": ErrorCode(
        "TemporalActivity", "500", "04", "Metadata extraction error"
    ),
    "METADATA_EXTRACTION_SQL_ERROR": ErrorCode(
        "TemporalActivity", "500", "05", "Metadata extraction SQL error"
    ),
    "METADATA_EXTRACTION_REST_ERROR": ErrorCode(
        "TemporalActivity", "500", "06", "Metadata extraction REST error"
    ),
    "METADATA_EXTRACTION_PARSE_ERROR": ErrorCode(
        "TemporalActivity", "500", "07", "Metadata extraction parse error"
    ),
    "METADATA_EXTRACTION_VALIDATION_ERROR": ErrorCode(
        "TemporalActivity", "422", "01", "Metadata extraction validation error"
    ),
}

# Combined dictionary of all error codes
ERROR_CODES: Dict[str, ErrorCode] = {
    **CLIENT_ERRORS,
    **FASTAPI_ERRORS,
    **TEMPORAL_ERRORS,
    **TEMPORAL_WORKFLOW_ERRORS,
    **IO_ERRORS,
    **COMMON_ERRORS,
    **DOCGEN_ERRORS,
    **ACTIVITY_ERRORS,
}
