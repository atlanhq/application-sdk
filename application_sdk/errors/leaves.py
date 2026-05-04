"""Twelve categorical leaf error classes — one per FailureCategory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import FailureCategory


@dataclass(kw_only=True)
class CancelledError(AppError):
    cancelled_by: str | None = None
    reason: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.CANCELLED
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "CANCELLED"


@dataclass(kw_only=True)
class TimeoutError(AppError):
    operation: str | None = None
    timeout_seconds: float | None = None
    elapsed_seconds: float | None = None

    category: ClassVar[FailureCategory] = FailureCategory.TIMEOUT
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "TIMEOUT"


@dataclass(kw_only=True)
class RateLimitedError(AppError):
    limit_type: str | None = None
    retry_after_seconds: float | None = None
    quota_name: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.RATE_LIMITED
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "RATE_LIMITED"


@dataclass(kw_only=True)
class AuthError(AppError):
    auth_method: str | None = None
    principal: str | None = None
    failure_reason: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.AUTH
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "AUTH"


@dataclass(kw_only=True)
class PermissionError(AppError):
    principal: str | None = None
    resource: str | None = None
    required_action: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.PERMISSION
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "PERMISSION"


@dataclass(kw_only=True)
class NotFoundError(AppError):
    resource_type: str | None = None
    resource_identifier: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "NOT_FOUND"


@dataclass(kw_only=True)
class InvalidInputError(AppError):
    field: str | None = None
    constraint: str | None = None
    value_summary: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INVALID_INPUT"


@dataclass(kw_only=True)
class PreconditionError(AppError):
    resource: str | None = None
    expected_state: str | None = None
    actual_state: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.PRECONDITION
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "PRECONDITION"


@dataclass(kw_only=True)
class DependencyUnavailableError(AppError):
    service: str | None = None
    target: str | None = None
    network_error: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.DEPENDENCY_UNAVAILABLE
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE"


@dataclass(kw_only=True)
class ResourceExhaustedError(AppError):
    resource: str | None = None
    limit: str | None = None
    observed: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.RESOURCE_EXHAUSTED
    default_retryable: ClassVar[bool] = True
    code: ClassVar[str] = "RESOURCE_EXHAUSTED"


@dataclass(kw_only=True)
class DataIntegrityError(AppError):
    expectation: str | None = None
    observed: str | None = None
    location: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.DATA_INTEGRITY
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "DATA_INTEGRITY"


@dataclass(kw_only=True)
class InternalError(AppError):
    component: str | None = None
    invariant: str | None = None
    classification_pending: bool = False

    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INTERNAL"
