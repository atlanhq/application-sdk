"""Typed Redis error subclasses — stable wire codes for the Redis client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    AppTimeoutError,
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)


@dataclass(kw_only=True)
class RedisConnectionError(DependencyUnavailableError):
    """Redis service is unreachable or the connection failed."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS_CONNECTION"


@dataclass(kw_only=True)
class RedisTimeoutError(AppTimeoutError):
    """Redis operation timed out."""

    code: ClassVar[str] = "TIMEOUT_REDIS_OPERATION"


@dataclass(kw_only=True)
class RedisAuthError(AuthError):
    """Redis authentication failed."""

    code: ClassVar[str] = "AUTH_REDIS"


@dataclass(kw_only=True)
class RedisProtocolError(DependencyUnavailableError):
    """Redis returned an unexpected response or protocol error."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_REDIS_PROTOCOL"


@dataclass(kw_only=True)
class RedisConfigError(InvalidInputError):
    """Redis configuration is missing or invalid."""

    code: ClassVar[str] = "INVALID_INPUT_REDIS_CONFIG"
