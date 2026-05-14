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
class MissingScheduleToCloseTimeoutError(InvalidInputError):
    """Activity decorated with @needs_lock was called without schedule_to_close_timeout."""

    code: ClassVar[str] = "INVALID_INPUT_LOCK_TIMEOUT_REQUIRED"
    message: str = (
        "Activities with @needs_lock must be called with schedule_to_close_timeout"
    )
    field: str | None = "schedule_to_close_timeout"
    activity: str | None = None
