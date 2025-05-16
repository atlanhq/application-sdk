# Observability Guide

The Application SDK provides a comprehensive observability system that includes logging, metrics, and tracing capabilities. This guide explains how to use these features effectively in your applications.

## Overview

The observability system consists of four main components:

1. **Logging**: Structured logging with context-aware information
2. **Metrics**: Collection and export of application metrics
3. **Traces**: Distributed tracing for request flows
4. **Base Observability**: Common infrastructure for all observability features

## Base Observability

The `AtlanObservability` class provides the foundation for all observability features. It handles:

- Buffering of records
- Periodic flushing to storage
- Parquet file management
- Object store integration
- Cleanup of old records

### Key Features

- **Batch Processing**: Records are buffered and written in batches
- **Date-based Files**: Optional date-based file organization
- **Retention Policy**: Automatic cleanup of old records
- **Dapr Integration**: Storage in object store via Dapr
- **Error Handling**: Graceful handling of system signals and exceptions

## Logging

The `AtlanLoggerAdapter` provides enhanced logging capabilities with:

- Structured logging with context
- Custom log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL, ACTIVITY, METRIC, TRACING)
- OpenTelemetry integration
- Temporal workflow and activity context
- Request context tracking

### Usage

```python
from application_sdk.common.logger_adaptors import get_logger

# Get a logger instance
logger = get_logger(__name__)

# Basic logging
logger.info("Operation completed successfully")

# Logging with context
logger.info("Processing request", extra={"request_id": "123", "user": "john"})

# Activity logging
logger.activity("Activity started", extra={"activity_id": "act_123"})

# Metric logging
logger.metric("Metric recorded", extra={"metric_name": "requests_total"})

# Tracing
logger.tracing("Trace started", extra={"trace_id": "trace_123"})
```

## Metrics

The `AtlanMetricsAdapter` handles metric collection and export with:

- Support for different metric types (Counter, Gauge, Histogram)
- OpenTelemetry integration
- Buffered storage in Parquet files
- Console logging of metrics

### Metric Types

- **Counter**: Monotonically increasing values (e.g., request count)
- **Gauge**: Point-in-time measurements (e.g., memory usage)
- **Histogram**: Statistical distributions (e.g., request duration)

### Usage

```python
from application_sdk.common.metrics_adaptor import get_metrics, MetricType

# Get metrics instance
metrics = get_metrics()

# Record a counter metric
metrics.record_metric(
    name="requests_total",
    value=1.0,
    metric_type=MetricType.COUNTER,
    labels={"endpoint": "/api/users"},
    description="Total number of requests",
    unit="count"
)

# Record a gauge metric
metrics.record_metric(
    name="memory_usage",
    value=1024.0,
    metric_type=MetricType.GAUGE,
    labels={"component": "cache"},
    description="Memory usage in bytes",
    unit="bytes"
)

# Record a histogram metric
metrics.record_metric(
    name="request_duration",
    value=0.5,
    metric_type=MetricType.HISTOGRAM,
    labels={"endpoint": "/api/users"},
    description="Request duration in seconds",
    unit="seconds"
)
```

## Tracing

The `AtlanTracesAdapter` provides distributed tracing capabilities with:

- Span creation and management
- Context propagation
- OpenTelemetry integration
- Trace context management
- Error tracking

### Usage

```python
from application_sdk.common.traces_adaptor import get_traces
import uuid

# Get traces instance
traces = get_traces()

# Create a trace context
with traces.record_trace(
    name="process_request",
    trace_id=str(uuid.uuid4()),
    span_id=str(uuid.uuid4()),
    kind="SERVER",
    status_code="OK",
    attributes={"endpoint": "/api/users"}
) as trace:
    # Your code here
    pass

# Trace with error handling
try:
    with traces.record_trace(
        name="process_request",
        trace_id=str(uuid.uuid4()),
        span_id=str(uuid.uuid4()),
        kind="SERVER",
        status_code="OK",
        attributes={"endpoint": "/api/users"}
    ) as trace:
        # Your code here
        raise Exception("Something went wrong")
except Exception as e:
    # Error will be automatically recorded in the trace
    pass
```

## Configuration

The observability system can be configured using environment variables:

### Common Configuration

- `OBSERVABILITY_DIR`: Directory for storing observability data
- `ENABLE_OBSERVABILITY_DAPR_SINK`: Enable Dapr sink for storage
- `OTEL_RESOURCE_ATTRIBUTES`: OpenTelemetry resource attributes
- `OTEL_WF_NODE_NAME`: Workflow node name for Argo environment

### Logging Configuration

