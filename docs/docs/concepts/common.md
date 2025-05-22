# Common Utilities

This section describes various utility functions and classes found within the `application_sdk.common` package. These utilities provide foundational functionalities used across different parts of the SDK, such as logging, configuration management, interacting with AWS, and general helper functions.

## Error Handling (`error_codes.py`)

The SDK provides a comprehensive error handling system with standardized error codes and categories.

### Key Concepts

*   **`ErrorComponent`**: Enum defining the components that can generate errors in the system:
    *   `CLIENT`: Client-related errors
    *   `API`: Server and API errors
    *   `ORCHESTRATOR`: Workflow and activity errors
    *   `WORKFLOW`: Workflow-specific errors
    *   `IO`: Input/Output errors
    *   `COMMON`: Common utility errors
    *   `DOCGEN`: Documentation generation errors
    *   `ACTIVITY`: Activity-specific errors
    *   `ATLAS_TRANSFORMER`: Atlas transformer errors

*   **`ErrorCode`**: Class representing an error code with component, HTTP code, and description:
    ```python
    class ErrorCode:
        def __init__(self, component: str, http_code: str, unique_id: str, description: str):
            self.code = f"Atlan-{component}-{http_code}-{unique_id}".upper()
            self.description = description
    ```

*   **`AtlanError`**: Base exception class for all Atlan errors.

*   **Error Categories**:
    *   `ClientError`: Client-related errors (400-499)
    *   `ApiError`: Server and API errors (500-599)
    *   `OrchestratorError`: Workflow and activity errors
    *   `WorkflowError`: Workflow-specific errors
    *   `IOError`: Input/Output errors
    *   `CommonError`: Common utility errors
    *   `DocGenError`: Documentation generation errors
    *   `ActivityError`: Activity-specific errors

### Usage

```python
from application_sdk.common.error_codes import ClientError, FastApiError, ActivityError

# Client errors
try:
    # Some operation
    pass
except ClientError as e:
    raise ClientError.REQUEST_VALIDATION_ERROR

# Server errors
try:
    # Server operation
    pass
except ApiError as e:
    raise ApiError.SERVER_START_ERROR

# Activity errors
try:
    # Activity operation
    pass
except ActivityError as e:
    raise ActivityError.ACTIVITY_START_ERROR
```

### Error Code Format

Error codes follow the format: `Atlan-{Component}-{HTTP_Code}-{Unique_ID}`

Example: `ATLAN-CLIENT-403-00` for a request validation error

### Error Categories and HTTP Codes

1. **Client Errors (400-499)**:
    *   `REQUEST_VALIDATION_ERROR` (403-00)
    *   `INPUT_VALIDATION_ERROR` (403-01)
    *   `CLIENT_AUTH_ERROR` (401-00)
    *   `HANDLER_AUTH_ERROR` (401-01)
    *   `SQL_CLIENT_AUTH_ERROR` (401-02)

2. **Server Errors (500-599)**:
    *   `SERVER_START_ERROR` (503-00)
    *   `SERVER_SHUTDOWN_ERROR` (503-01)
    *   `SERVER_CONFIG_ERROR` (500-00)
    *   `CONFIGURATION_ERROR` (500-01)
    *   `LOGGER_SETUP_ERROR` (500-02)
    *   And many more...

3. **Activity Errors**:
    *   `ACTIVITY_START_ERROR` (503-00)
    *   `ACTIVITY_END_ERROR` (500-00)
    *   `QUERY_EXTRACTION_ERROR` (500-01)
    *   And more...

### Best Practices

1. **Error Handling**:
    *   Use appropriate error categories for different types of errors
    *   Include error codes in logs for better tracking
    *   Map error codes to appropriate HTTP status codes
    *   Include stack traces for debugging
    *   Mask sensitive data in error messages

2. **Logging Errors**:
    ```python
    from application_sdk.common.logger_adaptors import get_logger
    from application_sdk.common.error_codes import ClientError

    logger = get_logger(__name__)

    try:
        # Some operation
        pass
    except Exception as e:
        logger.error(
            f"Operation failed: {ClientError.REQUEST_VALIDATION_ERROR}",
            exc_info=True,
            extra={"error_code": ClientError.REQUEST_VALIDATION_ERROR.code}
        )
        raise ClientError.REQUEST_VALIDATION_ERROR
    ```

