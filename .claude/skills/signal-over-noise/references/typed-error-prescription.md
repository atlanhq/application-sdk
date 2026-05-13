# Typed Error Prescription Reference

Authoritative reference for "which `AppError` should I raise here?" — used by
the signal-over-noise skill when producing prescriptive fix entries for raise
sites and silent-swallow → re-raise conversions.

Source of truth: `application_sdk/errors/{base,categories,leaves,wire}.py`.
Companion for connector apps (not the SDK itself): `.claude/skills/typed-failures/SKILL.md`.

---

## §1 — Why typed errors

Every `AppError` subclass produces a `FailureDetails` wire envelope via
`AppError.to_failure_details()` (`application_sdk/errors/base.py:78-108`).
Temporal serialises that envelope into `ApplicationError.details=[…]`. The
Automation Engine reads `category`, `code`, `audience`, and `retryable` as
typed, queryable fields — it **never** parses exception strings.

Bare builtin raises (`ValueError`, `RuntimeError`, `Exception`) and legacy
`AtlanError` subclasses (`ClientError`, `IOError`, etc.) reach AE as opaque
strings. Dashboard cuts, SLA attribution, and on-call routing are all blind to
them. Typed errors are not a style preference — they are the failure-attribution
contract between the SDK and the Automation Engine.

The `FailureDetails` envelope:

```python
FailureDetails(
    category: FailureCategory,   # what happened (closed enum)
    code: str,                   # fine-grained app-owned identifier
    retryable: bool,             # should the orchestrator retry?
    audience: Audience,          # who must act: USER / PLATFORM / APP_OWNER
    message: str,
    suggested_action: str | None,
    evidence: dict[str, Any],    # leaf's dataclass fields (auto-populated)
    cause_repr: str | None,      # sanitised upstream exception string
)
```

---

## §2 — The 14 SDK leaves

All defined in `application_sdk/errors/leaves.py`. Import path:
`from application_sdk.errors import <Leaf>`.

**You may not invent new categories.** The `FailureCategory` enum is closed
(`application_sdk/errors/categories.py`). If you need a sharper distinction,
subclass a leaf and override `code` — never add a new category value.

| Leaf class | Category | Default `audience` | Default `retryable` | Raise this when… |
|---|---|---|---|---|
| `CancelledError` | CANCELLED | APP_OWNER | False | Caller / workflow signalled cancellation; not a failure |
| `AppTimeoutError` | TIMEOUT | APP_OWNER | **True** | A bounded wait elapsed (network read, activity start-to-close, heartbeat) |
| `RateLimitedError` | RATE_LIMITED | USER | **True** | Source or dependency returned 429 or per-key quota signal |
| `AuthError` | AUTH | USER | False | Credentials missing, expired, or rejected |
| `AppPermissionDeniedError` | PERMISSION | USER | False | Authenticated but not authorised for the resource or action |
| `NotFoundError` | NOT_FOUND | USER | False | Targeted entity does not exist |
| `AlreadyExistsError` | ALREADY_EXISTS | USER | False | Entity the caller tried to create already exists (idempotent-create path) |
| `InvalidInputError` | INVALID_INPUT | USER | False | Argument or payload malformed irrespective of system state |
| `PreconditionError` | PRECONDITION | USER | False | Inputs syntactically valid but system state forbids the action |
| `DependencyUnavailableError` | DEPENDENCY_UNAVAILABLE | **PLATFORM** | **True** | Required platform service down or degraded (Dapr, Temporal, object store, source DB) |
| `ResourceExhaustedError` | RESOURCE_EXHAUSTED | **PLATFORM** | **True** | Local resource limit hit (OOM, disk full, file handles, worker slots) |
| `DataIntegrityError` | DATA_INTEGRITY | APP_OWNER | False | Returned data is corrupt or violates expected invariants |
| `InternalError` | INTERNAL | APP_OWNER | False | SDK or app bug; invariant broken in our code |
| `UnimplementedError` | UNIMPLEMENTED | APP_OWNER | False | Operation not supported or capability not yet built (known gap, not a bug) |

**Special SDK subclass** — `WorkerEvictedError` (`leaves.py:175-195`) subclasses
`DependencyUnavailableError` with `code = "WORKER_EVICTED"`. This is the
precedent for when the SDK itself should subclass a leaf: when a stable wire
`code` string is needed for cross-process recognition (Temporal serialises only
the type string, not the Python class). Do not add further SDK subclasses without
this specific need.

