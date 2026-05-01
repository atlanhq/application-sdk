# Common Utilities

This section describes utility functions and classes in the `application_sdk.common` package used across the SDK.

## Logging

v3 uses `structlog` for structured logging. The v2 patterns of `workflow.logger` and `activity.logger` from Temporal are no longer used -- all logging goes through `structlog`.

### Getting a Logger

```python
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

def my_function(data):
    logger.info("processing_data", data=data)
    try:
        result = process(data)
        logger.info("processing_complete", rows=result.count)
    except Exception:
        logger.error("processing_failed", exc_info=True)
```

### Structured Context

`structlog` automatically includes bound context in every log message. Inside `@task` methods and handlers, the framework binds workflow and request context (workflow_id, run_id, etc.) automatically.

```python
# Bind context for a block of work
logger = get_logger(__name__).bind(
    connection_id="conn-123",
    step="metadata_extraction",
)
logger.info("starting_extraction")  # includes connection_id and step
```

### Configuration

Logging is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Minimum log level |
| `ENABLE_OTLP_LOGS` | `false` | Export logs via OpenTelemetry Protocol |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP endpoint |

## Error Handling

The SDK provides a standardized error system with error codes and categories.

### Error Code Format

Error codes follow the format: `Atlan-{Component}-{HTTP_Code}-{Unique_ID}`

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
    raise ClientError(f"{ClientError.INPUT_VALIDATION_ERROR}: {e}") from e
```

## SQL Utilities

### read_sql_files

Reads all `.sql` files from a directory and returns them as a dictionary:

```python
from application_sdk.common.utils import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/queries")
fetch_tables_query = SQL_QUERIES.get("FETCH_TABLES")
```

Keys are uppercase filenames without the `.sql` extension.

### prepare_query

Formats a SQL query with include/exclude filters:

```python
from application_sdk.common.utils import prepare_query

query = prepare_query(
    base_query,
    workflow_args,
    temp_table_regex_sql="...",
)
```

### prepare_filters

Parses JSON filter strings into regex patterns for SQL `WHERE` clauses:

```python
from application_sdk.common.utils import prepare_filters

include_pattern, exclude_pattern = prepare_filters(
    '{"prod_db": ["analytics", "reporting"]}',
    '{"dev_db": "*"}',
)
```

## AWS Utilities

Helper functions for AWS RDS IAM authentication:

```python
from application_sdk.common.aws_utils import (
    generate_aws_rds_token_with_iam_role,
    generate_aws_rds_token_with_iam_user,
    get_region_name_from_hostname,
)

# IAM role authentication
token = generate_aws_rds_token_with_iam_role(
    role_arn="arn:aws:iam::123456789:role/my-role",
    host="mydb.cluster-xyz.us-east-1.rds.amazonaws.com",
    user="admin",
)

# IAM user authentication
token = generate_aws_rds_token_with_iam_user(
    aws_access_key_id="AKIA...",
    aws_secret_access_key="...",
    host="mydb.cluster-xyz.us-east-1.rds.amazonaws.com",
    user="admin",
)
```

## General Utilities

| Function | Description |
|----------|-------------|
| `get_actual_cpu_count()` | CPU count respecting container limits |
| `get_safe_num_threads()` | Reasonable thread count for parallel work (`cpu_count + 4`) |
| `parse_credentials_extra(credentials)` | Parse the `extra` JSON field in a credentials dict |

## Temporal Configuration

| Constant | Env Var | Default | Description |
|----------|---------|---------|-------------|
| `TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `0.0.0.0:9464` | Bind address for Temporal SDK Prometheus metrics |
