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
│   │   └── InvalidInputValueError  INVALID_INPUT (INVALID_INPUT_VALUE), also a builtin ValueError  retryable=False  audience=USER
│   ├── PreconditionError      PRECONDITION               retryable=False  audience=USER
│   ├── RateLimitedError       RATE_LIMITED               retryable=True   audience=USER
│   ├── DependencyUnavailableError  DEPENDENCY_UNAVAILABLE retryable=True  audience=PLATFORM
│   ├── SourceUnavailableError   SOURCE_UNAVAILABLE        retryable=True   audience=USER
│   ├── ResourceExhaustedError RESOURCE_EXHAUSTED         retryable=True   audience=PLATFORM
│   │   └── DiskFullError      RESOURCE_EXHAUSTED (RESOURCE_EXHAUSTED_DISK_FULL)  retryable=True   audience=PLATFORM
│   │   └── LocalVolumeUnwritableError  RESOURCE_EXHAUSTED (RESOURCE_EXHAUSTED_VOLUME_UNWRITABLE)  retryable=True   audience=PLATFORM
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

### StorageGatewayAuthUnavailableError — a 401 that is not about your credentials

`StorageGatewayAuthUnavailableError(StorageError)` carries
`DEPENDENCY_UNAVAILABLE_STORAGE_GATEWAY_AUTH` / `AAF-STR-010` instead of the generic
`AAF-STR-004`, for one narrow condition: Atlan's `/api/blobstorage` proxy verifies the SigV4
signature by looking the signing key's secret up in Keycloak on **every** request, and maps every
failure of that lookup — including its own 30-second timeout — onto
`401 {"code": 1005, "error": "Invalid Client"}`.

The leaf matches on both halves of that pair, so a 401 from any other store, or a `1005` on any
other status, still falls through to the generic `StorageError`.

It applies to reads as well as writes. Every store operation that can fail against a remote —
`upload_file`, `put`, `download_file`, `exists`, `get_file_meta`, `_get_bytes`, `delete`,
`list_keys` — now routes its non-not-found failures through the same classifier, so all of them
carry `http_status` / `provider_code` / `target` too. That matters for more than tidiness: a
`verify_refs` HEAD goes through `exists()`, and a HEAD was what the run that motivated this leaf
finally died on. The not-found contracts are unchanged — `exists` and `delete` still return
`False`, `get_file_meta` and `_get_bytes` still return `None` — because the classifier sits after
that short-circuit.