**`audience` override pattern** — when an SDK base default doesn't fit the locus,
override `audience` on a minimal subclass. Example from the SDK itself:
`WorkerEvictedError` inherits PLATFORM from `DependencyUnavailableError` and
keeps it (correct — the pod is platform infrastructure). For connector apps, the
source-side network is `audience=USER` (the customer controls it), so they
override in their `failures.py`. The SDK's own code should follow the same logic.

---

## §3 — Litmus tests for ambiguous categories

Use these in order. Pick the first rule that applies.

**`DEPENDENCY_UNAVAILABLE` vs `PRECONDITION`**
If retrying *the same call* without any state change is expected to succeed →
`DependencyUnavailableError`. If explicit state must change before the call can
succeed (schema must be updated, version must match, entity must be in a
different state) → `PreconditionError`.

**`RATE_LIMITED` vs `RESOURCE_EXHAUSTED`**
429 from a remote endpoint (per-key quota signal) → `RateLimitedError`.
Local resource limit (OOM, disk full, file handles, worker-slot exhaustion) →
`ResourceExhaustedError`.

**`ALREADY_EXISTS` vs `PRECONDITION`**
Entity already exists on an idempotent-create path → `AlreadyExistsError`.
Resource exists but is in the wrong *state* for the operation →
`PreconditionError`.

**`UNIMPLEMENTED` vs `INTERNAL`**
Known capability gap (feature not yet built, dialect not yet supported) →
`UnimplementedError`. Unexpected invariant violation (our code has a bug) →
`InternalError`.

**`INVALID_INPUT` vs `PRECONDITION`**
Payload itself is malformed irrespective of system state (missing required
field, wrong type, out-of-range value) → `InvalidInputError`. Payload
syntactically valid but system state blocks the operation → `PreconditionError`.

**`AUTH` vs `PERMISSION`**
Identity cannot be established (credentials missing, expired, or invalid;
authentication handshake failed) → `AuthError`. Identity established but the
principal lacks permission for the resource or action → `AppPermissionDeniedError`.

---

## §4 — SDK raise-context → leaf cookbook

Common situations in `application_sdk/`. Each entry gives a worked example.

Each entry includes a **Code:** annotation showing the suggested wire code for that
scenario. `code` is a `ClassVar[str]` on the leaf class — it cannot be passed to the
constructor, and inline raise examples emit the leaf's own default code (e.g.
`InvalidInputError` emits `INVALID_INPUT`). To use the scenario-specific code shown in
the annotation, subclass the leaf and override the `ClassVar`:

```python
@dataclass(kw_only=True)
class EngineNotInitializedError(InternalError):
    code: ClassVar[str] = "INTERNAL_ENGINE_NOT_INITIALIZED"
```

If the leaf's default code is adequate for triage, use the leaf directly without
subclassing. Subclass only when a stable, recognisable wire code is required for
cross-process routing or dashboard attribution.

**Code naming rule**: always start with the category prefix
(`AUTH_`, `INTERNAL_`, `DEPENDENCY_UNAVAILABLE_`, etc.) so the code is
self-describing without joining against the category column. No vendor name in
the code (vendor identity lives in `evidence`). Generic subtypes reused across
callers are better than hyper-specific ones.

### When to bake defaults into a subclass

If the same leaf will be raised with the same message and evidence at ≥2 call
sites, encode those defaults on the subclass itself so each raise site collapses
to one line. Raise sites pass only what is genuinely dynamic (typically
`cause=` or a per-site message that names a specific field). Use
`@dataclass(kw_only=True)` so the subclass can provide defaults for parent
default-less fields like `message: str` (Python 3.10+).

```python
# Subclass — typically in a sibling module (see WorkerEvictedError precedent).
@dataclass(kw_only=True)
class EngineNotInitializedError(InternalError):
    code: ClassVar[str] = "INTERNAL_ENGINE_NOT_INITIALIZED"
    message: str = "Engine is not initialized. Call load() first."
    component: str | None = "sql_client"
    invariant: str | None = "load_before_use"

# Raise sites — minimal.
raise EngineNotInitializedError()
```

Raise inline (no subclass) only when the leaf's default code is adequate and
the site appears once; the cookbook entries below show the inline form.

---

### Required field / config missing

