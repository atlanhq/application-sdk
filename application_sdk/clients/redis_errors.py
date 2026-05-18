"""Typed error leaves for the Redis client family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AppTimeoutError,
    DependencyUnavailableError,
    PreconditionError,
)


@dataclass(kw_only=True)
class RedisConnectionError(DependencyUnavailableError):
    """Redis connection failed or is unavailable."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS"
    message: str = "Redis connection failed"
    service: str | None = "redis"


@dataclass(kw_only=True)
class RedisTimeoutError(AppTimeoutError):
    """Redis operation timed out."""

    code: ClassVar[str] = "TIMEOUT_REDIS"
    message: str = "Redis operation timed out"
    operation: str | None = "redis"


@dataclass(kw_only=True)
class RedisProtocolError(DependencyUnavailableError):
    """Redis returned an unexpected or malformed response."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS_PROTOCOL"
    message: str = "Redis protocol or response error"
    service: str | None = "redis"


@dataclass(kw_only=True)
class RedisConfigError(PreconditionError):
    """Redis configuration is missing or invalid; deployment fix required."""

    code: ClassVar[str] = "PRECONDITION_REDIS_CONFIG"
    message: str = "Redis configuration is invalid or incomplete"
    resource: str | None = "redis"
    expected_state: str | None = "configured"
