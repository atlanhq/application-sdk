# ADR-0012: Error Hierarchy and Failure Taxonomy

## Status
**Accepted**

## Context

Before this work, `application_sdk/errors.py` was a 100-line file containing
only a frozen `ErrorCode("APP", 1)` dataclass and ~30 `AAF-{COMP}-{ID:03d}`
string constants. A parallel `application_sdk/common/error_codes.py` provided
an `Atlan-{Component}-{HTTP}-{ID}` taxonomy that coupled failure classification
to HTTP status codes. Domain modules (`app/`, `credentials/`, `storage/`,
`handler/`, `infrastructure/`) each defined their own ad-hoc `Exception`
subclasses with inconsistent shapes.

This created five concrete problems:

1. **No `except`-by-shape.** You could catch `CredentialError` but not "any
   auth failure regardless of domain."
2. **No retryability marker.** Temporal wrap sites had to encode retry intent
   by hand; there was no per-class default.
3. **No audience signal.** Nothing told a consumer whether a failure needed
   customer action, infra ops, or SDK engineering — downstream systems
   maintained manual lookup tables.
4. **Free-text evidence.** Per-error context lived in f-string messages.
   Log queries and dashboards had nothing structured to index.
5. **No typed wire schema.** Cross-process failures crossing
   `ApplicationError.details=[…]` were strings; consumers parsed them.

## Decision

Replace the ad-hoc taxonomy with a typed hierarchy built on three orthogonal
axes, a Pydantic wire envelope, and a multi-inheritance compatibility lattice
that keeps legacy `except Domain:` catch sites alive.

---

### 1. Three orthogonal axes

| Axis | Type | Owner | Closed? |
|---|---|---|---|
| `FailureCategory` — *what happened* | Enum (14 values) | SDK | Yes |
| `Audience` — *who must act* | Enum (3 values) | SDK | Yes |
| `suggested_action` — *what to do* | `str \| None` | App | No |

**`FailureCategory`** is a closed, SDK-owned vocabulary of 14 values:
`CANCELLED`, `TIMEOUT`, `RATE_LIMITED`, `AUTH`, `PERMISSION`, `NOT_FOUND`,
`ALREADY_EXISTS`, `INVALID_INPUT`, `PRECONDITION`, `DEPENDENCY_UNAVAILABLE`,
`RESOURCE_EXHAUSTED`, `DATA_INTEGRITY`, `INTERNAL`, `UNIMPLEMENTED`. It is
closed so Atlan-internal consumers (Automation Engine, SLA dashboards) can
branch on it without a lookup table, and so semantic drift requires an SDK
release with a documented evolution policy.

The vocabulary aligns closely with gRPC `google.rpc.Code` (see §5 below), with
litmus tests for the four ambiguous boundary pairs:

- `DEPENDENCY_UNAVAILABLE` vs `PRECONDITION`: if retrying *the same call*
  without any state change is expected to succeed, use
  `DEPENDENCY_UNAVAILABLE`; if explicit state must change first, use
  `PRECONDITION`.
- `RATE_LIMITED` vs `RESOURCE_EXHAUSTED`: remote per-key 429 →
  `RATE_LIMITED`; local resource limit (OOM, disk, file handles) →
  `RESOURCE_EXHAUSTED`.
- `ALREADY_EXISTS` vs `PRECONDITION`: idempotent-create with an already-
  existing entity → `ALREADY_EXISTS`; broader state conflict → `PRECONDITION`.
- `UNIMPLEMENTED` vs `INTERNAL`: known feature gap → `UNIMPLEMENTED`;
  unexpected invariant violation → `INTERNAL`. `UNIMPLEMENTED` exists
  precisely so on-call is not paged for an expected absence.

**`Audience`** is a closed three-value enum. Every leaf must pick one — there
is no `UNKNOWN` escape hatch. If the locus is unclear, the answer is
`APP_OWNER`: the team that wrote the code investigates and reclassifies. From
production routing's standpoint, SDK and connector share an owner (the team
debugs first, escalates to the SDK team if needed).

