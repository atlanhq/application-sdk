"""Fifteen categorical leaf error classes — one per FailureCategory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory


@dataclass(kw_only=True)
class CancelledError(AppError):
    cancelled_by: str | None = None
    reason: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.CANCELLED
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "CANCELLED"
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class AppTimeoutError(AppError):
    """A bounded wait elapsed.

    Use for network reads, activity start-to-close limits, and heartbeat
    timeouts.  Default audience is APP_OWNER because the locus is rarely
    obvious and the app team is best placed to investigate and reclassify
    (a source network timeout is USER-fixable; an internal Temporal deadline
    is PLATFORM-routed).  Override ``audience`` on leaf subclasses when the
    locus is known — do not leave it as the default if you can pick.
    """

    operation: str | None = None
    timeout_seconds: float | None = None
    elapsed_seconds: float | None = None

    category: ClassVar[FailureCategory] = FailureCategory.TIMEOUT
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "TIMEOUT"
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class TaskStalledError(AppTimeoutError):
    """An activity attempt was failed for making no observable progress (ADR-0018).

    Raised by the SDK's activity wrapper, not by app code: the stall watchdog in
    the auto-heartbeat loop cancels the attempt, and the cancellation handler
    turns that cancel into this error. It means the attempt kept heartbeating —
    the event loop was alive — while nothing observable advanced for longer than
    the task's ``max_no_progress_seconds``.

    A subtype of :class:`AppTimeoutError` rather than a sixteenth categorical
    leaf: a stall *is* a TIMEOUT-category failure, so ``except AppTimeoutError``
    catch sites and TIMEOUT-keyed consumers keep working, while the distinct
    ``code`` and the Temporal wire type ``"TaskStalledError"`` let a stall kill
    be counted separately from a ``StartToClose`` or heartbeat timeout. The
    ``TIMEOUT_`` prefix on the code is the convention every leaf subclass follows
    (P003) so the code column carries its category without a join.

    ``stalled_for_seconds`` is not the inherited ``elapsed_seconds``, and both
    can be meaningful at once: ``elapsed_seconds`` measures how long a bounded
    wait ran before it was abandoned, while this measures the *quiet gap* inside
    an attempt that may have been productively running for hours before it went
    silent. A stall has no bounded wait that elapsed — that absence is the whole
    problem the watchdog exists to detect — so ``timeout_seconds`` and
    ``elapsed_seconds`` are left unset on this path.

    **Retryable, deliberately.** The dominant cause is a transient source-side
    hang in an app whose error handling never surfaced it, which self-heals on a
    fresh attempt; a genuine wedge re-stalls and costs at most a few multiples of
    ``max_no_progress_seconds``, not of the duration backstop. Non-retryable
    would convert the self-healing majority into failed runs needing a manual
    re-run that restarts from zero anyway (ADR-0018 → *Failing the activity*).
    Pinned here rather than inherited so a future change to
    :class:`AppTimeoutError`'s default cannot silently flip it.
    """

    stalled_for_seconds: float | None = None
    last_progress_label: str | None = None

    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "TIMEOUT_TASK_STALLED"
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class RateLimitedError(AppError):
    limit_type: str | None = None
    retry_after_seconds: float | None = None
    quota_name: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.RATE_LIMITED
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "RATE_LIMITED"
    # APP_OWNER, not USER: RATE_LIMITED is in the gate's _GATE_BROKEN_CATEGORIES,
    # so the gate fails open on it rather than blaming the source. A 429 is our
    # call rate against a customer-owned endpoint — the remediation is connector
    # concurrency, which the customer cannot change (CONNECT-812 PF-29, P041).
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class AuthError(AppError):
    auth_method: str | None = None
    principal: str | None = None
    failure_reason: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.AUTH
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "AUTH"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class AppPermissionDeniedError(AppError):
    """Authenticated but not authorised."""

    principal: str | None = None
    resource: str | None = None
    required_action: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.PERMISSION
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "PERMISSION"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class NotFoundError(AppError):
    resource_type: str | None = None
    resource_identifier: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "NOT_FOUND"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class AlreadyExistsError(AppError):
    """Entity the caller tried to create already exists.

    Use for idempotent-create paths (asset already registered, entity already
    in the store).  Distinct from PRECONDITION — the resource exists and that
    is the problem, not some other state conflict.
    """

    resource_type: str | None = None
    resource_identifier: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.ALREADY_EXISTS
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "ALREADY_EXISTS"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class InvalidInputError(AppError):
    field: str | None = None
    constraint: str | None = None
    value_summary: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INVALID_INPUT"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class PreconditionError(AppError):
    """System state forbids the operation.

    Use when inputs are syntactically valid but the current state blocks the
    action (schema mismatch, version conflict, entity in wrong state).

    Litmus test vs DEPENDENCY_UNAVAILABLE: if retrying the *same call* without
    any state change is expected to succeed, use DependencyUnavailableError.
    If explicit state must change first, use PreconditionError.
    """

    resource: str | None = None
    expected_state: str | None = None
    actual_state: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.PRECONDITION
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "PRECONDITION"
    audience: ClassVar[Audience] = Audience.USER


@dataclass(kw_only=True)
class DependencyUnavailableError(AppError):
    """Required Atlan-internal platform service is temporarily down or degraded.

    Covers Dapr, Temporal, and object store.  Retrying the same call is
    expected to succeed once the dependency recovers.

    For customer-controlled source systems (databases, SaaS APIs), use
    SourceUnavailableError instead — those route to USER, not PLATFORM.

    Litmus test vs PRECONDITION: if system state must change before the call
    can succeed, use PreconditionError.  If the same call would work on retry,
    use DependencyUnavailableError.
    """

    service: str | None = None
    target: str | None = None
    network_error: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.DEPENDENCY_UNAVAILABLE
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE"
    audience: ClassVar[Audience] = Audience.PLATFORM


class ColdStartRaceError(DependencyUnavailableError):
    """Marker for a :class:`DependencyUnavailableError` that specifically means
    "this Dapr-backed dependency is not yet reachable" — as opposed to the
    dependency answering and definitively rejecting the request.

    What counts as "not yet reachable" is domain-specific and decided by
    each concrete subtype's classifier, not by this marker: a transport
    failure always qualifies, but a bare 5xx status does NOT necessarily —
    e.g. the Dapr *secrets* API also returns 500 for a genuinely-missing
    key (see
    :func:`~application_sdk.infrastructure._dapr.client.classify_secret_fetch_error`),
    so that classifier additionally inspects the error body before
    concluding "not yet reachable".

    Deliberately independent of ``retryable``/``effective_retryable``: that
    field is a general Temporal/wire-level retry hint (a plain, unclassified
    ``DependencyUnavailableError`` subtype legitimately defaults it to
    ``True`` — "this category of failure is generically worth retrying at
    the activity level"). This marker answers a narrower, unrelated
    question — "is this specific failure a cold-start race right now" — so a
    generic dependency helper (e.g.
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`)
    can retry any current or future subtype across domains (secret store,
    state store, pub/sub, ...) by catching this one marker, without a new
    per-domain exception type or check for each. Not meant to be raised
    directly — concrete subtypes (e.g. ``SecretStoreUnavailableError``)
    multiply-inherit it alongside their domain's own error type. Declares
    its own code regardless, so a direct instantiation still gets a
    triageable code instead of collapsing to the bare
    ``DependencyUnavailableError`` bucket.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_COLD_START"


@dataclass(kw_only=True)
class DaprSidecarUnreachableError(ColdStartRaceError):
    """Terminal form of a cold-start race: the Dapr sidecar never became
    reachable for the entire cold-start budget, across every attempt.

    Raised by
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start` only at
    budget exhaustion — the point at which no attempt ever got a usable
    answer. A transient race that eventually resolves returns normally and
    never reaches this type, so this type distinguishes a budget-exhausted
    outage from a still-booting one for diagnosis.

    It stays wire-retryable (inherits ``default_retryable = True``): naming the
    terminal state does not by itself change retry or fail-open behaviour. An
    activity-level retry can still recover when a later attempt lands on a
    healthy worker (a single bad pod in a multi-replica pool), so the retry
    hint is left on deliberately — the same topology reasoning that keeps a
    platform fault failing open rather than blocking the run. Choosing to
    *stop* on this state instead of retrying is a separate policy decision, not
    made here.

    Distinct from a plain ``ColdStartRaceError`` purely so the two read
    differently to an operator or a triaging agent: a bare
    ``ColdStartRaceError`` says "not reachable yet, still waiting"; this says
    "not reachable for the whole budget, done waiting". Reporting a persistent
    platform fault as a transient race is the defect this fixes. Subclasses
    ``ColdStartRaceError`` (not ``DependencyUnavailableError`` directly) so it
    inherits ``DEPENDENCY_UNAVAILABLE`` — keeping error routing and the
    preflight gate's ``gate_broken`` classification unchanged — and so every
    existing ``except ColdStartRaceError`` catch site keeps catching it.

    Carries the diagnosis an operator needs with no secret surface:
    ``component`` (a Dapr config identifier), ``attempts``, and
    ``elapsed_seconds`` are all safe to render. The underlying transport error
    rides the inherited ``cause`` field and is never string-interpolated into
    the message.
    """

    component: str | None = None
    attempts: int | None = None
    elapsed_seconds: float | None = None

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_SIDECAR_UNREACHABLE"


@dataclass(kw_only=True)
class ObjectStoreReadError(DependencyUnavailableError):
    """Object store listing returned no files matching the expected extension.

    Surfaces the prefix that was searched so operators can immediately tell
    whether the upstream task wrote to the wrong path, was skipped entirely,
    or if the configured prefix on this read side is wrong.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_READ"
    message: str = "No matching files found in object store."
    suggested_action: str | None = (
        "Verify the upstream task wrote to this prefix and that the "
        "configured prefix is correct."
    )
    service: str | None = "object_store"
    path: str | None = None
    file_extension: str | None = None


@dataclass(kw_only=True)
class ObjectStoreDownloadError(DependencyUnavailableError):
    """No local files found and download from object store failed."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_DOWNLOAD"
    message: str = "No files found locally and failed to download from object store"
    service: str | None = "object_store"
    path: str | None = None
    file_extension: str | None = None


@dataclass(kw_only=True)
class SourceUnavailableError(AppError):
    """Customer-controlled source system is temporarily unreachable.

    Use when a connector cannot reach the source (database, SaaS API, on-prem
    endpoint) due to a transient network or server-side condition.  Retrying
    is expected to succeed once the source recovers.

    Litmus test vs DependencyUnavailableError: SourceUnavailableError is for
    systems the *customer* owns and operates (their Snowflake account, their
    on-prem SQL Server, a third-party SaaS API); DependencyUnavailableError is
    for Atlan-internal platform services (Dapr, Temporal, object store).
    """

    source_type: str | None = None
    endpoint: str | None = None
    http_status: int | None = None
    network_error: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.SOURCE_UNAVAILABLE
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "SOURCE_UNAVAILABLE"
    audience: ClassVar[Audience] = Audience.USER


# Temporal wire type string for worker-pod eviction. Set as
# ``ApplicationError.type`` by the activity wrapper so workflow code can
# recognise the failure by string match across the Temporal boundary
# (Temporal serialises only the type string, not the Python exception class).
WORKER_EVICTED_TYPE = "WorkerEvicted"


@dataclass(kw_only=True)
class ResourceExhaustedError(AppError):
    resource: str | None = None
    limit: str | None = None
    observed: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.RESOURCE_EXHAUSTED
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "RESOURCE_EXHAUSTED"
    audience: ClassVar[Audience] = Audience.PLATFORM


@dataclass(kw_only=True)
class DiskFullError(ResourceExhaustedError):
    """A local write failed because the filesystem had no room for it (FND-318).

    Raised by the SDK's write boundary (:mod:`application_sdk.common.atomic`)
    for ``ENOSPC`` and ``EDQUOT`` — either the volume is out of blocks or the
    writing identity is over its quota. Both mean the same thing to whoever has
    to act, which is why they share one type.

    A bare ``OSError`` is the failure this replaces. It carries no category, so
    it lands in whatever broad ``except`` happens to be in the call stack and
    the run reports some downstream symptom instead — in the incident that
    motivated this, a truncated JSON artifact that failed in a *consuming*
    app's parser forty minutes later. A typed error is classified, attributable
    to the writing step, and alertable.

    **This error is also the operator's signal that the deployment needs more
    ephemeral storage.** Requests and limits are deployment configuration and
    are deliberately not requested from either the SDK or the app — neither can
    know the number. Naming the path, what the write needed, and what was free
    tells the operator which deployment to raise and by roughly how much,
    without either codebase guessing.

    **Retryable, inherited deliberately.** Disk pressure is often not ours: a
    co-tenant's scratch space frees, or a fresh attempt starts on a node that
    has room. A genuinely undersized deployment re-fails at
    :func:`~application_sdk.common.atomic.ensure_free_space` in seconds rather
    than part-way through a long write, so the retries are cheap and each one
    re-emits the same operator signal.
    """

    path: str | None = None
    operation: str | None = None
    required_bytes: int | None = None
    free_bytes: int | None = None

    code: ClassVar[str] = "RESOURCE_EXHAUSTED_DISK_FULL"
    resource: str | None = "disk"
    # Defaulted on the class rather than passed at each raise site: the
    # remediation is the same wherever this is raised from, and a raise site
    # that forgot it would ship the one failure whose whole purpose is telling
    # an operator what to change with no instruction attached.
    suggested_action: str | None = (
        "Raise the ephemeral-storage request/limit on this deployment, or "
        "reduce the volume this step stages on local disk."
    )


@dataclass(kw_only=True)
class DataIntegrityError(AppError):
    expectation: str | None = None
    observed: str | None = None
    location: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.DATA_INTEGRITY
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "DATA_INTEGRITY"
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class InternalError(AppError):
    component: str | None = None
    invariant: str | None = None
    classification_pending: bool = False

    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INTERNAL"
    audience: ClassVar[Audience] = Audience.APP_OWNER


@dataclass(kw_only=True)
class UnimplementedError(AppError):
    """Operation not supported or capability not yet built.

    Use for known feature gaps so on-call is not paged for an expected absence.
    Distinct from INTERNAL (unexpected invariant violation / bug).
    """

    operation: str | None = None
    reason: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.UNIMPLEMENTED
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "UNIMPLEMENTED"
    audience: ClassVar[Audience] = Audience.APP_OWNER
