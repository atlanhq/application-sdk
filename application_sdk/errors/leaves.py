"""Fourteen categorical leaf error classes — one per FailureCategory."""

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
    """Required platform service is temporarily down or degraded.

    Covers Dapr, Temporal, object store, and source databases.  Retrying
    the same call is expected to succeed once the dependency recovers.

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
