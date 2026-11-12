"""Typed error leaves for distributed locking.

.. deprecated:: 3.57.0
    The lock path that raises them is not wired in v3. Removed in v4.0.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import deprecated

from application_sdk.errors.leaves import DependencyUnavailableError, InvalidInputError


@deprecated(
    "application_sdk.execution._temporal._lock_errors.LockAcquisitionError is deprecated and "
    "has no effect: create_worker never registers the lock interceptor or the "
    "lock activities that raise it. Will be removed in v4.0.0."
)
@dataclass(kw_only=True)
class LockAcquisitionError(DependencyUnavailableError):
    """Distributed lock could not be acquired."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_LOCK_ACQUISITION"
    message: str = "Lock not acquired; will retry after some time"
    service: str | None = "dapr_lock"


@deprecated(
    "application_sdk.execution._temporal._lock_errors.MaxLocksInvalidError is deprecated and "
    "has no effect: create_worker never registers the lock interceptor or the "
    "lock activities that raise it. Will be removed in v4.0.0."
)
@dataclass(kw_only=True)
class MaxLocksInvalidError(InvalidInputError):
    """``max_locks`` parameter is <= 0."""

    code: ClassVar[str] = "INVALID_INPUT_MAX_LOCKS"
    message: str = "max_locks must be greater than 0"
    field: str | None = "max_locks"
    constraint: str | None = "positive_integer"


@deprecated(
    "application_sdk.execution._temporal._lock_errors.RedisLockError is deprecated and "
    "has no effect: create_worker never registers the lock interceptor or the "
    "lock activities that raise it. Will be removed in v4.0.0."
)
@dataclass(kw_only=True)
class RedisLockError(DependencyUnavailableError):
    """Redis connection or operation failed during lock acquisition."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS_LOCK"
    service: str | None = "redis"


@deprecated(
    "application_sdk.execution._temporal._lock_errors.MissingScheduleToCloseTimeoutError is deprecated and "
    "has no effect: create_worker never registers the lock interceptor or the "
    "lock activities that raise it. Will be removed in v4.0.0."
)
@dataclass(kw_only=True)
class MissingScheduleToCloseTimeoutError(InvalidInputError):
    """Activity decorated with @needs_lock was called without schedule_to_close_timeout."""

    code: ClassVar[str] = "INVALID_INPUT_LOCK_TIMEOUT_REQUIRED"
    message: str = (
        "Activities with @needs_lock must be called with schedule_to_close_timeout"
    )
    field: str | None = "schedule_to_close_timeout"
    activity: str | None = None
