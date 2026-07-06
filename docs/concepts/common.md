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

The SDK provides a structured error hierarchy in `application_sdk/errors/` built on two axes: a
closed `FailureCategory` enum (*what happened*) and an orthogonal `Audience` enum (*who must act*).

### Two-level hierarchy

```
AppError  (base — application_sdk.errors)
│
├── Categorical leaves  (application_sdk.errors.leaves)
│   ├── AuthError              CATEGORY=AUTH              retryable=False  audience=USER
│   ├── AppPermissionDeniedError  PERMISSION             retryable=False  audience=USER
│   ├── NotFoundError          NOT_FOUND                  retryable=False  audience=USER
│   ├── AlreadyExistsError     ALREADY_EXISTS             retryable=False  audience=USER
│   ├── InvalidInputError      INVALID_INPUT              retryable=False  audience=USER
│   ├── PreconditionError      PRECONDITION               retryable=False  audience=USER
│   ├── RateLimitedError       RATE_LIMITED               retryable=True   audience=USER
│   ├── DependencyUnavailableError  DEPENDENCY_UNAVAILABLE retryable=True  audience=PLATFORM
│   ├── SourceUnavailableError   SOURCE_UNAVAILABLE        retryable=True   audience=USER
│   ├── ResourceExhaustedError RESOURCE_EXHAUSTED         retryable=True   audience=PLATFORM
│   ├── AppTimeoutError        TIMEOUT                    retryable=True   audience=APP_OWNER
│   ├── CancelledError         CANCELLED                  retryable=False  audience=APP_OWNER
│   ├── DataIntegrityError     DATA_INTEGRITY             retryable=False  audience=APP_OWNER
│   ├── InternalError          INTERNAL                   retryable=False  audience=APP_OWNER
│   └── UnimplementedError     UNIMPLEMENTED              retryable=False  audience=APP_OWNER
│
└── Domain umbrellas  (leaf-first multi-inheritance)
    ├── CredentialError(AuthError)
    │   ├── CredentialNotFoundError(NotFoundError, CredentialError)
    │   ├── CredentialParseError(InvalidInputError, CredentialError)
    │   └── CredentialValidationError(InvalidInputError, CredentialError)
    ├── StorageError(DependencyUnavailableError)
    │   ├── StorageNotFoundError(NotFoundError, StorageError)
    │   ├── StoragePermissionError(AppPermissionDeniedError, StorageError)
    │   └── StorageConfigError(InvalidInputError, StorageError)
    └── SecretStoreError(DependencyUnavailableError)
        ├── SecretNotFoundError(NotFoundError, SecretStoreError)
        └── SecretStoreUnavailableError(SecretStoreError, ColdStartRaceError)
```

The **categorical leaf** (listed first in the MRO) drives `category`, `audience`, and
`default_retryable` on the wire. The **domain umbrella** (listed second) keeps legacy
`except StorageError:` / `except CredentialError:` catch sites alive. A single exception
instance satisfies both hierarchies simultaneously.

### ColdStartRaceError — the cross-domain transient marker

`ColdStartRaceError(DependencyUnavailableError)` is not a domain umbrella itself — it's a
marker mixed into a domain leaf's transient subtype to answer one narrow question: "is this
specific failure a not-yet-reachable dependency right now" (a transport failure, or — for the
secrets domain specifically — the one Dapr secrets-API error code that unambiguously means "no
secret store registered yet"), independent of the general `retryable` wire hint. A bare 5xx
from the Dapr *secrets* API is deliberately NOT treated as proof of unreachability on its own:
verified against a live sidecar, a genuinely-missing secret key also returns 500 with
`errorCode=ERR_SECRET_GET` — indistinguishable by status code alone from a still-cold
component — so classification there additionally inspects the JSON error body's `errorCode`
(see `application_sdk.infrastructure._dapr.client.classify_secret_fetch_error`). A generic
helper — `application_sdk.infrastructure.retry_past_dapr_cold_start` — retries any current or
future subtype across domains (secret store today; state store, pub/sub, or credential-vault
config fetches tomorrow) just by catching this one marker, with no new per-domain check needed.
`SecretStoreUnavailableError` above is the first concrete example: it multiply-inherits
`SecretStoreError` (so `except SecretStoreError:` still catches it) and `ColdStartRaceError`
(so the retry helper does too).

### Raise by failure shape

Pick the leaf whose `FailureCategory` best describes what happened. Prefer a domain subclass
when the calling context is clearly within that subsystem:

```python
from application_sdk.errors import (
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
    RateLimitedError,
)
from application_sdk.storage.errors import StorageNotFoundError

# Generic categorical leaf — any context
raise DependencyUnavailableError(
    message="Temporal frontend unreachable",
    service="temporal", target="temporal-frontend:7233", cause=exc,
)

# Domain subclass — storage context; routes as NOT_FOUND, catchable as StorageError
raise StorageNotFoundError(
    message="Object not found in bucket",
    key="artifacts/run-123/output.parquet",
)
```

### Catch by shape or by domain

```python
from application_sdk.errors import NotFoundError, AppError
from application_sdk.storage.errors import StorageError

# Catch any not-found regardless of domain:
except NotFoundError as e:
    ...

# Catch any storage failure regardless of category:
except StorageError as e:
    ...

# Catch everything the SDK can raise:
except AppError as e:
    fd = e.to_failure_details()
    logger.error("failure category=%s audience=%s", fd.category, fd.audience, exc_info=True)
```

### Audience

`Audience` is a closed three-value enum — every leaf must pick one:

| Value | First-responder |
|---|---|
| `USER` | Customer self-service (credentials, IAM, source config) |
| `PLATFORM` | Infra ops — shared deps down: Dapr, Temporal, object store, pod health |
| `APP_OWNER` | The team that wrote the failing code (connector or SDK): file a bug, add a specific subclass, investigate |

There is no `UNKNOWN` escape hatch. If the locus is unclear, `APP_OWNER` means "the team
that wrote this code investigates and reclassifies."

### Wire envelope

`AppError.to_failure_details()` builds a Pydantic `FailureDetails` envelope suitable for
`ApplicationError.details=[…]` in Temporal:

```python
fd = e.to_failure_details()
# fd.category      — FailureCategory enum (routing: what happened)
# fd.audience      — Audience enum (routing: who acts)
# fd.retryable     — bool (resolved from class default or per-instance override)
# fd.code          — str (app-owned fine-grained code, e.g. "NOT_FOUND_STORAGE")
# fd.suggested_action — str | None (imperative hint; voice shifts with audience)
# fd.evidence      — dict of per-error structured context (dataclass fields)
# fd.cause_repr    — str | None (sanitised str of wrapped exception: "{ExcType}: {msg}", URL/secret-redacted; cause message capped at 500 chars; never the live object)
```

Tenant identity is intentionally absent from `FailureDetails`. Per-tenant attribution is
the consumer's responsibility (e.g., the Automation Engine attaches tenant from its own
session at ingest time).

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
| `TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS` | `127.0.0.1:9464` | Bind address for Temporal SDK Prometheus metrics. Loopback-only — not externally reachable. Combined-mode FastAPI `/metrics` proxies it in-process. |
