"""Concrete error leaves used on the serving path.

Each leaf pins the :class:`FailureCategory` the HTTP boundary maps to a status
code.
"""

from __future__ import annotations

from server_sdk.errors.base import AppError
from server_sdk.errors.categories import FailureCategory


class AuthError(AppError):
    """Authentication / authorization failure."""

    category = FailureCategory.AUTH
    code = "AUTH"


class InvalidInputError(AppError):
    """Caller supplied invalid input (bad/missing field)."""

    category = FailureCategory.INVALID_INPUT
    code = "INVALID_INPUT"


class InternalError(AppError):
    """Unexpected internal failure / broken invariant."""

    category = FailureCategory.INTERNAL
    code = "INTERNAL"


class DependencyUnavailableError(AppError):
    """A required upstream dependency (driver, cloud API, service) was unavailable."""

    category = FailureCategory.DEPENDENCY_UNAVAILABLE
    code = "DEPENDENCY_UNAVAILABLE"
    retryable = True
