"""Typed error leaves for distributed locking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError, InvalidInputError


@dataclass(kw_only=True)
class LockAcquisitionError(DependencyUnavailableError):
    """Distributed lock could not be acquired."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_LOCK_ACQUISITION"
    message: str = "Lock not acquired; will retry after some time"
    service: str | None = "dapr_lock"


@dataclass(kw_only=True)
class MaxLocksInvalidError(InvalidInputError):
    """``max_locks`` parameter is <= 0."""

    code: ClassVar[str] = "INVALID_INPUT_MAX_LOCKS"
    message: str = "max_locks must be greater than 0"
    field: str | None = "max_locks"
    constraint: str | None = "positive_integer"


@dataclass(kw_only=True)
class RedisLockError(DependencyUnavailableError):
    """Redis connection or operation failed during lock acquisition."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS_LOCK"
    service: str | None = "redis"


@dataclass(kw_only=True)
class MissingScheduleToCloseTimeoutError(InvalidInputError):
    """Activity decorated with @needs_lock was called without schedule_to_close_timeout."""

    code: ClassVar[str] = "INVALID_INPUT_LOCK_TIMEOUT_REQUIRED"
    message: str = (
        "Activities with @needs_lock must be called with schedule_to_close_timeout"
    )
    field: str | None = "schedule_to_close_timeout"
    activity: str | None = None