## Logging (`logger_adaptors.py`)

The SDK uses the `loguru` library for enhanced logging capabilities, combined with standard Python logging and OpenTelemetry (OTLP) integration for structured, observable logs.

### Key Concepts

*   **`InterceptHandler`**: A standard `logging.Handler` that intercepts logs from standard Python logging (including libraries like `boto3`) and redirects them through `loguru`, ensuring consistent formatting and handling.
*   **`AtlanObservability`**: A superclass responsible for managing log retention, logs batching and parquet sink operations.
*   **`AtlanLoggerAdapter`**: The main interface for logging within the SDK. It wraps `loguru`, configures standard output format (including colors), handles OTLP exporter setup, and automatically enriches log messages with context.
    *   **Context Enrichment**: Automatically includes details from the current Temporal Workflow or Activity context (like `workflow_id`, `run_id`, `activity_id`, `attempt`, etc.) and FastAPI request context (`request_id`) if available.
    *   **OTLP Integration**: If `ENABLE_OTLP_LOGS` is true, logs are exported via the OpenTelemetry Protocol (OTLP) using `OTLPLogExporter`. Resource attributes (`service.name`, `service.version`, `k8s.workflow.node.name`, etc.) are automatically added based on environment variables (`OTEL_RESOURCE_ATTRIBUTES`, `OTEL_WF_NODE_NAME`, `SERVICE_NAME`, `SERVICE_VERSION`).
    *   **Custom Levels**: Includes custom log levels:
        *   `"ACTIVITY"`: For activity-specific logging
        *   `"METRIC"`: For metric-specific logging
        *   `"TRACING"`: For trace-specific logging
    *   **Parquet Sink**: Logs are automatically written to a parquet file for efficient storage and querying. The sink implements buffering and periodic flushing based on batch size and time interval.
    *   **Log Retention**: Implements automatic cleanup of old logs based on the configured retention period. Logs older than `LOG_RETENTION_DAYS` are automatically removed.
*   **Severity Mapping**: Maps standard log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL) and custom levels (ACTIVITY, METRIC, TRACING) to OpenTelemetry `SeverityNumber`.
*   **Configuration**: Log level (`LOG_LEVEL`), OTLP endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`), batching (`OTEL_BATCH_DELAY_MS`, `OTEL_BATCH_SIZE`), etc., are configured via environment variables defined in `application_sdk.constants`.

### Log Models

*   **`LogExtraModel`**: Pydantic model for log extra fields, including:
    *   Request context: `client_host`, `duration_ms`, `method`, `path`, `request_id`, `status_code`, `url`
    *   Workflow context: `workflow_id`, `run_id`, `workflow_type`, `namespace`, `task_queue`, `attempt`
    *   Activity context: `activity_id`, `activity_type`, `schedule_to_close_timeout`, `start_to_close_timeout`, `schedule_to_start_timeout`, `heartbeat_timeout`
    *   Other fields: `log_type`

1. **Parquet Storage**:
   - Logs are stored in parquet format for efficient storage and querying
   - Implements buffering to reduce I/O operations
   - Flushes logs based on two conditions:
     - When buffer size reaches `LOG_BATCH_SIZE`
     - When time since last flush exceeds `LOG_FLUSH_INTERVAL_SECONDS`
   - Uses Hive partitioning for efficient data organization:
     - Partitioned by year/month/day
     - Single file per partition for better performance
     - Uses day-level partitioning for all observability data (logs, metrics, traces)
   - Handles type consistency for partition columns (year, month, day) as integers

2. **Log Retention**:
   - Automatically cleans up logs older than `LOG_RETENTION_DAYS`
   - Runs cleanup once per day
   - Maintains state of last cleanup in Dapr state store
   - Handles both local parquet files and object store cleanup
   - Efficiently deletes entire partition directories

3. **Storage Locations**:
   - Local:
     - Hive partitioned: `/tmp/observability/logs/year=YYYY/month=MM/day=DD/data.parquet`
     - Hive partitioned: `/tmp/observability/metrics/year=YYYY/month=MM/day=DD/data.parquet`
     - Hive partitioned: `/tmp/observability/traces/year=YYYY/month=MM/day=DD/data.parquet`
   - Object Store:
     - Hive partitioned: `logs/year=YYYY/month=MM/day=DD/data.parquet`
     - Hive partitioned: `metrics/year=YYYY/month=MM/day=DD/data.parquet`
     - Hive partitioned: `traces/year=YYYY/month=MM/day=DD/data.parquet`
     (via Dapr object store binding)

### Usage

The primary way to get a logger instance is via the `get_logger` function:

```python
from application_sdk.observability.logger_adaptor import get_logger

