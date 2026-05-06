"""Structured error codes and error hierarchy for Application SDK.

Canonical API (v3.x+)::

    from application_sdk.errors import (
        AppError,
        FailureCategory,
        Audience,
        FailureDetails,
        AuthError,
        AppPermissionDeniedError, AppTimeoutError,
        NotFoundError, AlreadyExistsError,
        InvalidInputError, PreconditionError,
        RateLimitedError, UnimplementedError,
        DependencyUnavailableError, ResourceExhaustedError,
        DataIntegrityError, CancelledError, InternalError,
    )

Legacy constants (v3.x — deprecated, removed in v4.0)::

    from application_sdk.errors import ErrorCode, APP_ERROR, CREDENTIAL_NOT_FOUND, ...
"""

from dataclasses import dataclass

# ── New canonical hierarchy ──────────────────────────────────────────────────
from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    WORKER_EVICTED_TYPE,
    AlreadyExistsError,
    AppPermissionDeniedError,
    AppTimeoutError,
    AuthError,
    CancelledError,
    DataIntegrityError,
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    RateLimitedError,
    ResourceExhaustedError,
    UnimplementedError,
    WorkerEvictedError,
)
from application_sdk.errors.wire import FailureDetails

# ── Legacy ErrorCode dataclass + AAF-* constants (deprecated — removed v4.0) ─


@dataclass(frozen=True)
class ErrorCode:
    """Structured error code for monitoring and alerting.

    Deprecated: use ``FailureCategory`` + typed ``AppError`` subclasses.
    Will be removed in v4.0.
    """

    component: str
    id: int

    @property
    def code(self) -> str:
        """Format as AAF-{COMP}-{ID:03d}."""
        return f"AAF-{self.component}-{self.id:03d}"

    def __str__(self) -> str:
        return self.code


# APP - Core App errors
APP_ERROR = ErrorCode("APP", 1)
APP_NON_RETRYABLE = ErrorCode("APP", 2)
APP_CONTEXT_ERROR = ErrorCode("APP", 3)
APP_NOT_FOUND = ErrorCode("APP", 4)
APP_ALREADY_REGISTERED = ErrorCode("APP", 5)
TASK_NOT_FOUND = ErrorCode("APP", 6)

# STR - Storage errors
STORAGE_NOT_FOUND = ErrorCode("STR", 1)
STORAGE_PERMISSION = ErrorCode("STR", 2)
STORAGE_CONFIG = ErrorCode("STR", 3)
STORAGE_OPERATION = ErrorCode("STR", 4)

# CTR - Contract errors
CONTRACT_VALIDATION = ErrorCode("CTR", 1)
PAYLOAD_SAFETY = ErrorCode("CTR", 2)

# HDL - Handler errors
HANDLER_ERROR = ErrorCode("HDL", 1)

# EXE - Execution errors
EXECUTION_ERROR = ErrorCode("EXE", 1)
EXECUTION_WORKER_ERROR = ErrorCode("EXE", 2)
EXECUTION_ACTIVITY_ERROR = ErrorCode("EXE", 3)

# INF - Infrastructure errors
STATE_STORE_ERROR = ErrorCode("INF", 1)
PUBSUB_ERROR = ErrorCode("INF", 2)
BINDING_ERROR = ErrorCode("INF", 3)
SECRET_STORE_ERROR = ErrorCode("INF", 4)
SECRET_NOT_FOUND = ErrorCode("INF", 5)

# CRD - Credential errors
CREDENTIAL_ERROR = ErrorCode("CRD", 1)
CREDENTIAL_NOT_FOUND = ErrorCode("CRD", 2)
CREDENTIAL_PARSE_ERROR = ErrorCode("CRD", 3)
CREDENTIAL_VALIDATION_ERROR = ErrorCode("CRD", 4)
CREDENTIAL_VAULT_ERROR = ErrorCode("CRD", 5)

# DSC - Discovery errors
DISCOVERY_ERROR = ErrorCode("DSC", 1)

# EVT - Event/Analytics errors
EVENT_PUBLISH = ErrorCode("EVT", 1)
EVENT_BUS = ErrorCode("EVT", 2)
SEGMENT_ERROR = ErrorCode("EVT", 3)

__all__ = [
    # New canonical
    "AppError",
    "Audience",
    "FailureCategory",
    "FailureDetails",
    "AlreadyExistsError",
    "AppPermissionDeniedError",
    "AppTimeoutError",
    "AuthError",
    "CancelledError",
    "DataIntegrityError",
    "DependencyUnavailableError",
    "InternalError",
    "InvalidInputError",
    "NotFoundError",
    "PreconditionError",
    "RateLimitedError",
    "ResourceExhaustedError",
    "UnimplementedError",
    "WorkerEvictedError",
    "WORKER_EVICTED_TYPE",
    # Legacy (deprecated — removed in v4.0)
    "ErrorCode",
    "APP_ERROR",
    "APP_NON_RETRYABLE",
    "APP_CONTEXT_ERROR",
    "APP_NOT_FOUND",
    "APP_ALREADY_REGISTERED",
    "TASK_NOT_FOUND",
    "STORAGE_NOT_FOUND",
    "STORAGE_PERMISSION",
    "STORAGE_CONFIG",
    "STORAGE_OPERATION",
    "CONTRACT_VALIDATION",
    "PAYLOAD_SAFETY",
    "HANDLER_ERROR",
    "EXECUTION_ERROR",
    "EXECUTION_WORKER_ERROR",
    "EXECUTION_ACTIVITY_ERROR",
    "STATE_STORE_ERROR",
    "PUBSUB_ERROR",
    "BINDING_ERROR",
    "SECRET_STORE_ERROR",
    "SECRET_NOT_FOUND",
    "CREDENTIAL_ERROR",
    "CREDENTIAL_NOT_FOUND",
    "CREDENTIAL_PARSE_ERROR",
    "CREDENTIAL_VALIDATION_ERROR",
    "CREDENTIAL_VAULT_ERROR",
    "DISCOVERY_ERROR",
    "EVENT_PUBLISH",
    "EVENT_BUS",
    "SEGMENT_ERROR",
]