| Value | First-responder |
|---|---|
| `USER` | Customer self-service or support ticket to the connector owner |
| `PLATFORM` | Infra ops — check dashboards (Dapr, Temporal, pod health, S3) |
| `APP_OWNER` | The team that owns the failing code — connector or SDK engineering |

**`suggested_action`** is a single optional string. Its voice shifts with
audience: customer-facing text when `audience=USER`, engineer-facing
remediation when `audience=APP_OWNER`, runbook hint when `audience=PLATFORM`.
One field, not a per-audience split — see §4 for why the split was rejected.

---

### 2. `AppError` base and categorical leaves

`AppError` is a `@dataclass(kw_only=True)` subclass of `Exception`. Instance
fields (`message`, `retryable`, `cause`, `app_name`, `run_id`,
`suggested_action`) are the base set. ClassVars (`category`,
`default_retryable`, `code`, `audience`) carry the per-class identity.

Fourteen **categorical leaves** in `application_sdk/errors/leaves.py` — one
per `FailureCategory` — subclass `AppError` and fix all four ClassVars plus
add dataclass fields for structured, per-category evidence:

| Leaf | Category | Retryable | Audience | Evidence fields |
|---|---|---|---|---|
| `CancelledError` | CANCELLED | No | APP_OWNER | `cancelled_by`, `reason` |
| `AppTimeoutError` | TIMEOUT | **Yes** | APP_OWNER | `operation`, `timeout_seconds`, `elapsed_seconds` |
| `RateLimitedError` | RATE_LIMITED | **Yes** | USER | `limit_type`, `retry_after_seconds`, `quota_name` |
| `AuthError` | AUTH | No | USER | `auth_method`, `principal`, `failure_reason` |
| `AppPermissionDeniedError` | PERMISSION | No | USER | `principal`, `resource`, `required_action` |
| `NotFoundError` | NOT_FOUND | No | USER | `resource_type`, `resource_identifier` |
| `AlreadyExistsError` | ALREADY_EXISTS | No | USER | `resource_type`, `resource_identifier` |
| `InvalidInputError` | INVALID_INPUT | No | USER | `field`, `constraint`, `value_summary` |
| `PreconditionError` | PRECONDITION | No | USER | `resource`, `expected_state`, `actual_state` |
| `DependencyUnavailableError` | DEPENDENCY_UNAVAILABLE | **Yes** | PLATFORM | `service`, `target`, `network_error` |
| `ResourceExhaustedError` | RESOURCE_EXHAUSTED | **Yes** | PLATFORM | `resource`, `limit`, `observed` |
| `DataIntegrityError` | DATA_INTEGRITY | No | APP_OWNER | `expectation`, `observed`, `location` |
| `InternalError` | INTERNAL | No | APP_OWNER | `component`, `invariant`, `classification_pending` |
| `UnimplementedError` | UNIMPLEMENTED | No | APP_OWNER | `operation`, `reason` |

`AppTimeoutError` and `CancelledError` default to `APP_OWNER` because their
locus varies: a source network timeout is USER-fixable; an internal Temporal
deadline is PLATFORM-routed. Subclasses override `audience` when the locus is
known.

`InternalError.classification_pending` is a triage flag. Caught-but-
unclassified failures surface as `InternalError(classification_pending=True)`,
creating an auditable backlog rather than hiding among real bugs.

Retryability is encoded as a typed `bool` with a per-class `default_retryable`
ClassVar and a per-instance `retryable: bool | None` override, resolved
through `AppError.effective_retryable`.

---

### 3. Dataclass-as-schema and the wire envelope

`AppError.to_failure_details()` builds the Pydantic wire envelope by walking
`dataclasses.fields(self)` and excluding `_BASE_FIELDS`
(`message`, `retryable`, `cause`, `app_name`, `run_id`, `suggested_action`).
Every remaining dataclass field on the instance flows into `evidence`.
**Adding a field to a leaf automatically extends the wire payload — there is
no parallel Pydantic model per leaf to maintain.**

