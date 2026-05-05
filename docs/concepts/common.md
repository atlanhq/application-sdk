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

The SDK provides a structured error hierarchy in `application_sdk/errors/` with a closed `FailureCategory` enum and an orthogonal `Audience` enum for routing.

### Categorical hierarchy (new code)

Raise the leaf class whose `FailureCategory` matches the failure. Each leaf sets `retryable` and `audience` defaults:

```python
from application_sdk.errors import (
    AuthError,              # CATEGORY=AUTH,  retryable=False, audience=USER
    DependencyUnavailableError,  # retryable=True,  audience=PLATFORM
    InvalidInputError,      # retryable=False, audience=USER
    NotFoundError,          # retryable=False, audience=USER
    AlreadyExistsError,     # retryable=False, audience=USER
    PreconditionError,      # retryable=False, audience=USER
    RateLimitedError,       # retryable=True,  audience=USER
    AppTimeoutError,        # retryable=True,  audience=APP_OWNER
    AppPermissionDeniedError, # retryable=False, audience=USER
    ResourceExhaustedError, # retryable=True,  audience=PLATFORM
    DataIntegrityError,     # retryable=False, audience=APP_OWNER
    InternalError,          # retryable=False, audience=APP_OWNER
    UnimplementedError,     # retryable=False, audience=APP_OWNER
    CancelledError,         # retryable=False, audience=APP_OWNER
)

raise DependencyUnavailableError(
    message="Temporal frontend unreachable",
    service="temporal", target="temporal-frontend:7233", cause=exc,
)
```

`Audience` is a closed three-value enum — every leaf must pick one:
- `USER` — customer self-service (credentials, IAM, source config)
- `PLATFORM` — infra ops (shared deps down: Dapr, Temporal, object store, pod health)
- `APP_OWNER` — the team that wrote the failing code (connector or SDK): file a bug, add a more specific subclass, or investigate

There is no `UNKNOWN` escape hatch — if the locus is unclear, the answer is `APP_OWNER` (the team investigates and reclassifies).

`FailureDetails` (the Pydantic wire envelope on `AppError.to_failure_details()`) carries `category`, `code`, `retryable`, `audience`, `message`, an optional `suggested_action` (imperative remediation hint whose voice shifts with the audience), `app_name`, `run_id`, and `cause_repr`. Tenant identity is intentionally not on this envelope — per-tenant attribution is the consumer's responsibility, not the producer's.

### Legacy error-code namespaces (backward-compat only)

- **`application_sdk.common.error_codes`** — `ATLAN-{COMPONENT}-{HTTP_CODE}-{SEQ}` HTTP-style codes. Do not use in new code.
- **`application_sdk.errors` legacy constants** — `AAF-{COMP}-{NNN}` format (`APP_ERROR`, `HANDLER_ERROR`, etc.). Do not use in new code; retained for v3.x back-compat, removed in v4.0.

### Legacy constant usage (back-compat shim)

```python
# Still works for existing code — do not use in new code
from application_sdk.errors import APP_ERROR, APP_NON_RETRYABLE, HANDLER_ERROR

logger.error("Task failed [%s]", APP_ERROR, exc_info=exc)

from application_sdk.execution import ApplicationError
raise ApplicationError(str(APP_NON_RETRYABLE), non_retryable=True)
```

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
