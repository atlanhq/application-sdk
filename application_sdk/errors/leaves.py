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
    audience: ClassVar[Audience] = Audience.USER


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