`FailureDetails` at `application_sdk/errors/wire.py` is a Pydantic
`BaseModel` (`frozen=True`, `extra="forbid"`) that crosses Temporal's
`ApplicationError.details=[…]` via `pydantic_data_converter`. Its schema:

```
category        FailureCategory   routing axis
code            str               app-owned fine-grained code
retryable       bool              resolved effective_retryable
audience        Audience          routing axis
message         str               human description
suggested_action  str | None      imperative hint (voice shifts with audience)
evidence        dict[str, Any]    per-leaf structured context
app_name        str | None        correlation
run_id          str | None        correlation
cause_repr      str | None        repr(cause) — never the live exception
```

`cause_repr` is a string. The live exception never crosses the wire: no
serialisation of non-picklable state, no stack-frame leakage.

**Tenant identity is deliberately absent.** The producing app does not know
or carry tenant context. Per-tenant attribution is the consumer's
responsibility (the Automation Engine reads `ApplicationError.details` and
attaches tenant from its own session context at ingest time). This keeps
producer-side logs free of tenant identifiers and avoids leaking them through
stack traces.

**Secret redaction at envelope construction.** `FailureDetails._no_secret_keys`
is a Pydantic field-validator that rejects evidence keys matching an exact
denylist (`auth_header`, `authorization`, `cookie`, `token`, `password`,
`secret`, `api_key`, `private_key`) or a suffix denylist (`_secret`,
`_password`, `_token`). The suffix list catches compound names like
`client_secret` and `db_password` without blocking generic names like
`object_key` or `cache_key`. This is **the single layer where sensitivity
is decided** — domain classes promote their context as dataclass fields
without duplicating the policy.

---

### 4. Domain umbrellas via leaf-first multi-inheritance

Three domain umbrella classes (`CredentialError`, `StorageError`,
`SecretStoreError`) bridge the legacy `except Domain:` pattern with the new
categorical routing:

```python
class CredentialError(AuthError): ...           # category=AUTH
class StorageError(DependencyUnavailableError): ...  # category=DEPENDENCY_UNAVAILABLE
class SecretStoreError(DependencyUnavailableError): ...  # category=DEPENDENCY_UNAVAILABLE
```

Specialised subclasses use **leaf-first multi-inheritance** so the
`category`, `audience`, and `default_retryable` ClassVars resolve through
the categorical leaf (MRO column 1), while the domain umbrella remains
catchable (MRO column 2):

```python
class StorageNotFoundError(NotFoundError, StorageError): ...
# category=NOT_FOUND (from NotFoundError), not DEPENDENCY_UNAVAILABLE

class CredentialNotFoundError(NotFoundError, CredentialError): ...
# category=NOT_FOUND; except CredentialError: still catches

class SecretNotFoundError(NotFoundError, SecretStoreError): ...
# category=NOT_FOUND; except SecretStoreError: still catches
```

All ten domain umbrella classes are `@dataclass(kw_only=True)`. Base
umbrellas carry a domain-specific evidence field (`CredentialError.credential_name`,
`StorageError.key`, `SecretStoreError.secret_name`) that flows through
`to_failure_details().evidence` automatically. These are references (a
credential GUID, an object key, a secret name) — they are not secrets and
pass the wire denylist. The denylist is the redaction policy; the domain
classes do not gate it.

Custom `__init__` overrides on each class preserve the legacy positional
calling conventions (`CredentialError("msg")`,
`CredentialNotFoundError("guid")`, `SecretNotFoundError("KEY")`) required
by existing callers and asserted in `tests/unit/errors/test_back_compat.py`.

The `CredentialError`/`StorageError`/`SecretStoreError` families are
first-class members of the hierarchy — they do not emit `DeprecationWarning`
and are not scheduled for removal in v4.0.

