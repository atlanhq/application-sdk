"""I/O bindings abstraction."""

from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from application_sdk.errors import BINDING_ERROR, ErrorCode


class BindingError(Exception):
    """Raised when binding operations fail."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = BINDING_ERROR

    def __init__(
        self,
        message: str,
        *,
        binding_name: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.binding_name = binding_name
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
        if self.binding_name:
            parts.append(f"binding={self.binding_name}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(frozen=True)
class BindingRequest:
    """Request to invoke a binding."""

    operation: str
    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BindingResponse:
    """Response from a binding invocation."""

    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class Binding(Protocol):
    """Protocol for generic bindings.

    Bindings provide an abstraction over external resources like
    storage, queues, HTTP endpoints, etc.
    """

    @property
    def name(self) -> str:
        """Name of this binding."""
        ...

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse:
        """Invoke the binding.

        Args:
            operation: Operation name (binding-specific).
            data: Optional data payload.
            metadata: Optional metadata.

        Returns:
            Binding response.

        Raises:
            BindingError: If invocation fails.
        """
        ...


class InputBinding(Protocol):
    """Protocol for input bindings (event sources).

    Input bindings receive events from external systems.
    """

    @property
    def name(self) -> str:
        """Name of this binding."""
        ...

    async def read(self) -> tuple[bytes, dict[str, str]]:
        """Read the next event.

        Returns:
            Tuple of (data, metadata).

        Raises:
            BindingError: If read fails.
        """
        ...


class OutputBinding(Protocol):
    """Protocol for output bindings (event sinks).

    Output bindings send events to external systems.
    """

    @property
    def name(self) -> str:
        """Name of this binding."""
        ...

    async def write(
        self,
        data: bytes,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Write data to the binding.

        Args:
            data: Data to write.
            metadata: Optional metadata.

        Raises:
            BindingError: If write fails.
        """
        ...