# Get a logger instance, usually named after the module
logger = get_logger(__name__)

def my_function(data):
    logger.info(f"Processing data: {data}")
    try:
        # ... do something ...
        logger.activity("Data processing step completed successfully.") # Use custom activity level
    except Exception as e:
        logger.error(f"Failed during processing: {e}", exc_info=True) # Include stack trace

# Log metrics
logger.metric("Request duration", duration_ms=150)

# Log traces
logger.tracing("API call started", endpoint="/api/v1/users")

# In a Temporal Activity:
from temporalio import activity
logger = get_logger(__name__)
activity.logger = logger # Temporal integration

@activity.defn
async def my_activity():
    logger.info("Starting my activity...")
    # Logger automatically includes workflow/activity context
```

### Configuration

The logger can be configured using the following environment variables:

```bash
# Log level and format
LOG_LEVEL=INFO

# Parquet storage settings
LOG_BATCH_SIZE=100  # Number of logs to buffer before writing
LOG_FLUSH_INTERVAL_SECONDS=10  # Seconds between forced flushes
LOG_RETENTION_DAYS=30  # Days to keep logs before cleanup
LOG_CLEANUP_ENABLED=true  # Enable automatic cleanup

# Hive partitioning configuration
ENABLE_HIVE_PARTITIONING=true  # Enable Hive partitioning
# Partitioning is fixed at day level for all observability data

# OTLP settings (if enabled)
ENABLE_OTLP_LOGS=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

## Metrics (`metrics_adaptor.py`)

The SDK provides a comprehensive metrics system using OpenTelemetry (OTLP) integration and local storage capabilities.

### Key Concepts

*   **`MetricRecord`**: A Pydantic model that defines the structure of metric records, including:
    *   `timestamp`: When the metric was recorded
    *   `name`: Name of the metric
    *   `value`: Numeric value of the metric
    *   `type`: Type of metric (counter, gauge, histogram)
    *   `labels`: Key-value pairs for metric dimensions
    *   `description`: Optional description of the metric
    *   `unit`: Optional unit of measurement

*   **`AtlanMetricsAdapter`**: The main interface for metrics within the SDK. It provides:
    *   **OpenTelemetry Integration**: If `ENABLE_OTLP_METRICS` is true, metrics are exported via OTLP using `OTLPMetricExporter`
    *   **Resource Attributes**: Automatically includes service attributes (`service.name`, `service.version`) and workflow node name if available
    *   **Metric Types**: Supports counters, gauges, and histograms
    *   **Parquet Storage**: Metrics are stored in parquet format for efficient storage and querying
    *   **Buffering**: Implements buffering and periodic flushing based on batch size and time interval
    *   **Log Integration**: Metrics are also logged with a custom "METRIC" level for visibility

### Usage

The primary way to get a metrics instance is via the `get_metrics` function:

```python
from application_sdk.observability.metrics_adaptor import get_metrics

# Get the metrics instance
metrics = get_metrics()

# Record different types of metrics
metrics.record_metric(
    name="request_duration_seconds",
    value=1.5,
    metric_type="histogram",
    labels={"endpoint": "/api/v1/users", "method": "GET"},
    description="Request duration in seconds",
    unit="s"
)

metrics.record_metric(
    name="active_connections",
    value=42,
    metric_type="gauge",
    labels={"database": "postgres"},
    description="Number of active database connections"
)

metrics.record_metric(
    name="total_requests",
    value=1,
    metric_type="counter",
    labels={"status": "success"},
    description="Total number of requests processed"
)
```

### Metric Types

1. **Counter**: A cumulative metric that only increases (e.g., total requests, errors)
2. **Gauge**: A metric that can increase and decrease (e.g., active connections, memory usage)
3. **Histogram**: A metric that tracks the distribution of values (e.g., request duration, response size)

### Configuration

The metrics system can be configured using the following environment variables:

```bash
# Metrics storage settings
METRICS_BATCH_SIZE=100  # Number of metrics to buffer before writing
METRICS_FLUSH_INTERVAL_SECONDS=10  # Seconds between forced flushes
METRICS_RETENTION_DAYS=30  # Days to keep metrics before cleanup

# OTLP settings (if enabled)
ENABLE_OTLP_METRICS=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
```

## Traces (`traces_adaptor.py`)

The SDK provides a comprehensive tracing system using OpenTelemetry (OTLP) integration and local storage capabilities.

### Key Concepts

*   **`TraceRecord`**: A Pydantic model that defines the structure of trace records, including:
    *   `timestamp`: When the trace was recorded
    *   `trace_id`: Unique identifier for the trace
    *   `span_id`: Unique identifier for the span
    *   `parent_span_id`: Optional identifier for the parent span
    *   `name`: Name of the span
    *   `kind`: Type of span (SERVER, CLIENT, INTERNAL, etc.)
    *   `status_code`: Status of the span (OK, ERROR, etc.)
    *   `status_message`: Optional message describing the status
    *   `attributes`: Key-value pairs for span attributes
    *   `events`: Optional list of events that occurred during the span
    *   `duration_ms`: Duration of the span in milliseconds

*   **`AtlanTracesAdapter`**: The main interface for traces within the SDK. It provides:
    *   **OpenTelemetry Integration**: If `ENABLE_OTLP_TRACES` is true, traces are exported via OTLP using `OTLPSpanExporter`
    *   **Resource Attributes**: Automatically includes service attributes (`service.name`, `service.version`) and workflow node name if available
    *   **Span Types**: Supports various span kinds (SERVER, CLIENT, INTERNAL)
    *   **Parquet Storage**: Traces are stored in parquet format for efficient storage and querying
    *   **Buffering**: Implements buffering and periodic flushing based on batch size and time interval
    *   **Log Integration**: Traces are also logged with INFO level for visibility
    *   **Automatic Cleanup**: Implements automatic cleanup of old traces based on retention period

### Usage

The primary way to get a traces instance is via the `get_traces` function:

```python
from application_sdk.observability.traces_adaptor import get_traces

# Get the traces instance
traces = get_traces()

# Record a trace
traces.record_trace(
    name="process_request",
    trace_id="1234567890abcdef",
    span_id="abcdef1234567890",
    kind="SERVER",
    status_code="OK",
    attributes={
        "endpoint": "/api/v1/users",
        "method": "GET",
        "user_id": "123"
    },
    duration_ms=150.5,
    events=[{
        "name": "request_processed",
        "attributes": {"status": "success"}
    }]
)
```

### Trace Types

1. **SERVER**: Represents a server-side operation (e.g., handling an HTTP request)
2. **CLIENT**: Represents a client-side operation (e.g., making an HTTP request)
3. **INTERNAL**: Represents an internal operation (e.g., processing data)

### Storage and Retention

The traces adapter implements a sophisticated storage and retention system:

1. **Parquet Storage**:
   - Traces are stored in parquet format for efficient storage and querying
   - Implements buffering to reduce I/O operations
   - Flushes traces based on two conditions:
     - When buffer size reaches `TRACES_BATCH_SIZE`
     - When time since last flush exceeds `TRACES_FLUSH_INTERVAL_SECONDS`
   - Uses Hive partitioning for efficient data organization:
     - Partitioned by year/month/day
     - Single file per partition for better performance
     - Uses day-level partitioning for all observability data

2. **Trace Retention**:
   - Automatically cleans up traces older than `TRACES_RETENTION_DAYS`
   - Runs cleanup once per day
   - Maintains state of last cleanup in Dapr state store
   - Handles both local parquet files and object store cleanup

3. **Storage Locations**:
   - Local: `/tmp/observability/traces/year=YYYY/month=MM/day=DD/data.parquet`
   - Object Store: `traces/year=YYYY/month=MM/day=DD/data.parquet` (via Dapr object store binding)

## AWS Utilities (`aws_utils.py`)

Provides helper functions specifically for interacting with AWS services, particularly RDS authentication.

### Key Functions