```python
# Situation: raise ValueError("X is required")
raise InvalidInputError(
    message="aws_role_arn is required",
    field="aws_role_arn",
)
```
Code: `INVALID_INPUT_MISSING_FIELD`. Use `field=` evidence to name the param.

---

### SDK invariant violated (caller misused the API)

```python
# Situation: "Engine is not initialized. Call load() first."
raise InternalError(
    message="Engine is not initialized. Call load() first.",
    component="sql_client",
    invariant="load_before_use",
)
```
Code: `INTERNAL_ENGINE_NOT_INITIALIZED`. Audience: APP_OWNER.
`component=` names the SDK layer; `invariant=` is a short stable key suitable
for runbook lookup.

---

### Credentials missing or invalid (no HTTP response)

```python
raise AuthError(
    message="No credentials found for connection",
    auth_method="connection_config",
    failure_reason="credentials_absent",
)
```
Code: `AUTH_MISSING_CREDENTIALS`. Audience: USER (default — correct).

---

### 401 from source (rejected by remote)

```python
except HTTPError as exc:
    raise AuthError(
        message="Source rejected credentials",
        auth_method="api_key",
        failure_reason="http_401",
        cause=exc,
    ) from exc
```
Code: `AUTH_REJECTED`. Set `cause=exc` + `from exc` (see §6).

---

### 403 from source

```python
except SomeSourceError as exc:
    raise AppPermissionDeniedError(
        message="Permission denied by source",
        resource=endpoint,
        required_action="read",
        cause=exc,
    ) from exc
```
Code: `PERMISSION_DENIED`. Audience: USER (default — correct).

---

### 404 from source

```python
except SomeSourceError as exc:
    raise NotFoundError(
        message=f"Resource not found: {resource_id}",
        resource_type="table",
        resource_identifier=resource_id,
        cause=exc,
    ) from exc
```
Code: `NOT_FOUND_RESOURCE`. Evidence: `resource_type=` and `resource_identifier=`.

---

### 409 / idempotent-create conflict

```python
raise AlreadyExistsError(
    message=f"Entity already exists: {entity_id}",
    resource_type="connection",
    resource_identifier=entity_id,
)
```
Code: `ALREADY_EXISTS_RESOURCE`.

---

### 429 / quota exceeded

```python
raise RateLimitedError(
    message="API quota exceeded",
    limit_type="requests_per_minute",
    retry_after_seconds=float(retry_after) if retry_after else None,
    cause=exc,
) from exc
```
Code: `RATE_LIMITED_API`. Set `retry_after_seconds=` from response header when
available.

---

### Network unreachable / 5xx from source

```python
raise DependencyUnavailableError(
    message="Source database unreachable",
    service="source_db",
    target=host,
    network_error=str(exc),
    cause=exc,
) from exc
```
Code: `DEPENDENCY_UNAVAILABLE_NETWORK`. Audience: default PLATFORM.
**Override to USER** when the unreachable host is the customer's own source
(their firewall / VPC). Add `audience: ClassVar[Audience] = Audience.USER`
on a minimal subclass if doing this consistently.

---

### Dapr / Temporal / object-store outage

```python
raise DependencyUnavailableError(
    message="Dapr state store unavailable",
    service="dapr_state_store",
    network_error=str(exc),
    cause=exc,
) from exc
```
Code: `DEPENDENCY_UNAVAILABLE_DAPR` / `DEPENDENCY_UNAVAILABLE_TEMPORAL` /
`DEPENDENCY_UNAVAILABLE_OBJECT_STORE`. Audience: PLATFORM (default — correct).

---

### Schema mismatch / version conflict

```python
raise PreconditionError(
    message="Schema version mismatch; re-run migration",
    resource="output_schema",
    expected_state="v2",
    actual_state="v1",
)
```
Code: `PRECONDITION_SCHEMA_MISMATCH`.

---

### Bounded-wait elapsed (timeout)

```python
raise AppTimeoutError(
    message="Query execution exceeded timeout",
    operation="sql_query",
    timeout_seconds=30.0,
    cause=exc,
) from exc
```
Code: `TIMEOUT_QUERY`. Set `operation=` and `timeout_seconds=` from the
configured limit.

---

### Local OOM / disk full / handle exhaustion

```python
raise ResourceExhaustedError(
    message="Worker ran out of memory processing batch",
    resource="heap_memory",
    limit="container_limit",
    observed=str(exc),
    cause=exc,
) from exc
```
Code: `RESOURCE_EXHAUSTED_MEMORY`. Audience: PLATFORM (default — correct).

