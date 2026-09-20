"""Concrete error leaves used on the serving path.

Each leaf pins the :class:`FailureCategory` the HTTP boundary maps to a status
code, and there is exactly one leaf per category (asserted in the suite).

That completeness is the point. Only four leaves shipped originally, so a
connector needing any other category had to subclass :class:`AppError` and
redeclare ``category`` itself — which the conformance suite flags as taxonomy
drift (P002), and which every SQL connector therefore does, ten times over in
one file. Inheriting the right leaf is the fix; redeclaring was only ever a
workaround for the leaf not existing.
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


class AppPermissionDeniedError(AppError):
    """Authenticated, but not permitted to do this."""

    category = FailureCategory.PERMISSION
    code = "PERMISSION"


class NotFoundError(AppError):
    """A referenced object does not exist."""

    category = FailureCategory.NOT_FOUND
    code = "NOT_FOUND"


class AlreadyExistsError(AppError):
    """The object being created already exists."""

    category = FailureCategory.ALREADY_EXISTS
    code = "ALREADY_EXISTS"


class PreconditionError(AppError):
    """The request is well-formed but the system is not in a state to serve it."""

    category = FailureCategory.PRECONDITION
    code = "PRECONDITION"


class RateLimitedError(AppError):
    """Throttled by the source or by us. Retryable: the limit is time-bounded."""

    category = FailureCategory.RATE_LIMITED
    code = "RATE_LIMITED"
    retryable = True


class AppTimeoutError(AppError):
    """An operation exceeded its deadline. Retryable."""

    category = FailureCategory.TIMEOUT
    code = "TIMEOUT"
    retryable = True


class SourceUnavailableError(AppError):
    """The customer's data source is unreachable or restarting. Retryable.

    Distinct from DEPENDENCY_UNAVAILABLE, which is OUR upstream being down: this
    one is the customer's system, and the two want different operator responses.
    """

    category = FailureCategory.SOURCE_UNAVAILABLE
    code = "SOURCE_UNAVAILABLE"
    retryable = True


class ResourceExhaustedError(AppError):
    """Out of some bounded resource (connections, memory, quota). Retryable."""

    category = FailureCategory.RESOURCE_EXHAUSTED
    code = "RESOURCE_EXHAUSTED"
    retryable = True


class DataIntegrityError(AppError):
    """The data read back violates an invariant it was required to hold."""

    category = FailureCategory.DATA_INTEGRITY
    code = "DATA_INTEGRITY"


class UnimplementedError(AppError):
    """This surface is not implemented for this app."""

    category = FailureCategory.UNIMPLEMENTED
    code = "UNIMPLEMENTED"


class CancelledError(AppError):
    """The caller went away before the work finished."""

    category = FailureCategory.CANCELLED
    code = "CANCELLED"
