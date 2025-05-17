# Error Codes Guide

This guide explains how to work with error codes in the application-sdk and provides a reference of all defined error codes.

## Error Code Structure

Error codes follow the format: `Atlan-{Component}-{HTTP_Code}-{Unique_ID}`

### Components

| Component | Description |
|-----------|-------------|
| Client | Client-side errors (400 series) |
| Internal | Internal server errors (500 series) |
| Temporal | Workflow and activity errors |
| IO | Input/Output related errors |
| Common | Common utility errors |
| DocGen | Documentation generation errors |
| Activity | Activity-specific errors |

## How to Add a New Error Code

1. **Choose the appropriate component** from the ErrorComponent enum in `error_codes.py`:
```python
class ErrorComponent(Enum):
    CLIENT = "Client"
    INTERNAL = "Internal"
    TEMPORAL = "Temporal"
    IO = "IO"
    COMMON = "Common"
    DOCGEN = "DocGen"
    ACTIVITY = "Activity"
```

2. **Create a new error code** in the appropriate dictionary:
```python
COMPONENT_ERRORS = {
    "ERROR_NAME": ErrorCode(
        component="Component",
        http_code="HTTP_CODE",
        unique_id="UNIQUE_ID",
        description="Description of the error"
    ),
}
```

Example:
```python
CLIENT_ERRORS = {
    "REQUEST_VALIDATION_ERROR": ErrorCode(
        "Client",
        "403",
        "00",
        "Request validation failed"
    ),
}
```

3. **Use the error code** in your logging:
```python
from application_sdk.common.error_codes import CLIENT_ERRORS

logger.error(
    f"Request validation failed: {str(exc)}",
    error_code=CLIENT_ERRORS["REQUEST_VALIDATION_ERROR"].code
)
```

## Current Error Codes

### Client Errors (400)
| Code | Name | Description |
|------|------|-------------|
| Atlan-Client-403-00 | REQUEST_VALIDATION_ERROR | Request validation failed |
| Atlan-Client-403-01 | INVALID_CREDENTIALS | Invalid credentials provided |
| Atlan-Client-403-02 | INVALID_PERMISSIONS | Invalid permissions for operation |
| Atlan-Client-403-03 | INVALID_REQUEST | Invalid request parameters |
| Atlan-Client-403-04 | INVALID_CONFIGURATION | Invalid configuration provided |
| Atlan-Client-403-05 | INVALID_WORKFLOW | Invalid workflow configuration |
| Atlan-Client-403-06 | INVALID_ACTIVITY | Invalid activity configuration |
| Atlan-Client-403-07 | INVALID_STATE | Invalid state configuration |
| Atlan-Client-403-08 | INVALID_STORAGE | Invalid storage configuration |
| Atlan-Client-403-09 | INVALID_CONNECTION | Invalid connection configuration |
| Atlan-Client-403-10 | INVALID_QUERY | Invalid query parameters |
| Atlan-Client-403-11 | INVALID_FILTER | Invalid filter parameters |
| Atlan-Client-403-12 | INVALID_SORT | Invalid sort parameters |
| Atlan-Client-403-13 | INVALID_PAGINATION | Invalid pagination parameters |
| Atlan-Client-403-14 | INVALID_FORMAT | Invalid format parameters |
| Atlan-Client-403-15 | INVALID_VERSION | Invalid version parameters |
| Atlan-Client-403-16 | INVALID_TENANT | Invalid tenant parameters |
| Atlan-Client-403-17 | INVALID_USER | Invalid user parameters |
| Atlan-Client-403-18 | INVALID_ROLE | Invalid role parameters |
| Atlan-Client-403-19 | INVALID_TOKEN | Invalid token parameters |
| Atlan-Client-403-20 | INVALID_SESSION | Invalid session parameters |
| Atlan-Client-403-21 | INVALID_AUTH | Invalid authentication parameters |
| Atlan-Client-403-22 | INVALID_HEADER | Invalid header parameters |
| Atlan-Client-403-23 | INVALID_BODY | Invalid body parameters |
| Atlan-Client-403-24 | INVALID_PARAM | Invalid parameter provided |
| Atlan-Client-403-25 | INVALID_PATH | Invalid path provided |
| Atlan-Client-403-26 | INVALID_QUERY_STRING | Invalid query string provided |
| Atlan-Client-403-27 | INVALID_FRAGMENT | Invalid fragment provided |
| Atlan-Client-403-28 | INVALID_METHOD | Invalid method provided |
| Atlan-Client-403-29 | INVALID_PROTOCOL | Invalid protocol provided |
| Atlan-Client-403-30 | INVALID_HOST | Invalid host provided |
| Atlan-Client-403-31 | INVALID_PORT | Invalid port provided |
| Atlan-Client-403-32 | INVALID_URI | Invalid URI provided |
| Atlan-Client-403-33 | INVALID_URL | Invalid URL provided |
| Atlan-Client-403-34 | INVALID_ENDPOINT | Invalid endpoint provided |
| Atlan-Client-403-35 | INVALID_RESOURCE | Invalid resource provided |
| Atlan-Client-403-36 | INVALID_OPERATION | Invalid operation provided |
| Atlan-Client-403-37 | INVALID_ACTION | Invalid action provided |
| Atlan-Client-403-38 | INVALID_EVENT | Invalid event provided |
| Atlan-Client-403-39 | INVALID_NOTIFICATION | Invalid notification provided |
| Atlan-Client-403-40 | INVALID_ALERT | Invalid alert provided |
| Atlan-Client-403-41 | INVALID_METRIC | Invalid metric provided |
| Atlan-Client-403-42 | INVALID_LOG | Invalid log provided |
| Atlan-Client-403-43 | INVALID_TRACE | Invalid trace provided |
| Atlan-Client-403-44 | INVALID_SPAN | Invalid span provided |
| Atlan-Client-403-45 | INVALID_BAGGAGE | Invalid baggage provided |
| Atlan-Client-403-46 | INVALID_PROPAGATION | Invalid propagation provided |
| Atlan-Client-403-47 | INVALID_SAMPLING | Invalid sampling provided |
| Atlan-Client-403-48 | INVALID_EXPORT | Invalid export provided |
| Atlan-Client-403-49 | INVALID_PROCESSOR | Invalid processor provided |
| Atlan-Client-403-50 | INVALID_EXPORTER | Invalid exporter provided |