---

### Returned data corrupt / invariant violated

```python
raise DataIntegrityError(
    message="Row count mismatch after transform",
    expectation="row_count == source_count",
    observed=f"{actual} rows vs {expected} expected",
    location="transformer:finalize",
)
```
Code: `DATA_INTEGRITY_ROW_COUNT_MISMATCH`. Audience: APP_OWNER (default).

---

### Known capability gap

```python
raise UnimplementedError(
    message="Cursor type 'server-side' not yet supported",
    operation="sql_fetch_cursor",
    reason="server_side_cursor_not_implemented",
)
```
Code: `UNIMPLEMENTED_CURSOR_TYPE`. Audience: APP_OWNER. Use this — **not**
`InternalError` — for known feature gaps so on-call is not paged for an
expected absence.

---

### Workflow / caller cancellation

Rarely raised manually — usually propagated from Temporal. If the SDK raises it:

```python
raise CancelledError(
    message="Activity cancelled by workflow signal",
    cancelled_by="workflow",
    reason="graceful_shutdown",
)
```
Code: `CANCELLED`.

---

## §5 — Exhaustive legacy `AtlanError` → `AppError` migration table

Full mapping of every constant in
`application_sdk/common/error_codes.py`. Use this as a deterministic lookup
when converting P13 (legacy `AtlanError`) raise sites.

Columns: `legacy constant` | `target leaf` | `suggested code` |
`audience override?` | `notes`

### `ClientError` → (`application_sdk/common/error_codes.py:66-105`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `REQUEST_VALIDATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_REQUEST_VALIDATION` | — | set `constraint=` with validation details |
| `INPUT_VALIDATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_VALIDATION` | — | set `field=` if field-level |
| `CLIENT_AUTH_ERROR` | `AuthError` | `AUTH_CLIENT_FAILED` | — | set `auth_method=` |
| `HANDLER_AUTH_ERROR` | `AuthError` | `AUTH_HANDLER_FAILED` | — | HTTP handler auth failure |
| `SQL_CLIENT_AUTH_ERROR` | `AuthError` | `AUTH_SQL_CLIENT_FAILED` | — | set `auth_method="sql"` |
| `AUTH_TOKEN_REFRESH_ERROR` | `AuthError` | `AUTH_TOKEN_REFRESH_FAILED` | — | set `failure_reason="token_refresh"` |
| `AUTH_CREDENTIALS_ERROR` | `AuthError` | `AUTH_CREDENTIALS_NOT_FOUND` | — | set `failure_reason="credentials_absent"` |
| `AUTH_CONFIG_ERROR` | `InvalidInputError` | `INVALID_INPUT_AUTH_CONFIG` | — | configuration error, not a credentials error |
| `REDIS_CONNECTION_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_REDIS` | — | set `service="redis"` |
| `REDIS_TIMEOUT_ERROR` | `AppTimeoutError` | `TIMEOUT_REDIS` | — | set `operation="redis_op"` |
| `REDIS_AUTH_ERROR` | `AuthError` | `AUTH_REDIS_FAILED` | — | set `auth_method="redis_auth"` |
| `REDIS_PROTOCOL_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_REDIS_PROTOCOL` | — | set `service="redis"`, `network_error=` |

### `ApiError` → (`application_sdk/common/error_codes.py:107-144`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `SERVER_START_ERROR` | `InternalError` | `INTERNAL_SERVER_START` | — | server startup invariant |
| `SERVER_SHUTDOWN_ERROR` | `InternalError` | `INTERNAL_SERVER_SHUTDOWN` | — | |
| `SERVER_CONFIG_ERROR` | `InvalidInputError` | `INVALID_INPUT_SERVER_CONFIG` | — | bad configuration supplied |
| `CONFIGURATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_CONFIG` | — | general config error |
| `LOGGER_SETUP_ERROR` | `InternalError` | `INTERNAL_LOGGER_SETUP` | — | observability setup invariant |
| `LOGGER_PROCESSING_ERROR` | `InternalError` | `INTERNAL_LOGGER_PROCESSING` | — | |
| `LOGGER_OTLP_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OTLP` | — | OTLP collector unreachable |
| `LOGGER_RESOURCE_ERROR` | `ResourceExhaustedError` | `RESOURCE_EXHAUSTED_LOGGER` | — | |
| `UNKNOWN_ERROR` | `InternalError` | `INTERNAL_UNKNOWN` | — | set `classification_pending=True` pending triage |
| `SQL_FILE_ERROR` | `InternalError` | `INTERNAL_SQL_FILE` | — | SQL template / file read failure |
| `ENDPOINT_ERROR` | `InternalError` | `INTERNAL_ENDPOINT` | — | HTTP endpoint setup/dispatch |
| `EVENT_TRIGGER_ERROR` | `InternalError` | `INTERNAL_EVENT_TRIGGER` | — | |
| `MIDDLEWARE_ERROR` | `InternalError` | `INTERNAL_MIDDLEWARE` | — | |
| `ROUTE_HANDLER_ERROR` | `InternalError` | `INTERNAL_ROUTE_HANDLER` | — | |
| `LOG_MIDDLEWARE_ERROR` | `InternalError` | `INTERNAL_LOG_MIDDLEWARE` | — | |