---

### 5. Deprecation strategy

The legacy `ErrorCode` dataclass and `AAF-{COMP}-{ID:03d}` constants remain
importable from `application_sdk.errors` through v3.x and are removed in
v4.0. The `application_sdk/common/error_codes.py` `Atlan-{Component}-{HTTP}-{ID}`
taxonomy emits `DeprecationWarning` on every `AtlanError.__init__` call and is
also removed in v4.0.

---

## Industry precedents

**gRPC `google.rpc.Code`** is the primary vocabulary reference. The 14
`FailureCategory` values align near 1-to-1:

| gRPC status | FailureCategory |
|---|---|
| `UNAVAILABLE` | `DEPENDENCY_UNAVAILABLE` |
| `FAILED_PRECONDITION` | `PRECONDITION` |
| `RESOURCE_EXHAUSTED` | `RESOURCE_EXHAUSTED` |
| `UNIMPLEMENTED` | `UNIMPLEMENTED` |
| `ALREADY_EXISTS` | `ALREADY_EXISTS` |
| `NOT_FOUND` | `NOT_FOUND` |
| `PERMISSION_DENIED` | `PERMISSION` |
| `UNAUTHENTICATED` | `AUTH` |
| `INVALID_ARGUMENT` | `INVALID_INPUT` |
| `DATA_LOSS` | `DATA_INTEGRITY` |
| `INTERNAL` | `INTERNAL` |
| `CANCELLED` | `CANCELLED` |
| `DEADLINE_EXCEEDED` | `TIMEOUT` |

The `Audience` first-responder mapping is adapted from the intent gRPC
documents for each of its status codes.

**Temporal failure model.** `FailureDetails` is shaped to ride
`ApplicationError(details=[…])`. `effective_retryable` maps to Temporal's
`non_retryable` flag. The Pydantic envelope round-trips through
`pydantic_data_converter` with no dict adapter, matching Temporal's
established Pydantic-native serialisation convention in this codebase.

**Pydantic `BaseModel`.** `frozen=True` + `extra="forbid"` deliver schema
closure and immutability for free, consistent with how other SDK contracts are
defined.

**OpenTelemetry semantic conventions.** The `evidence` dict mirrors OTel's
typed-attribute model: key/value pairs that consumers can index without
parsing free text. The `Audience` routing axis is Atlan-specific and is not
derived from OTel.

**AWS SDK / Stripe error model.** Both were surveyed during initial analysis.
Both bake routing into HTTP status codes — the same transport-coupling that
the `Atlan-{Component}-{HTTP}-{ID}` taxonomy had and that this work removes.
The `code` + `message` + structured-detail shape was borrowed; the HTTP-status
routing was not.

---

## Alternatives considered

**Keep the AAF / Atlan string-coded taxonomy.** Couples failure classification
to HTTP transport; provides no class hierarchy for `except`; carries no
retryability or audience signal; forces consumers to maintain a code-to-meaning
lookup table.

**Pure domain hierarchy** (the legacy state). Cannot answer "is this
retryable?" or "who must fix this?" without per-class custom code. Every
consumer must know every domain class.

**Pure categorical hierarchy — drop `CredentialError` / `StorageError`.**
Would delete every legacy `except StorageError:` catch site overnight. The
leaf-first multi-inheritance lattice keeps both axes simultaneously catchable.

**Open `FailureCategory` enum.** Rejected. Closed enum lets consumers `match`
exhaustively and prevents semantic drift; new categories require an SDK
release and a documented evolution policy.

**Derive `Audience` from `FailureCategory`.** Rejected. The same category
routes to different responders depending on context. `AppTimeoutError`'s
`APP_OWNER` default encodes that the locus varies; the field exists so
routing can be specialised at the leaf.

**Add an `UNKNOWN` audience (four-value enum).** Considered and explicitly
rejected. An `UNKNOWN` bucket is a routing hole — failures land there and
nobody pages. `APP_OWNER` makes the absence of a known locus a routing
decision: the team that wrote the code investigates and reclassifies.