- `LOG_LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `LOG_BATCH_SIZE`: Number of logs to batch before flushing
- `LOG_FLUSH_INTERVAL_SECONDS`: Interval between forced flushes
- `LOG_RETENTION_DAYS`: Number of days to retain logs
- `LOG_CLEANUP_ENABLED`: Enable cleanup of old logs

### Metrics Configuration

- `ENABLE_OTLP_METRICS`: Enable OpenTelemetry metrics
- `METRICS_BATCH_SIZE`: Number of metrics to batch before flushing
- `METRICS_FLUSH_INTERVAL_SECONDS`: Interval between forced flushes
- `METRICS_RETENTION_DAYS`: Number of days to retain metrics
- `METRICS_CLEANUP_ENABLED`: Enable cleanup of old metrics

### Tracing Configuration

- `ENABLE_OTLP_TRACES`: Enable OpenTelemetry traces
- `TRACES_BATCH_SIZE`: Number of traces to batch before flushing
- `TRACES_FLUSH_INTERVAL_SECONDS`: Interval between forced flushes
- `TRACES_RETENTION_DAYS`: Number of days to retain traces
- `TRACES_CLEANUP_ENABLED`: Enable cleanup of old traces

## Best Practices

1. **Use Appropriate Log Levels**
   - DEBUG: Detailed information for debugging
   - INFO: General operational information
   - WARNING: Warning messages for potential issues
   - ERROR: Error messages for recoverable errors
   - CRITICAL: Critical errors that may cause system failure

2. **Include Context in Logs**
   - Request IDs
   - User information
   - Operation details
   - Error codes and messages

3. **Choose the Right Metric Type**
   - Use counters for cumulative values
   - Use gauges for point-in-time measurements
   - Use histograms for statistical distributions

4. **Trace Important Operations**
   - API endpoints
   - Database operations
   - External service calls
   - Long-running processes

5. **Handle Errors Gracefully**
   - Log errors with stack traces
   - Record error metrics
   - Create error spans in traces

6. **Monitor Resource Usage**
   - Memory usage
   - CPU utilization
   - Network I/O
   - Disk I/O

## Integration with External Systems

The observability system integrates with:

1. **OpenTelemetry**
   - Metrics export
   - Trace export
   - Log export

2. **Dapr**
   - Object store for data persistence
   - State store for cleanup tracking

3. **Temporal**
   - Workflow context in logs
   - Activity context in logs
   - Trace correlation

## Troubleshooting

Common issues and solutions:

1. **Missing Logs**
   - Check log level configuration
   - Verify Dapr sink is enabled
   - Check file permissions

2. **Missing Metrics**
   - Verify OpenTelemetry is enabled
   - Check metric type is correct
   - Ensure labels are properly formatted

3. **Missing Traces**
   - Verify OpenTelemetry is enabled
   - Check trace context propagation
   - Ensure spans are properly closed

4. **Storage Issues**
   - Check disk space
   - Verify Dapr connection
   - Check file permissions

5. **Performance Issues**
   - Adjust batch sizes
   - Modify flush intervals
   - Check resource usage

## Error Codes

The Application SDK provides a standardized error code system for consistent error handling and reporting. Error codes follow the format: `Atlan-{Component}-{HTTP_Code}-{Unique_ID}`

### Components

The system defines several error components:
- `Client`: Client-related errors
- `FastApi`: Server and API errors
- `Temporal`: Workflow and activity errors
- `IO`: Input/Output errors
- `Common`: Common utility errors
- `Docgen`: Documentation generation errors
- `TemporalActivity`: Activity-specific errors
- `AtlasTransformer`: Atlas transformer errors

### Error Code Structure

Each error code consists of:
1. **Component**: The system component generating the error
2. **HTTP Code**: Standard HTTP status code
3. **Unique ID**: A unique identifier for the specific error

Example: `Atlan-Client-403-00` represents a client request validation error.

### Adding New Error Codes

To add new error codes:

1. **Choose the Component**
   - Select the appropriate component from `ErrorComponent` enum
   - If needed, add a new component to the enum

2. **Define the Error Code**
   ```python
   from application_sdk.common.error_codes import ErrorCode

   # Add to the appropriate error dictionary
   NEW_ERRORS = {
       "CUSTOM_ERROR": ErrorCode(
           "Component",  # Component name
           "500",       # HTTP status code
           "00",        # Unique ID
           "Error description"  # Human-readable description
       )
   }
   ```

3. **Add to Combined Dictionary**
   ```python
   ERROR_CODES: Dict[str, ErrorCode] = {
       **EXISTING_ERRORS,
       **NEW_ERRORS,
   }
   ```

### Best Practices

1. **Naming Convention**
   - Use UPPERCASE for error code keys
   - Use descriptive names that indicate the error type
   - Follow existing patterns in the codebase

2. **HTTP Status Codes**
   - 400: Bad Request
   - 401: Unauthorized
   - 403: Forbidden
   - 404: Not Found
   - 422: Unprocessable Entity
   - 500: Internal Server Error
   - 503: Service Unavailable

3. **Unique IDs**
   - Use two-digit numbers (00-99)
   - Keep IDs sequential within each component
   - Document new IDs in comments

4. **Error Descriptions**
   - Be clear and concise
   - Include actionable information
   - Avoid sensitive information

### Usage Example

```python
from application_sdk.common.error_codes import ERROR_CODES

# Get error code
error = ERROR_CODES["CLIENT_AUTH_ERROR"]
print(error)  # Output: Atlan-Client-401-00: Client authentication failed

# Use in exception handling
try:
    # Your code here
    pass
except Exception as e:
    error = ERROR_CODES["CLIENT_AUTH_ERROR"]
    logger.error(f"{error}: {str(e)}")
```

### Integration with Logging

Error codes integrate with the logging system to provide structured error information:

```python
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.error_codes import ERROR_CODES

logger = get_logger(__name__)

try:
    # Your code here
    pass
except Exception as e:
    error = ERROR_CODES["CLIENT_AUTH_ERROR"]
    logger.error(
        "Authentication failed",
        extra={
            "error_code": error.code,
            "error_description": error.description,
            "exception": str(e)
        }
    )
```

This structured approach to error handling helps with:
- Consistent error reporting
- Error tracking and analysis
- Debugging and troubleshooting
- Integration with monitoring systems 