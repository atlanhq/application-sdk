# Common Utilities

This section describes utility functions and classes in the `application_sdk.common` package used across the SDK.

## Logging

v3 uses `loguru` (via an `AtlanLoggerAdapter` wrapper) for structured logging. The v2 patterns of `workflow.logger` and `activity.logger` from Temporal are no longer used — all logging goes through `get_logger`.

### Getting a Logger

```python
from application_sdk.observability import get_logger

logger = get_logger(__name__)

def my_function(data):
    logger.info("processing_data: %s", data)
    try:
        result = process(data)
        logger.info("processing_complete: rows=%s", result.count)
    except Exception:
        logger.error("processing_failed", exc_info=True)
```

Use `%`-style format strings in message bodies. The only kwarg you should ever pass to a log call is `exc_info=True` (or `exc_info=exc`); embed every other field — `correlation_id`, `workflow_id`, `run_id`, etc. — in the message body via %-style so it is always visible in log output regardless of pipeline configuration.

### Configuration

Logging is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ATLAN_LOG_LEVEL` | `INFO` | Minimum log level (fallback: `LOG_LEVEL`) |
| `ENABLE_OTLP_LOGS` | `false` | Export logs via OpenTelemetry Protocol |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP endpoint |

## Error Handling

The SDK provides a standardized error system with error codes and categories.

The SDK has two error-code namespaces:

- **`application_sdk.common.error_codes`** — categorised HTTP-style codes for client-facing errors. Format: `ATLAN-{COMPONENT}-{HTTP_CODE}-{SEQ}` (e.g. `ATLAN-CLIENT-403-00`).
- **`application_sdk.errors`** — app-framework codes for Temporal / monitoring signals. Format: `AAF-{COMP}-{NNN}` (e.g. `AAF-APP-001`).

### Error Code Format (`application_sdk.common.error_codes`)

Error codes follow the format: `ATLAN-{Component}-{HTTP_Code}-{Unique_ID}`

Example: `ATLAN-CLIENT-403-00` for a request validation error.

### Error Categories

| Category | Import | Description |
|----------|--------|-------------|
| `ClientError` | `application_sdk.common.error_codes` | Client-related errors (400-499) |
| `ApiError` | `application_sdk.common.error_codes` | Server and API errors (500-599) |
| `OrchestratorError` | `application_sdk.common.error_codes` | Workflow and task errors |
| `IOError` | `application_sdk.common.error_codes` | Input/Output errors |

### Usage

```python
from application_sdk.common.error_codes import ClientError

try:
    validate_input(data)
except ValidationError as e:
    raise ClientError(f"{ClientError.REQUEST_VALIDATION_ERROR}: {e}") from e
```

For application-level error codes, use the top-level `application_sdk.errors` module:

```python
from application_sdk.errors import APP_ERROR, APP_NON_RETRYABLE, HANDLER_ERROR

# Log structured error codes for monitoring/alerting
logger.error("Task failed [%s]", APP_ERROR, exc_info=exc)

# Reference error codes in ApplicationError for Temporal retry control
from application_sdk.execution import ApplicationError
raise ApplicationError(str(APP_NON_RETRYABLE), non_retryable=True)
```

Available error constants: `APP_ERROR`, `APP_NON_RETRYABLE`, `HANDLER_ERROR`, `CONTRACT_VALIDATION`, `PAYLOAD_SAFETY`, `CREDENTIAL_ERROR`, `CREDENTIAL_NOT_FOUND`, `STORAGE_NOT_FOUND`, `SECRET_NOT_FOUND`.

## SQL Utilities

### read_sql_files

Reads all `.sql` files from a directory and returns them as a dictionary:

```python
from application_sdk.common.sql_filters import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/queries")
fetch_tables_query = SQL_QUERIES.get("FETCH_TABLES")
```

Keys are uppercase filenames without the `.sql` extension.

### prepare_query

Formats a SQL query with include/exclude filters:

```python
from application_sdk.common.sql_filters import prepare_query

query = prepare_query(
    base_query,
    workflow_args,
    temp_table_regex_sql="...",
)
```

### prepare_filters

Parses JSON filter strings into regex patterns for SQL `WHERE` clauses:

```python
from application_sdk.common.sql_filters import prepare_filters

include_pattern, exclude_pattern = prepare_filters(
    '{"prod_db": ["analytics", "reporting"]}',
    '{"dev_db": "*"}',
)
```

## General Utilities

| Function | Import | Description |
|----------|--------|-------------|
| `get_actual_cpu_count()` | `application_sdk.common` | CPU count respecting container limits |
| `get_safe_num_threads()` | `application_sdk.common` | Reasonable thread count for parallel work (`cpu_count * 2`, min 2) |
| `parse_credentials_extra(credentials)` | `application_sdk.credentials` | Parse the `extra` JSON field in a credentials dict |

## Temporal Configuration

| Constant | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `0.0.0.0:9464` | Bind address for Temporal SDK Prometheus metrics |