### Internal Errors (500)
| Code | Name | Description |
|------|------|-------------|
| Atlan-Internal-500-00 | INTERNAL_ERROR | Internal server error |
| Atlan-Internal-500-01 | DATABASE_ERROR | Database operation failed |
| Atlan-Internal-500-02 | CACHE_ERROR | Cache operation failed |
| Atlan-Internal-500-03 | QUEUE_ERROR | Queue operation failed |
| Atlan-Internal-500-04 | STORAGE_ERROR | Storage operation failed |
| Atlan-Internal-500-05 | NETWORK_ERROR | Network operation failed |
| Atlan-Internal-500-06 | AUTH_ERROR | Authentication operation failed |
| Atlan-Internal-500-07 | ENCRYPTION_ERROR | Encryption operation failed |
| Atlan-Internal-500-08 | DECRYPTION_ERROR | Decryption operation failed |
| Atlan-Internal-500-09 | COMPRESSION_ERROR | Compression operation failed |
| Atlan-Internal-500-10 | DECOMPRESSION_ERROR | Decompression operation failed |
| Atlan-Internal-500-11 | SERIALIZATION_ERROR | Serialization operation failed |
| Atlan-Internal-500-12 | DESERIALIZATION_ERROR | Deserialization operation failed |
| Atlan-Internal-500-13 | VALIDATION_ERROR | Validation operation failed |
| Atlan-Internal-500-14 | TRANSFORMATION_ERROR | Transformation operation failed |
| Atlan-Internal-500-15 | AGGREGATION_ERROR | Aggregation operation failed |
| Atlan-Internal-500-16 | FILTERING_ERROR | Filtering operation failed |
| Atlan-Internal-500-17 | SORTING_ERROR | Sorting operation failed |
| Atlan-Internal-500-18 | PAGINATION_ERROR | Pagination operation failed |
| Atlan-Internal-500-19 | FORMATTING_ERROR | Formatting operation failed |
| Atlan-Internal-500-20 | VERSIONING_ERROR | Versioning operation failed |
| Atlan-Internal-500-21 | TENANCY_ERROR | Tenancy operation failed |
| Atlan-Internal-500-22 | USER_ERROR | User operation failed |
| Atlan-Internal-500-23 | ROLE_ERROR | Role operation failed |
| Atlan-Internal-500-24 | TOKEN_ERROR | Token operation failed |
| Atlan-Internal-500-25 | SESSION_ERROR | Session operation failed |
| Atlan-Internal-500-26 | AUTHENTICATION_ERROR | Authentication operation failed |
| Atlan-Internal-500-27 | AUTHORIZATION_ERROR | Authorization operation failed |
| Atlan-Internal-500-28 | RATE_LIMITING_ERROR | Rate limiting operation failed |
| Atlan-Internal-500-29 | THROTTLING_ERROR | Throttling operation failed |
| Atlan-Internal-500-30 | CIRCUIT_BREAKER_ERROR | Circuit breaker operation failed |
| Atlan-Internal-500-31 | FALLBACK_ERROR | Fallback operation failed |
| Atlan-Internal-500-32 | RETRY_ERROR | Retry operation failed |
| Atlan-Internal-500-33 | TIMEOUT_ERROR | Timeout operation failed |
| Atlan-Internal-500-34 | DEADLINE_ERROR | Deadline operation failed |
| Atlan-Internal-500-35 | CANCELLATION_ERROR | Cancellation operation failed |
| Atlan-Internal-500-36 | TERMINATION_ERROR | Termination operation failed |
| Atlan-Internal-500-37 | SHUTDOWN_ERROR | Shutdown operation failed |
| Atlan-Internal-500-38 | STARTUP_ERROR | Startup operation failed |
| Atlan-Internal-500-39 | INITIALIZATION_ERROR | Initialization operation failed |
| Atlan-Internal-500-40 | CONFIGURATION_ERROR | Configuration operation failed |
| Atlan-Internal-500-41 | DEPLOYMENT_ERROR | Deployment operation failed |
| Atlan-Internal-500-42 | SCALING_ERROR | Scaling operation failed |
| Atlan-Internal-500-43 | LOAD_BALANCING_ERROR | Load balancing operation failed |
| Atlan-Internal-500-44 | SERVICE_DISCOVERY_ERROR | Service discovery operation failed |
| Atlan-Internal-500-45 | HEALTH_CHECK_ERROR | Health check operation failed |
| Atlan-Internal-500-46 | METRICS_ERROR | Metrics operation failed |
| Atlan-Internal-500-47 | LOGGING_ERROR | Logging operation failed |
| Atlan-Internal-500-48 | TRACING_ERROR | Tracing operation failed |
| Atlan-Internal-500-49 | MONITORING_ERROR | Monitoring operation failed |
| Atlan-Internal-500-50 | ALERTING_ERROR | Alerting operation failed |