### `OrchestratorError` → (`application_sdk/common/error_codes.py:147-159`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `ORCHESTRATOR_CLIENT_CONNECTION_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL` | — | set `service="temporal"` |
| `ORCHESTRATOR_CLIENT_ACTIVITY_ERROR` | `InternalError` | `INTERNAL_ORCHESTRATOR_ACTIVITY` | — | activity dispatch failure |
| `ORCHESTRATOR_CLIENT_WORKER_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL_WORKER` | — | set `service="temporal_worker"` |

### `WorkflowError` → (`application_sdk/common/error_codes.py:161-188`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `WORKFLOW_EXECUTION_ERROR` | `InternalError` | `INTERNAL_WORKFLOW_EXECUTION` | — | unexpected workflow failure |
| `WORKFLOW_CONFIG_ERROR` | `InvalidInputError` | `INVALID_INPUT_WORKFLOW_CONFIG` | — | bad workflow configuration |
| `WORKFLOW_VALIDATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_WORKFLOW_VALIDATION` | — | set `constraint=` with validation detail |
| `WORKFLOW_CLIENT_START_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL_START` | — | set `service="temporal"` |
| `WORKFLOW_CLIENT_STOP_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL_STOP` | — | set `service="temporal"` |
| `WORKFLOW_CLIENT_STATUS_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL_STATUS` | — | set `service="temporal"` |
| `WORKFLOW_CLIENT_WORKER_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_TEMPORAL_WORKER` | — | set `service="temporal_worker"` |
| `WORKFLOW_CLIENT_NOT_FOUND_ERROR` | `NotFoundError` | `NOT_FOUND_WORKFLOW` | — | set `resource_type="workflow"` |