**Carry `tenant_id` on the wire envelope.** Rejected. The producer does not
know or carry tenant context; per-tenant attribution is the consumer's job at
ingest. Producers emitting tenant identifiers would also leak them through
producer-side logs and stack traces.

**Split per-audience messages (`customer_message` / `internal_message`).**
Rejected. `suggested_action` is a single field whose voice shifts with
`audience`. Per-audience message fields duplicate intent and drift.

**Pydantic-only error model (no dataclass `AppError`).** Rejected. The
dataclass *is* the schema. `to_failure_details()` derives the wire envelope
from `dataclasses.fields(self)` — no parallel Pydantic model per leaf to
maintain.

**Python builtin-shadow names (`PermissionError`, `TimeoutError`).** Tried,
abandoned. Shadowing builtins broke `except PermissionError:` for OS-level
errors. Renamed to `AppPermissionDeniedError` / `AppTimeoutError`; the SDK
still exports module-level aliases for catch-by-name code that does not
want the prefix.

**Marker mixins for retryability (`Retryable` / `NonRetryable`).** Rejected.
Encoded as a typed `bool` with a per-category `default_retryable` ClassVar.
Marker mixins carry no value and do not survive serialisation.

**Withhold domain context from the dataclass to avoid logging references.**
Rejected. `credential_name`, `key`, `secret_name` are references, not secrets.
Sensitivity is decided at one layer — `FailureDetails._no_secret_keys` — not
by leaving fields outside the dataclass system.

**Exact-match-only secret-key denylist.** Extended with suffix matching
(`_secret`, `_password`, `_token`) so `client_secret` and `db_password` are
caught without false-positives on `object_key` or `cache_key`.

**Keep `AppContextError` inheriting from `RuntimeError`.** Rejected as a
deliberate breaking change; asserted in
`tests/unit/errors/test_back_compat.py::test_app_context_error_no_longer_is_runtime_error`.

---

## Consequences

**Positive:**

- `except AppError:` catches every SDK-emitted failure.
- `except <CategoricalLeaf>:` catches every failure of a given shape
  regardless of domain.
- Legacy `except StorageError:` / `except CredentialError:` catch sites keep
  working with no source changes.
- `FailureDetails` round-trips through Temporal with typed routing fields and
  structured evidence. Consumers branch on `category` and `audience` without
  parsing strings.
- Retryability is a first-class typed property; Temporal wrap sites read
  `effective_retryable`.
- Sensitivity is decided once, at the wire layer. Domain classes expose their
  context as dataclass fields that flow into evidence uniformly.
- The dataclass *is* the schema — no parallel Pydantic model per leaf to
  keep in sync.

**Negative:**

- Two error systems coexist during v3.x (legacy `AAF-…` constants and the
  `AtlanError` family alongside the new hierarchy). Both are removed in v4.0.
- Leaf-first multi-inheritance MRO has a learning curve; the convention is
  documented in the module docstrings of `credentials/errors.py`,
  `storage/errors.py`, and `infrastructure/secrets.py`.

---

## Implementation

Key files:

- `application_sdk/errors/{base,categories,leaves,wire,__init__}.py` —
  canonical hierarchy
- `application_sdk/credentials/errors.py` — domain umbrellas (dataclasses)
- `application_sdk/storage/errors.py` — domain umbrellas (dataclasses)
- `application_sdk/infrastructure/secrets.py` — domain umbrellas (dataclasses)
- `tests/unit/errors/test_back_compat.py` — legacy invariants
- `tests/unit/errors/test_categorical.py` — categorical leaf invariants
- `tests/unit/errors/test_domain_evidence.py` — domain evidence invariants
- `tests/unit/errors/test_app_subclassing.py` — subclassing patterns
- `docs/standards/exceptions.md` — updated to recommend categorical leaves
