"""State store abstraction."""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from application_sdk.errors import STATE_STORE_ERROR, ErrorCode
from application_sdk.errors.leaves import DependencyUnavailableError


class StateStoreError(DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.StateStoreUnavailableError`` — removed in v4.0."""

    #: Leaf instances never run this ``__init__``; the class default keeps the
    #: inherited ``error_code`` and ``__str__`` working on them.
    _error_code: ErrorCode | None = None
    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STATE_STORE_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STATE_STORE"

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

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.key:
            parts.append(f"key={self.key}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


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