### `IOError` → (`application_sdk/common/error_codes.py:190-277`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `INPUT_ERROR` | `InvalidInputError` | `INVALID_INPUT_IO` | — | generic IO input error |
| `INPUT_LOAD_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_INPUT_LOAD` | — | source load failure (retryable) |
| `INPUT_PROCESSING_ERROR` | `InternalError` | `INTERNAL_INPUT_PROCESSING` | — | processing invariant |
| `SQL_QUERY_ERROR` | `InvalidInputError` | `INVALID_INPUT_SQL_QUERY` | USER | bad query from caller |
| `SQL_QUERY_BATCH_ERROR` | `InternalError` | `INTERNAL_SQL_BATCH` | — | SDK batch execution bug |
| `SQL_QUERY_PANDAS_ERROR` | `InternalError` | `INTERNAL_SQL_PANDAS` | — | |
| `SQL_QUERY_DAFT_ERROR` | `InternalError` | `INTERNAL_SQL_DAFT` | — | Daft engine invariant |
| `JSON_READ_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_JSON_READ` | — | malformed JSON on read |
| `JSON_BATCH_ERROR` | `InternalError` | `INTERNAL_JSON_BATCH` | — | |
| `JSON_DAFT_ERROR` | `InternalError` | `INTERNAL_JSON_DAFT` | — | |
| `JSON_DOWNLOAD_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_JSON_DOWNLOAD` | — | download from object store |
| `PARQUET_READ_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_PARQUET_READ` | — | corrupt/malformed Parquet |
| `PARQUET_BATCH_ERROR` | `InternalError` | `INTERNAL_PARQUET_BATCH` | — | |
| `PARQUET_DAFT_ERROR` | `InternalError` | `INTERNAL_PARQUET_DAFT` | — | |
| `PARQUET_VALIDATION_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_PARQUET_VALIDATION` | — | schema / row validation |
| `ICEBERG_READ_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_ICEBERG_READ` | — | |
| `ICEBERG_DAFT_ERROR` | `InternalError` | `INTERNAL_ICEBERG_DAFT` | — | |
| `ICEBERG_TABLE_ERROR` | `PreconditionError` | `PRECONDITION_ICEBERG_TABLE` | — | table state blocks operation |
| `OBJECT_STORE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OBJECT_STORE` | — | set `service="object_store"` |
| `OBJECT_STORE_DOWNLOAD_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OBJECT_STORE_DOWNLOAD` | — | set `service="object_store"` |
| `OBJECT_STORE_READ_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OBJECT_STORE_READ` | — | set `service="object_store"` |
| `STATE_STORE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_STATE_STORE` | — | set `service="dapr_state_store"` |
| `STATE_STORE_EXTRACT_ERROR` | `InternalError` | `INTERNAL_STATE_STORE_EXTRACT` | — | extraction-phase invariant |
| `STATE_STORE_VALIDATION_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_STATE_STORE` | — | state data corrupt |
| `OUTPUT_ERROR` | `InternalError` | `INTERNAL_OUTPUT` | — | generic output failure |
| `OUTPUT_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OUTPUT_WRITE` | — | write destination unavailable |
| `OUTPUT_STATISTICS_ERROR` | `InternalError` | `INTERNAL_OUTPUT_STATISTICS` | — | stats computation |
| `OUTPUT_VALIDATION_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_OUTPUT_VALIDATION` | — | output data fails validation |
| `JSON_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_JSON_WRITE` | — | write destination |
| `JSON_BATCH_WRITE_ERROR` | `InternalError` | `INTERNAL_JSON_BATCH_WRITE` | — | |
| `JSON_DAFT_WRITE_ERROR` | `InternalError` | `INTERNAL_JSON_DAFT_WRITE` | — | |
| `PARQUET_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_PARQUET_WRITE` | — | |
| `PARQUET_DAFT_WRITE_ERROR` | `InternalError` | `INTERNAL_PARQUET_DAFT_WRITE` | — | |
| `ICEBERG_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_ICEBERG_WRITE` | — | |
| `ICEBERG_DAFT_WRITE_ERROR` | `InternalError` | `INTERNAL_ICEBERG_DAFT_WRITE` | — | |
| `ICEBERG_TABLE_ERROR_OUT` | `PreconditionError` | `PRECONDITION_ICEBERG_TABLE_OUT` | — | table state blocks write |
| `OBJECT_STORE_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_OBJECT_STORE_WRITE` | — | set `service="object_store"` |
| `STATE_STORE_WRITE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_STATE_STORE_WRITE` | — | set `service="dapr_state_store"` |

### `CommonError` → (`application_sdk/common/error_codes.py:279-307`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `AWS_REGION_ERROR` | `InvalidInputError` | `INVALID_INPUT_AWS_REGION` | — | bad or missing region config |
| `AWS_ROLE_ERROR` | `AppPermissionDeniedError` | `PERMISSION_AWS_ROLE` | — | IAM assume-role denied |
| `AWS_CREDENTIALS_ERROR` | `AuthError` | `AUTH_AWS_CREDENTIALS` | — | set `auth_method="aws_iam"` |
| `AWS_TOKEN_ERROR` | `AuthError` | `AUTH_AWS_TOKEN` | — | STS token error |
| `QUERY_PREPARATION_ERROR` | `InternalError` | `INTERNAL_QUERY_PREP` | — | query templating / preparation |
| `FILTER_PREPARATION_ERROR` | `InternalError` | `INTERNAL_FILTER_PREP` | — | filter expression build |
| `CREDENTIALS_PARSE_ERROR` | `InvalidInputError` | `INVALID_INPUT_CREDENTIALS_PARSE` | — | malformed credentials config |
| `CREDENTIALS_RESOLUTION_ERROR` | `AuthError` | `AUTH_CREDENTIALS_RESOLUTION` | — | Dapr secret store / vault |
| `AZURE_CREDENTIAL_ERROR` | `AuthError` | `AUTH_AZURE_CREDENTIAL` | — | set `auth_method="azure"` |
| `AZURE_CONNECTION_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_AZURE` | — | set `service="azure"` |
| `AZURE_SERVICE_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_AZURE_SERVICE` | — | Azure API failure |