### Temporal Errors
| Code | Name | Description |
|------|------|-------------|
| Atlan-Temporal-500-00 | WORKFLOW_EXECUTION_ERROR | Workflow execution failed |
| Atlan-Temporal-500-01 | WORKFLOW_CLIENT_STOP_ERROR | Error stopping workflow client |
| Atlan-Temporal-500-02 | WORKFLOW_CLIENT_STATUS_ERROR | Error getting workflow client status |
| Atlan-Temporal-500-03 | WORKFLOW_CLIENT_MONITOR_ERROR | Error monitoring workflow client |
| Atlan-Temporal-500-04 | ACTIVITY_STATE_ERROR | Error getting activity state |
| Atlan-Temporal-500-05 | ACTIVITY_PREFLIGHT_ERROR | Activity preflight check failed |
| Atlan-Temporal-500-06 | ACTIVITY_WORKFLOW_ID_ERROR | Failed to get workflow ID |
| Atlan-Temporal-500-07 | ACTIVITY_PARALLEL_ERROR | Failed to parallelize queries |

### IO Errors
| Code | Name | Description |
|------|------|-------------|
| Atlan-IO-500-00 | OBJECT_STORE_ERROR | Object store operation failed |
| Atlan-IO-500-01 | OBJECT_STORE_READ_ERROR | Error reading from object store |
| Atlan-IO-500-02 | OBJECT_STORE_WRITE_ERROR | Error writing to object store |
| Atlan-IO-500-03 | OBJECT_STORE_DOWNLOAD_ERROR | Error downloading from object store |
| Atlan-IO-500-04 | JSON_READ_ERROR | Error reading JSON data |
| Atlan-IO-500-05 | JSON_WRITE_ERROR | Error writing JSON data |
| Atlan-IO-500-06 | JSON_BATCH_READ_ERROR | Error reading batched JSON data |
| Atlan-IO-500-07 | JSON_BATCH_WRITE_ERROR | Error writing batched JSON data |
| Atlan-IO-500-08 | JSON_DAFT_READ_ERROR | Error reading JSON data using daft |
| Atlan-IO-500-09 | JSON_DAFT_WRITE_ERROR | Error writing JSON data using daft |
| Atlan-IO-500-10 | PARQUET_READ_ERROR | Error reading parquet data |
| Atlan-IO-500-11 | PARQUET_WRITE_ERROR | Error writing parquet data |
| Atlan-IO-500-12 | PARQUET_DAFT_WRITE_ERROR | Error writing parquet data using daft |
| Atlan-IO-500-13 | ICEBERG_READ_ERROR | Error reading Iceberg data |
| Atlan-IO-500-14 | ICEBERG_WRITE_ERROR | Error writing Iceberg data |
| Atlan-IO-500-15 | ICEBERG_DAFT_READ_ERROR | Error reading Iceberg data using daft |
| Atlan-IO-500-16 | ICEBERG_DAFT_WRITE_ERROR | Error writing Iceberg data using daft |
| Atlan-IO-500-17 | STATE_STORE_ERROR | State store operation failed |
| Atlan-IO-500-18 | STATE_STORE_EXTRACT_ERROR | Error extracting state from store |

