from server_sdk.errors.base import AppError
from server_sdk.errors.categories import Audience, FailureCategory
from server_sdk.errors.leaves import (
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
    SourceUnavailableError,
    UnimplementedError,
)
from server_sdk.errors.wire import FailureDetails

# Deprecated (removal v4.0) and deliberately absent from __all__, but kept
# importable so the `except HandlerError` sites and any app already importing
# it keep working. isort: skip keeps this off the line above, so the noqa stays
# scoped to HandlerError rather than silencing the whole import statement.
from server_sdk.errors.base import HandlerError  # noqa: F401  # isort: skip

__all__ = [
    "AppError",
    "Audience",
    "FailureCategory",
    "FailureDetails",
    "AlreadyExistsError",
    "AuthError",
    "CancelledError",
    "DataIntegrityError",
    "DependencyUnavailableError",
    "InternalError",
    "InvalidInputError",
    "NotFoundError",
    "AppPermissionDeniedError",
    "PreconditionError",
    "RateLimitedError",
    "ResourceExhaustedError",
    "SourceUnavailableError",
    "AppTimeoutError",
    "UnimplementedError",
]
