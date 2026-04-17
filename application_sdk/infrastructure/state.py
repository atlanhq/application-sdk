"""State store abstraction."""

from typing import Any, ClassVar, Protocol

from application_sdk.errors import STATE_STORE_ERROR, ErrorCode


class StateStoreError(Exception):
    """Raised when state store operations fail."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = STATE_STORE_ERROR

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.key = key
        self.operation = operation
        self.cause = cause
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


class StateStore(Protocol):
    """Protocol for state storage.

    State stores provide key-value storage for Apps.
    The underlying implementation (DAPR, Redis, etc.) is hidden.
    """

    async def save(self, key: str, value: dict[str, Any]) -> None: ...
    async def load(self, key: str) -> dict[str, Any] | None: ...
    async def delete(self, key: str) -> bool: ...
    async def list_keys(self, prefix: str = "") -> list[str]: ...


def __getattr__(name: str):
    if name == "InMemoryStateStore":
        import warnings

        warnings.warn(
            "InMemoryStateStore is removed in v3. Use application_sdk.testing.mocks.MockStateStore.",
            DeprecationWarning,
            stacklevel=2,
        )
        from application_sdk.testing.mocks import MockStateStore

        return MockStateStore
    raise AttributeError(name)