Why the distinction earns a code: the signing key is a *static* Keycloak client id/secret, so a
genuinely wrong credential fails the very first request a deployment makes — the SDR preflight
probe at startup, long before any artifact moves. A `1005` arriving mid-run, after that probe
passed, is the gateway being unavailable, and no credential change fixes it. Its
`suggested_action` says so explicitly, because the generic wording ("lacked valid authentication
credentials") sends an operator to rotate credentials that are working.

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

### InvalidInputValueError — a compatibility shim, not a leaf to reach for

`InvalidInputValueError(InvalidInputError, ValueError)` exists for one job: typing a public SDK
entry point whose documented contract was already a bare `ValueError`. `ValueError` stays in the
bases, so `except ValueError:` in an app keeps catching the failure while the raise now carries a
typed `INVALID_INPUT` / `USER` envelope. Its own code, `INVALID_INPUT_VALUE`, keeps the shim
countable apart from the bare leaf — a non-zero rate on it measures how much still depends on the
builtin contract, which is what decides when it can be retired.

**Do not use it for new APIs.** A new entry point has no `ValueError` contract to preserve, so
raise plain `InvalidInputError` and let callers catch the typed hierarchy. Copying the shim onto
new code spreads the builtin dependency this class exists to contain.

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
# fd.message       — str (the human line; URL userinfo of any credential shape (Azure blob `container@account` addressing excepted) and secret-named params are redacted by the envelope's own validator, at construction and again on model_validate)
# fd.suggested_action — str | None (imperative hint; voice shifts with audience; redacted the same way as message)
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
innocently-named key, or nested inside a dict or list value. Redact values yourself before
attaching them as evidence — `redact_secrets` for a single string, `redact_wire_value` for
anything with structure:

```python
from application_sdk.errors import redact_wire_value

# Walks dicts, lists, tuples and sets to any depth, redacting every string it
# reaches and leaving the shape alone. Cycle- and depth-guarded, so a hostile
# structure truncates rather than hanging the worker.
safe = redact_wire_value({"dsn": "postgresql://u:p@host/db", "tags": ["pwd=hunter2"]})
# {'dsn': 'postgresql://***@host/db', 'tags': ['pwd=***']}
```

Use it on anything handler-authored that goes under an `evidence` key: nested values are not
redacted where they are built. `message` and `suggested_action` **are** — a `field_validator` on
`FailureDetails` runs `redact_secrets` over both at construction and again on `model_validate`,
idempotently, so a handler gains nothing by redacting them first. `cause_repr` is redacted where the
cause is captured, by `sanitize_cause_repr`.

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

## Asset Serialisation

`application_sdk.common.asset_serialization.entity_bytes()` is the single seam that turns an asset-mapper return value into one Atlas wire-shape JSONL record. Every template that runs the `map_<entity>()` pattern calls it, so a connector never serialises its own assets.

```python
from application_sdk.common.asset_serialization import entity_bytes

line = entity_bytes(
    asset,
    connection_name="My MySQL",
    last_sync=last_sync,       # see "Framework-injected attributes" below
    entity_type="table",
)
```

**What it accepts**, in dispatch order:

| Shape | Protocol | Notes |
|-------|----------|-------|
| `to_nested_bytes()` | `NestedBytesAsset` | `pyatlan_v9` assets. Under the `PYATLAN` envelope, passed through byte-for-byte; under the default flattened envelope the asset goes through pyatlan's own `to_atlas_format` instead (see [Entity envelope](#entity-envelope)). |
| `to_nested_dict()` | `NestedDictAsset` | Serialised with the shared `orjson_default` (`Decimal` → float, `bytes` → text). |
| `model_dump()` | `ModelDumpAsset` | pyatlan v1 / pydantic assets. Last of the object shapes: `model_dump()` yields the model's own field names, which for a snake_case model is *not* the Atlas wire shape, so an asset exposing a nested encoder as well is serialised through that instead. |
| `dict` | — | Already in the Atlas wire shape. |

Anything else raises `UnserializableMapperResultError`. There is deliberately no fallback: the branch this replaced wrote the *raw source record* when no shape matched, so a mapper returning a `pyatlan_v9` asset — the type `map_<entity>()` is annotated to return — published unmapped source rows as entities while the run reported SUCCESS (FND-2056).

**Every failure is that one typed error**, non-retryable and attributed to `APP_OWNER`, with a message naming the offending type and the entity:

| Failure | `observed` |
|---------|-----------|
| The result matches no supported shape | The returned type |
| A supported shape holds a value neither orjson nor `orjson_default` can render | The *nested* value's type |
| `to_nested_bytes()` returned JSON spanning more than one line | The asset's type |

The last one guards a public protocol rather than a live bug — `pyatlan_v9`'s encoder is compact — but `NestedBytesAsset` is exported, and the caller writes the returned bytes verbatim plus one newline. A pretty-printing implementer would therefore split one entity across several JSONL records: well-formed lines, wrong count, no error. That is the same silent, count-passing damage this seam exists to remove, so it is refused rather than trusted.

### Framework-injected attributes

Two groups of attributes are stamped by the seam, before the dispatch, on the asset itself rather than on a serialised dict afterwards — one of the accepted shapes (`to_nested_bytes()`) never produces a dict to patch.

| Attribute(s) | Source | Who wins on a conflict |
|---|---|---|
| `connectionName` | The `connection_name` argument | The mapper. A value it set explicitly is kept. |
| `lastSyncRun`, `lastSyncWorkflowName`, `lastSyncRunAt` | The `last_sync` argument | The framework. A resolved value overwrites whatever the mapper set. |

Both exist for the same reason: the mapper is handed a source record and a connection *qualified* name, and nothing else, so anything resolved from run context has to be injected by the framework. One seam beats a copy in every connector, which is how these ended up missing or wrong in the first place.

They differ on who wins because they are different kinds of value. `connectionName` is asset content the mapper may legitimately know better. The three `lastSync*` attributes are *run identity* the mapper structurally cannot resolve: a connector that tries reaches for the workflow id it was handed, which is the **child** workflow's Temporal id, not the AE-dispatched run — so the value lands on the asset looking right and is not clickable back to the run that produced it (FND-2097, BLDX-1229). One exception, inherited from the primitive: an empty resolved `run` or `workflow_name` is never written, so outside Temporal (CLI tools, tests) a hand-set value survives rather than being blanked.

If the asset declares the attribute but refuses assignment (frozen, or a property with no setter), the value is dropped rather than failing the transform — every asset type that genuinely needs these exposes settable fields.

#### Placeholder `guid` removal

The seam also *removes* one value. pyatlan's `.creator()` methods are wrapped in `@init_guid`, which sets `guid` to a fresh random negative integer on every call. That placeholder means "not yet persisted" to pyatlan's own client; the SDK never saves through that client, so written out it is only noise that changes every run. `atlan-publish-app` hashes the whole entity, so a changing `guid` classifies every entity as DIFF instead of SYNCED and forces a per-entity diff that finds nothing (FND-2720).

`entity_bytes()` therefore clears a placeholder `guid` (a negative-integer string) before the dispatch: set to `UNSET` on a `pyatlan_v9` asset, dropped from a dict. Both envelopes are covered, and the `PYATLAN` path stays on the native encoder. A real guid — a UUID a mapper set on purpose — is kept. Connectors should not clear the guid themselves.

The stamping itself is `application_sdk.common.last_sync.set_last_sync_details_on_asset()`, unwrapped — the seam adds the wider aperture (`entity_bytes` takes an `object`, so the shape may be a dict, may not declare the fields, or may refuse assignment) but not a second definition of what stamping means. An asset object qualifies when it satisfies `LastSyncStampable`, a runtime-checkable Protocol over the three fields; it is a structural type rather than pyatlan's `Asset` because both pyatlan generations are valid targets and they are unrelated classes.

**Resolve `last_sync` once per transform activity**, never per record:

```python
from application_sdk.common.asset_serialization import entity_bytes
from application_sdk.common.last_sync import resolve_last_sync_details

last_sync = resolve_last_sync_details()   # once, outside the record loop

for record in records:
    line = entity_bytes(
        mapper(record, connection_qn),
        connection_name="My MySQL",
        last_sync=last_sync,
        entity_type="table",
    )
```

`lastSyncRunAt` is a property of the *run*, so every asset one crawl produces must carry the same value; a per-record `time.time()` gives every row in one crawl a different "last synced at". `SqlApp._transform_entity` does this for every SQL connector already. **Non-SQL apps get the same behaviour from the same two calls** — nothing in this seam or in `last_sync` is SQL-specific, and an app that writes `asset.to_nested_bytes()` directly today gets both injections plus the typed-error contract by routing through `entity_bytes()` instead. Conformance rule **P052** flags that direct call (and `to_nested_dict()` / `pyatlan_v9` `to_atlas_format()`) in app code, so the bypass does not spread from one connector to the next.

`resolve_last_sync_details()` reads the execution and correlation contextvars the SDK's Temporal interceptor populates. Call it on the event loop inside the activity. `run_in_thread` does propagate contextvars (it runs the callable under `contextvars.copy_context()`), so resolving inside an offloaded loop works too — but then the correctness rests on an offload implementation detail rather than on where the call sits.

### Entity envelope

`entity_bytes()` decides *how to serialise* the mapper's return value. `application_sdk.common.entity_envelope` decides *what the finished line looks like* — and in particular where relationship references live (FND-2137).

Before this, every connector hand-rolled its own post-serialisation pass, and a scan of the six SqlApp-pattern connectors found four mutually incompatible envelope strategies, two of which disagreed about that question. Two apps publishing to the same downstream disagreed about the wire contract.

```python
from application_sdk.common.entity_envelope import (
    EntityDecorations,
    EntityEnvelopePolicy,
    EnvelopeShape,
)

class TeradataApp(SqlApp):
    entity_envelope = EntityEnvelopePolicy(sql_dialect="teradata")
```

Declared once per app as a class attribute, not per mapper: the envelope is a property of the downstream contract, and per-mapper choice is how the fleet ended up with four of them. `SqlApp` reads it in `_transform_entity` and threads it into `entity_bytes()`; a non-SQL app passes `envelope=` directly.

**`shape`** — where relationship refs live:

| Value | Output | Use |
|---|---|---|
| `EnvelopeShape.FLATTENED` | Refs merged into `attributes`, no `relationshipAttributes` key | The default |
| `EnvelopeShape.PYATLAN` | Refs under a top-level `relationshipAttributes` key | **Deprecated, removed in v4.0.** A migration lever for a connector whose *released* output is this shape |

Flattened is the default because that is what the publish app's diff engine reads: it does set-based append/remove diffing for `inputs`, `outputs` and `upstreamTables` out of `attributes`, and a top-level `relationshipAttributes` key falls through to a whole-dict equality branch. Under the pyatlan-native envelope the relationship append/remove path never fires. A connector emitting no lineage gets away with it; the first one that does loses incremental relationship diffing silently. `application_sdk.validation.assets` and the SDK's own seed harness are already on the flattened side.

The flattening itself is `pyatlan_v9.model.transform.to_atlas_format` — the SDK does not hand-roll it, and does not need to: that encoder is *cheaper* than the nested one (≈3 µs/record against ≈11 µs), and never emits the `appendRelationshipAttributes` / `removeRelationshipAttributes` keys the publish app generates itself. A dict-returning mapper or a pyatlan v1 model goes through `flatten_envelope()` instead.

**The two paths agree on ref placement, and on nulls.** Neither drops them. `flatten_envelope()` does not, because a dict-returning mapper emits them on purpose: `atlan-clickhouse-app` emits a null relationship stub to mirror the legacy transformer's all-None-leaves collapse, and null `rowCount` / `sizeBytes` for v2 parity. Dropping those would delete real wire values and rehash every entity in the publish app's diff cache.

`to_atlas_format` does not either — **from pyatlan 11.3.0**, which is why `pyproject.toml` floors at `pyatlan>=11.3` rather than the `>=11` range that would otherwise do. This is a pinned dependency premise, not a property of SDK code, so it is worth knowing what the older behaviour was. `pyatlan_v9` fields are three-state — `Union[str, None, UnsetType] = UNSET` — so an asset *can* express an explicit null, and `to_nested_bytes` has always preserved it. Up to pyatlan 11.2.0 `to_atlas_format` rendered `None` and `UNSET` identically, as an absent key. That was survivable rather than harmless: the publish app's `calculate_attributes_diff` re-synthesises the clear, emitting `{key: None}` when a key in the cached entity is absent from the new one, so a dropped null still reached Atlas on the incremental path and a create had nothing to clear. On `>=11.3` the argument is moot — a producer-side null on a v9 asset reaches the wire as written, the same as one from a dict mapper.

So `FLATTENED` means exactly **"relationship refs live in `attributes`"** — nothing more. Null handling stays the mapper's decision, on both paths.

**`sql_dialect`** — stamped as `attributes.sqlDialect` on assets that carry DDL (`tableDefinition` on a `Table`, `definition` on a `View` / `MaterialisedView`), so downstream SQL parsing knows which grammar to read a definition with. The value is static per connector; the condition is per record. It lives here because no `pyatlan_v9` asset type declares `sqlDialect` — an asset-returning mapper has nowhere to put it.

**Decorations** — top-level fields no pyatlan model field can hold, returned per record from `SqlApp.decorate_entity()`:

```python
class MysqlApp(SqlApp):
    def decorate_entity(self, *, entity_type, record) -> EntityDecorations | None:
        return EntityDecorations(
            default_catalog_name=record["table_catalog"],
            default_schema_name=record["table_schema"],
        )
```

These land on the entity *root*, beside `typeName`, because that is where their readers look — Query Intelligence reads `defaultCatalogName` / `defaultSchemaName` into each `success.json` row, which lineage-app then uses to resolve a bare table name to a fully-qualified Atlas path. The publish app strips unknown root keys before hashing, so a decoration reaches its reader off the transformed artifact or not at all.

The return type is a typed model rather than a `Mapping[str, Any]` deliberately. Every field is a named cross-app contract with a specific reader; a free dict is how a second undocumented side-channel gets added without anyone noticing. A connector needing a field `EntityDecorations` does not have adds it there, which forces the conversation about who reads it.


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