### `DocGenError` → (`application_sdk/common/error_codes.py:311-358`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `DOCGEN_ERROR` | `InternalError` | `INTERNAL_DOCGEN` | — | generic docgen failure |
| `DOCGEN_EXPORT_ERROR` | `InternalError` | `INTERNAL_DOCGEN_EXPORT` | — | |
| `DOCGEN_BUILD_ERROR` | `InternalError` | `INTERNAL_DOCGEN_BUILD` | — | |
| `MANIFEST_NOT_FOUND_ERROR` | `NotFoundError` | `NOT_FOUND_MANIFEST` | — | set `resource_type="manifest"` |
| `MANIFEST_PARSE_ERROR` | `InvalidInputError` | `INVALID_INPUT_MANIFEST_PARSE` | — | malformed manifest YAML/JSON |
| `MANIFEST_VALIDATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_MANIFEST_VALIDATION` | — | set `constraint=` |
| `MANIFEST_YAML_ERROR` | `InvalidInputError` | `INVALID_INPUT_MANIFEST_YAML` | — | YAML parse error |
| `DIRECTORY_VALIDATION_ERROR` | `InvalidInputError` | `INVALID_INPUT_DIRECTORY_VALIDATION` | — | |
| `DIRECTORY_CONTENT_ERROR` | `InvalidInputError` | `INVALID_INPUT_DIRECTORY_CONTENT` | — | |
| `DIRECTORY_STRUCTURE_ERROR` | `InvalidInputError` | `INVALID_INPUT_DIRECTORY_STRUCTURE` | — | |
| `DIRECTORY_FILE_ERROR` | `InvalidInputError` | `INVALID_INPUT_DIRECTORY_FILE` | — | missing / malformed file |
| `MKDOCS_CONFIG_ERROR` | `InvalidInputError` | `INVALID_INPUT_MKDOCS_CONFIG` | — | |
| `MKDOCS_EXPORT_ERROR` | `InternalError` | `INTERNAL_MKDOCS_EXPORT` | — | MkDocs export subprocess |
| `MKDOCS_NAV_ERROR` | `InternalError` | `INTERNAL_MKDOCS_NAV` | — | navigation generation |
| `MKDOCS_BUILD_ERROR` | `InternalError` | `INTERNAL_MKDOCS_BUILD` | — | |

### `ActivityError` → (`application_sdk/common/error_codes.py:361-409`)

| Legacy constant | Target leaf | Suggested code | Audience override? | Notes |
|---|---|---|---|---|
| `ACTIVITY_START_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_ACTIVITY_START` | — | Temporal activity start failure |
| `ACTIVITY_END_ERROR` | `InternalError` | `INTERNAL_ACTIVITY_END` | — | activity teardown invariant |
| `QUERY_EXTRACTION_ERROR` | `InternalError` | `INTERNAL_QUERY_EXTRACTION` | — | query extraction activity |
| `QUERY_EXTRACTION_SQL_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_QUERY_SQL` | — | SQL source unreachable during extraction |
| `QUERY_EXTRACTION_PARSE_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_QUERY_EXTRACTION_PARSE` | — | returned data unparseable |
| `QUERY_EXTRACTION_VALIDATION_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_QUERY_EXTRACTION_VALIDATION` | — | extracted data fails schema check |
| `METADATA_EXTRACTION_ERROR` | `InternalError` | `INTERNAL_METADATA_EXTRACTION` | — | generic extraction failure |
| `METADATA_EXTRACTION_SQL_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_METADATA_SQL` | — | SQL source unreachable |
| `METADATA_EXTRACTION_REST_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_METADATA_REST` | — | REST source unreachable |
| `METADATA_EXTRACTION_PARSE_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_METADATA_EXTRACTION_PARSE` | — | |
| `METADATA_EXTRACTION_VALIDATION_ERROR` | `DataIntegrityError` | `DATA_INTEGRITY_METADATA_EXTRACTION_VALIDATION` | — | |
| `ATLAN_UPLOAD_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_ATLAN_UPLOAD` | — | set `service="atlan_api"` |
| `LOCK_ACQUISITION_ERROR` | `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE_LOCK_ACQUISITION` | — | set `service="dapr_lock"` |
| `LOCK_RELEASE_ERROR` | `InternalError` | `INTERNAL_LOCK_RELEASE` | — | lock release invariant |
| `LOCK_TIMEOUT_ERROR` | `AppTimeoutError` | `TIMEOUT_LOCK_ACQUISITION` | — | set `operation="lock_acquire"` |