### Common Utility Errors
| Code | Name | Description |
|------|------|-------------|
| Atlan-Common-500-00 | UTILITY_ERROR | Common utility operation failed |
| Atlan-Common-500-01 | VALIDATION_ERROR | Validation operation failed |
| Atlan-Common-500-02 | TRANSFORMATION_ERROR | Transformation operation failed |
| Atlan-Common-500-03 | AGGREGATION_ERROR | Aggregation operation failed |
| Atlan-Common-500-04 | FILTERING_ERROR | Filtering operation failed |
| Atlan-Common-500-05 | SORTING_ERROR | Sorting operation failed |
| Atlan-Common-500-06 | PAGINATION_ERROR | Pagination operation failed |
| Atlan-Common-500-07 | FORMATTING_ERROR | Formatting operation failed |
| Atlan-Common-500-08 | VERSIONING_ERROR | Versioning operation failed |
| Atlan-Common-500-09 | TENANCY_ERROR | Tenancy operation failed |
| Atlan-Common-500-10 | USER_ERROR | User operation failed |

### DocGen Errors
| Code | Name | Description |
|------|------|-------------|
| Atlan-DocGen-500-00 | DOCGEN_ERROR | Documentation generation failed |
| Atlan-DocGen-500-01 | MARKDOWN_ERROR | Markdown generation failed |
| Atlan-DocGen-500-02 | HTML_ERROR | HTML generation failed |
| Atlan-DocGen-500-03 | PDF_ERROR | PDF generation failed |
| Atlan-DocGen-500-04 | IMAGE_ERROR | Image generation failed |
| Atlan-DocGen-500-05 | TABLE_ERROR | Table generation failed |
| Atlan-DocGen-500-06 | CODE_ERROR | Code generation failed |
| Atlan-DocGen-500-07 | DIAGRAM_ERROR | Diagram generation failed |
| Atlan-DocGen-500-08 | CHART_ERROR | Chart generation failed |
| Atlan-DocGen-500-09 | GRAPH_ERROR | Graph generation failed |
| Atlan-DocGen-500-10 | MAP_ERROR | Map generation failed |

### Activity Errors
| Code | Name | Description |
|------|------|-------------|
| Atlan-Activity-500-00 | ACTIVITY_ERROR | Activity operation failed |
| Atlan-Activity-500-01 | ACTIVITY_STATE_ERROR | Activity state operation failed |
| Atlan-Activity-500-02 | ACTIVITY_PREFLIGHT_ERROR | Activity preflight operation failed |
| Atlan-Activity-500-03 | ACTIVITY_WORKFLOW_ID_ERROR | Activity workflow ID operation failed |
| Atlan-Activity-500-04 | ACTIVITY_PARALLEL_ERROR | Activity parallel operation failed |
| Atlan-Activity-500-05 | ACTIVITY_SERIALIZATION_ERROR | Activity serialization operation failed |
| Atlan-Activity-500-06 | ACTIVITY_DESERIALIZATION_ERROR | Activity deserialization operation failed |
| Atlan-Activity-500-07 | ACTIVITY_VALIDATION_ERROR | Activity validation operation failed |
| Atlan-Activity-500-08 | ACTIVITY_TRANSFORMATION_ERROR | Activity transformation operation failed |
| Atlan-Activity-500-09 | ACTIVITY_AGGREGATION_ERROR | Activity aggregation operation failed |
| Atlan-Activity-500-10 | ACTIVITY_FILTERING_ERROR | Activity filtering operation failed |

## Best Practices

1. **Use Descriptive Names**: Error code names should be clear and descriptive
2. **Maintain Component Boundaries**: Keep errors in their appropriate components
3. **Follow HTTP Status Codes**: Use appropriate HTTP status codes in error codes
4. **Document All Codes**: Always add new codes to this documentation
5. **Include Context**: When logging errors, include relevant context with the error code

## Example Usage

```python
from application_sdk.common.error_codes import CLIENT_ERRORS
from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)

try:
    # Some operation
    result = process_request(request)
except Exception as e:
    logger.error(
        f"Request validation failed: {str(e)}",
        error_code=CLIENT_ERRORS["REQUEST_VALIDATION_ERROR"].code
    )
```