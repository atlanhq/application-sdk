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
| `ATLAN_LOG_SOURCE` | `APPLICATION_NAME` | Overrides the app label in the `source` field stamped on every record. Records from `application_sdk` are labelled `sdk` and known third-party loggers `dependency` regardless of this setting — it only renames the *app* bucket. Apps should leave it unset so their own name appears; the Automation Engine sets `ae` so its orchestration lines are attributed to the engine rather than to the app it is running. See [Monitoring → Log provenance](monitoring.md#log-provenance-the-source-field). |

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
│   │   └── DiskFullError      RESOURCE_EXHAUSTED (RESOURCE_EXHAUSTED_DISK_FULL)  retryable=True   audience=PLATFORM
│   ├── AppTimeoutError        TIMEOUT                    retryable=True   audience=APP_OWNER
│   │   └── TaskStalledError   TIMEOUT (TIMEOUT_TASK_STALLED)  retryable=True   audience=APP_OWNER
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
    │   ├── StorageConfigError(InvalidInputError, StorageError)
    │   └── StorageBucketRelocationError(StorageError)                                 # platform-side, temporary
    └── SecretStoreError(DependencyUnavailableError)
        ├── SecretNotFoundError(NotFoundError, SecretStoreError)
        ├── SecretStoreUnavailableError(SecretStoreError, ColdStartRaceError)          # transient
        └── SecretStoreUnreachableError(SecretStoreError, DaprSidecarUnreachableError)  # terminal
```

The **categorical leaf** (listed first in the MRO) drives `category`, `audience`, and
`default_retryable` on the wire. The **domain umbrella** (listed second) keeps legacy
`except StorageError:` / `except CredentialError:` catch sites alive. A single exception
instance satisfies both hierarchies simultaneously.

### StorageBucketRelocationError — a write rejected by a bucket relocation

`StorageBucketRelocationError(StorageError)` keeps the generic
`DependencyUnavailableError` category and PLATFORM audience of its parent, but carries its own
`code` (`DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION`) and `ErrorCode` (`AAF-STR-008`) rather than
the generic `AAF-STR-004`. It exists because a dual-/multi-region bucket relocation makes a store
reject multipart upload *initiation* for the whole move window while plain single-request PUTs
keep working — so artifact uploads above the writer's part size fail while smaller ones succeed.

Nothing the app or the customer controls fixes it: no credential, permission, or connector change
shortens a relocation. That is why it is PLATFORM-attributed and `retryable=True`, and why its
`suggested_action` says to retry once the relocation finishes. Both the preflight gate's
`objectStoreAccess:<store>` check and a mid-run `upload_file` failure raise or stamp this one code,
so a relocation lands in a single analytics bucket wherever it is caught.

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

**Terminal vs transient — `DaprSidecarUnreachableError`.** `ColdStartRaceError` means "not
reachable *yet*, still waiting". Its terminal counterpart is
`DaprSidecarUnreachableError(ColdStartRaceError)`, raised by `retry_past_dapr_cold_start` only
when the whole cold-start budget elapses without one usable answer — "waited the whole budget,
*done* waiting". It stays a `ColdStartRaceError` subtype on purpose: the same
`except ColdStartRaceError:` sites keep catching it and its category stays `DEPENDENCY_UNAVAILABLE`
(so preflight-gate routing and `gate_broken` are unchanged), while its distinct type name and
`code = DEPENDENCY_UNAVAILABLE_SIDECAR_UNREACHABLE` — plus `component` / `attempts` /
`elapsed_seconds` — let an operator tell a persistent sidecar outage from a still-booting one. Catch
`ColdStartRaceError` to retry the race; read the concrete subtype to report the fault.

The secrets domain carries both forms as a pair: `SecretStoreUnavailableError` (transient) and
`SecretStoreUnreachableError(SecretStoreError, DaprSidecarUnreachableError)` (terminal). The
secret-resolution catch sites re-raise the terminal one — hash-labelled and cause-free, same
redaction as the transient — when `retry_past_dapr_cold_start` exhausts its budget, so a
budget-exhausted outage stays distinguishable from a still-cold race end-to-end even after the raw
`DaprSidecarUnreachableError` is redacted at the secret boundary. Both stay `SecretStoreError` (so
`except SecretStoreError:` catches either) and `ColdStartRaceError` (so the probe aggregators route
either); only a store that has already answered once (steady state, not first contact) surfaces the
transient type on a later blip.

### TaskStalledError — raised by the SDK, never by an app

`TaskStalledError(AppTimeoutError)` is the failure the stall watchdog produces when an activity
attempt keeps heartbeating but nothing observable advances for longer than the task's
no-progress budget (ADR-0018). It carries `stalled_for_seconds` and `last_progress_label`, so
the failure names *where* the attempt went quiet rather than only that it did, and it is
**retryable**: the dominant cause is a transient source-side hang that self-heals on a fresh
attempt. A subtype rather than a sixteenth leaf, so `except AppTimeoutError:` still catches it
while the distinct `TIMEOUT_TASK_STALLED` code and the `TaskStalledError` Temporal wire type keep stall
kills countable apart from `StartToClose` and heartbeat timeouts. App code should not raise it —
raise the leaf that describes what the source actually did.

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
# fd.cause_repr    — str | None (sanitised str of wrapped exception: "{ExcType}: {msg}", URL/secret-redacted; cause message capped at 2000 chars; never the live object)
```

#### How `cause_repr` is sanitised

Three steps, in this order — the order is what makes the result safe:

1. **Redact.** `redact_secrets` strips URL userinfo and known secret query-params,
   including the presigned-URL signatures (`X-Amz-Signature`, `X-Goog-Signature`,
   Azure's `sig=`) that object-store errors quote verbatim.
2. **Strip the driver's debug dump.** `object_store` appends a multi-line Rust
   `Debug source:` block to every error's `str()`. It sits *after* the provider's
   own explanation, so it would otherwise compete for the budget.
3. **Cap at 2000 chars, keeping both ends** — 1200 from the head, 700 from the
   tail, with `…[N chars elided]…` between them. A backend error puts the request
   URL at the head and the reason at the tail, so a head-only cut spends the whole
   budget on boilerplate and deletes the diagnostic.

Redaction runs *before* truncation, so keeping a tail can never expose an
unredacted secret. Do not reorder these.

#### Storage failures carry the backend's verdict

Every `Storage*` error populates `evidence` with `service` (always
`"object_store"`), `target` (a credential-free `scheme://bucket/key` identity —
never the request URL, never `store.config`), and, when the failure was a backend
HTTP rejection, `http_status` and `provider_code` parsed from the driver message:

```python
# fd.evidence == {
#     "service":       "object_store",
#     "target":        "gs://example-bucket/artifacts/apps/…/table.json",
#     "key":           "artifacts/apps/…/table.json",
#     "http_status":   400,
#     "provider_code": "PreconditionFailed",
#     …
# }
```

`http_status` and `provider_code` are **evidence, not routing** — they ride the
envelope so a consumer holding context the SDK lacks can branch on them, but the
SDK reclassifies on only two conditions (a missing Azure container, and a bucket
mid-relocation). Everything else stays a retryable `StorageError`. `target`'s
*shape* is per-producer: see the `evidence` bullet in
[cross-repo-contracts.md](../standards/cross-repo-contracts.md) before comparing
it across raise sites.

Tenant identity is intentionally absent from `FailureDetails`. Per-tenant attribution is
the consumer's responsibility (e.g., the Automation Engine attaches tenant from its own
session at ingest time).

#### Evidence keys may not be secret-named

`FailureDetails` refuses evidence keys that advertise a secret -- exact names
(`password`, `token`, `secret`, `api_key`, `private_key`, `authorization`, `auth_header`,
`cookie`) and compound suffixes (`*_password`, `*_token`, `*_secret`, so `client_secret`
and `db_password` are rejected while `object_key` and `cache_key` pass). Construction
raises `ValidationError`, so a leaf that declares such a dataclass field cannot serialise
at all:

```python
from application_sdk.errors.wire import secret_named_evidence_keys

# Ask before you build — the rejection names no keys you can act on.
bad = secret_named_evidence_keys({"host": "db.internal", "api_key": "…"})
# frozenset({'api_key'})
```

The denylist is a name check, not a value check: it cannot see a credential sitting in an
innocently-named key, or nested inside a dict or list value. Redact values yourself with
`redact_secrets` before attaching them as evidence.

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
| `WORKER_LIVENESS_MAX_IDLE_SECONDS` | `ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS` | `0` (disabled) | Idle window for the worker `/live` probe. When set to a positive number of seconds, `/live` fails once no worker activity has been recorded within that window, letting a Kubernetes `livenessProbe` recycle a worker whose Temporal poll loop has silently parked (BLDX-1552). Disabled by default because a positive window false-positives on legitimately idle queues — enable it only for continuously-busy queues, and set it larger than the longest activity that runs without heartbeating. Non-numeric, non-finite (`inf`/`nan`), or negative values fall back to `0`. See `docs/concepts/server.md` for the probe behavior. |