*   **`get_region_name_from_hostname(hostname)`**: Extracts the AWS region (e.g., `us-east-1`) from an RDS endpoint hostname.
*   **`generate_aws_rds_token_with_iam_role(...)`**: Assumes an IAM role using STS (`AssumeRole`) and then uses the temporary credentials to generate an RDS authentication token (`rds:GenerateDBAuthToken`). Requires `role_arn`, `host`, `user`. Optionally takes `external_id`, `session_name`, `port`, `region`.
*   **`generate_aws_rds_token_with_iam_user(...)`**: Generates an RDS authentication token directly using IAM user credentials (`aws_access_key_id`, `aws_secret_access_key`). Also requires `host`, `user`. Optionally takes `port`, `region`.

### Usage

These functions are typically used within a custom `Client`'s `load` method or a `Handler`'s credential handling logic when connecting to RDS databases using IAM authentication.

```python
# Example within a hypothetical Client's load method
from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

async def load(self, credentials: dict):
    auth_type = credentials.get("authType")
    if auth_type == "iam_role":
        password = generate_aws_rds_token_with_iam_role(
            role_arn=credentials.get("roleArn"),
            host=credentials.get("host"),
            user=credentials.get("username"),
            external_id=credentials.get("externalId"),
            region=credentials.get("region") # or determine automatically
        )
        # Use the generated token as the password for the connection
    # ... other authentication types ...
```

## General Utilities (`utils.py`)

Contains miscellaneous helper functions used throughout the SDK.

### Key Functions

*   **`prepare_query(query, workflow_args, temp_table_regex_sql)`**: Modifies a base SQL query string by formatting it with include/exclude filters, temporary table exclusion logic, and flags for excluding empty tables or views. Filters are sourced from `workflow_args["metadata"]`.
*   **`prepare_filters(include_filter_str, exclude_filter_str)`**: Parses JSON string filters (include/exclude) and converts them into normalized regex patterns suitable for SQL `WHERE` clauses (e.g., `db1.schema1|db1.schema2`).
*   **`normalize_filters(filter_dict, is_include)`**: Takes a dictionary defining filters (e.g., `{"db1": ["schema1", "schema2"], "db2": "*"}`) and converts it into a list of normalized regex strings.
*   **`get_workflow_config(config_id)`**: Retrieves workflow configuration data stored via `StateStoreInput`.
*   **`update_workflow_config(config_id, config)`**: Updates specific keys in a stored workflow configuration via `StateStoreOutput`.
*   **`read_sql_files(queries_prefix)`**: Recursively reads all `.sql` files from a specified directory (`queries_prefix`). Returns a dictionary mapping uppercase filenames (without `.sql`) to their string content. Useful for loading SQL queries used in activities.
*   **`get_actual_cpu_count()`**: Attempts to determine the number of CPUs available to the current process, considering potential container limits (via `os.sched_getaffinity`), falling back to `os.cpu_count()`.
*   **`get_safe_num_threads()`**: Calculates a reasonable number of threads for parallel processing, typically `get_actual_cpu_count() + 4`.
*   **`parse_credentials_extra(credentials)`**: Safely parses the `extra` field within a `credentials` dictionary (assuming it's a JSON string) and merges its contents back into the main dictionary.
*   **`run_sync(func)`**: A decorator (intended for internal use, e.g., in `AsyncBaseSQLClient`) to run a synchronous function (`func`) in a `ThreadPoolExecutor` to avoid blocking an asyncio event loop.

### Usage Examples

```python
# Reading SQL files for activities
from application_sdk.common.utils import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/my/queries")
fetch_tables_query = SQL_QUERIES.get("FETCH_TABLES")

# Getting workflow config
from application_sdk.common.utils import get_workflow_config

config = get_workflow_config("my-config-id-123")
api_key = config.get("credentials", {}).get("apiKey")

# Preparing filters for a query
from application_sdk.common.utils import prepare_filters

include_pattern, exclude_pattern = prepare_filters(
    '{"prod_db": ["analytics", "reporting$"]}', # Include specific schemas in prod_db
    '{"dev_db": "*"}' # Exclude all of dev_db
)
# use patterns in SQL: WHERE table_schema SIMILAR TO '{include_pattern}' AND table_schema NOT SIMILAR TO '{exclude_pattern}'
```

## Summary

The `common` utilities provide essential services for logging, AWS integration, configuration management, and various helper tasks, forming a core part of the SDK's functionality and promoting consistent practices across different modules.