---

## §6 — Mandatory raise-site rules

These rules apply to every raise site in `application_sdk/` and in connector
apps. Enforce them whenever creating or reviewing a raise.

**`message=` is always required.** Set it to the same human-readable text the
legacy raise used. Don't re-engineer the message during a migration — preserve
it verbatim. The message is visible in logs, AE, and Temporal history.

**`cause=exc` AND `raise typed from exc` when wrapping.** Always apply both.
They serve different consumers:
- `cause=exc` → populates `FailureDetails.cause_repr` (sanitised, length-capped
  string in the wire envelope; visible to AE dashboards).
- `from exc` → sets Python's `__cause__` (preserves the in-process traceback
  for `exc_info=True` in logs and local debugging).

```python
# Correct — both bindings
except SomeSourceError as exc:
    raise DependencyUnavailableError(
        message="Source unreachable",
        service="source_db",
        cause=exc,
    ) from exc
```

**Never set `app_name` or `run_id` from inside the SDK.**
- `app_name` — AE provides via DAG node label (single source of truth).
- `run_id` — AE attaches from Temporal context at ingest time.

**`suggested_action`** — may be set at a raise site when the call site has specific
user-facing guidance (e.g. "regrant Glue read access on the IAM role"). Omit when no
specific action can be prescribed. Never default it on the class definition.

**Evidence fields.** Add dataclass fields when the call site has obvious context
that aids debugging. Use the leaf's existing fields first (`field` on
`InvalidInputError`, `service`/`target` on `DependencyUnavailableError`, etc.).
Add a subclass field only when the leaf's existing fields are insufficient.
Match the restraint in the Athena reference.

**Secret-named evidence keys are rejected by the wire layer.** `wire.py:67-84`
blocks any evidence key matching the exact denylist or the suffix denylist
(`_secret`, `_password`, `_token`). Use a safe proxy name (e.g.
`credential_name` instead of `credential_token`) or omit.

---

## §7 — SDK vs app subclassing rule

**Connector apps** build `app/failures.py` with their own subclasses using
the `/typed-failures` skill. Every connector app's typed classes live there,
not in the SDK.

**The SDK** raises leaves directly in most cases. The exception: when a stable
wire `code` string is needed for *cross-process* recognition across the
activity/workflow boundary — `WorkerEvictedError` is the only current example
(`leaves.py:172`, `code = "WORKER_EVICTED"`). The wire type string lets
workflow code identify the failure without depending on the Python class name.

If you are tempted to subclass a leaf in the SDK:
1. Is there a cross-process recognition need? If not, raise the leaf directly
   using the leaf's default `code`. If a specific `code` string is needed,
   subclass and override the `ClassVar` — `code` cannot be set at the raise site.
2. Does the new subclass bake repeated message+evidence defaults (≥2 call sites)?
   If yes, that also justifies a subclass (see §4 "When to bake defaults").
3. Does the new subclass add evidence fields? If yes, that justifies a subclass.
4. Otherwise: raise the existing leaf directly.

---

## §8 — Surface or swallow? Decision tree

When surface mode identifies a silent-swallow pattern and the right fix might
involve re-raising (FT-1b, FT-3b, FT-5b), use this decision tree:

```
Does the caller depend on the operation succeeding?
  YES → typed re-raise (FT-1b / FT-3b / FT-5b)
  NO  ↓
Is the exception from an activity body that reaches AE?
  YES → typed re-raise — failure attribution depends on it
  NO  ↓
Does the surrounding code produce a visibly wrong result
on failure (returns empty/None/False that callers trust)?
  YES → typed re-raise
  NO  ↓
Is this a module-load / __init__ site?
  YES → typed re-raise (deferred crashes are worse)
  NO  ↓
Is this genuinely best-effort cleanup (cache invalidation,
stats flush, optional side-effect)?
  YES → log-and-continue (FT-1a / FT-3a / FT-5a)
        log at DEBUG if failure is expected; WARNING if unexpected
  NO  → typed re-raise (default — lean toward surfacing)
```

When uncertain, lean toward typing and surfacing. A `classification_pending=True`
`InternalError` is better than a silent swallow.
