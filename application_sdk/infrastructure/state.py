"""State store abstraction."""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from application_sdk.errors import STATE_STORE_ERROR, ErrorCode
from application_sdk.errors.base import _LegacyErrorCodeMixin
from application_sdk.errors.leaves import DependencyUnavailableError


class StateStoreError(_LegacyErrorCodeMixin, DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.StateStoreUnavailableError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STATE_STORE_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STATE_STORE"
    _STR_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("key", "key"),
        ("operation", "operation"),
    )

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "StateStoreError is deprecated; use application_sdk.infrastructure.StateStoreUnavailableError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        DependencyUnavailableError.__init__(self, message=message, cause=cause)
        self.key = key
        self.operation = operation
        self._error_code = error_code


@dataclass(kw_only=True)
class StateStoreUnavailableError(StateStoreError):
    """A state-store read, write or delete failed.

    Raised by the SDK's own ``StateStore`` implementations. It subclasses the deprecated
    ``StateStoreError`` only until v4.0, so ``except StateStoreError:`` clauses keep matching
    what the SDK raises; constructing it does not warn. v4.0 deletes
    ``StateStoreError`` and re-bases this class onto ``DependencyUnavailableError``.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STATE_STORE"
    key: str | None = None
    operation: str | None = None


class StateStore(Protocol):
    """Protocol for state storage.

    State stores provide key-value storage for Apps.
    The underlying implementation (DAPR, Redis, etc.) is hidden.
    """

    async def save(self, key: str, value: dict[str, Any]) -> None: ...
    async def load(self, key: str) -> dict[str, Any] | None: ...
    async def delete(self, key: str) -> bool: ...
    async def list_keys(self, prefix: str = "") -> list[str]: ...
