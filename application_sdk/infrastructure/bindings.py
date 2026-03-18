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


class StorageBinding:
    """Storage binding abstraction for blob/file storage.

    Provides a higher-level interface over raw bindings for
    common storage operations.
    """

    def __init__(self, binding: Binding) -> None:
        self._binding = binding

    @property
    def name(self) -> str:
        """Name of the underlying binding."""
        return self._binding.name

    async def get(self, key: str) -> bytes | None:
        """Get an object by key.

        Args:
            key: Object key/path.

        Returns:
            Object data, or None if not found.
        """
        try:
            response = await self._binding.invoke(
                operation="get",
                metadata={"key": key},
            )
            return response.data
        except BindingError:
            return None

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        """Put an object.

        Args:
            key: Object key/path.
            data: Object data.
            content_type: Optional content type.
        """
        metadata: dict[str, str] = {"key": key}
        if content_type:
            metadata["contentType"] = content_type

        await self._binding.invoke(
            operation="create",
            data=data,
            metadata=metadata,
        )

    async def delete(self, key: str) -> bool:
        """Delete an object.

        Args:
            key: Object key/path.

        Returns:
            True if deleted, False if not found.
        """
        try:
            await self._binding.invoke(
                operation="delete",
                metadata={"key": key},
            )
            return True
        except BindingError:
            return False

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List objects with optional prefix.

        Args:
            prefix: Key prefix to filter by.

        Returns:
            List of object keys.
        """
        response = await self._binding.invoke(
            operation="list",
            metadata={"prefix": prefix} if prefix else {},
        )
        if response.data:
            import json

            return json.loads(response.data.decode())
        return []


class InMemoryBinding:
    """In-memory binding for testing."""

    def __init__(self, name: str = "test-binding") -> None:
        self._name = name
        self._data: dict[str, bytes] = {}

    @property
    def name(self) -> str:
        return self._name

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse:
        """Handle binding operations."""
        metadata = metadata or {}

        if operation == "get":
            key = metadata.get("key", "")
            result = self._data.get(key)
            return BindingResponse(data=result)

        elif operation == "create":
            key = metadata.get("key", "")
            if data:
                self._data[key] = data
            return BindingResponse()

        elif operation == "delete":
            key = metadata.get("key", "")
            self._data.pop(key, None)
            return BindingResponse()

        elif operation == "list":
            import json

            prefix = metadata.get("prefix", "")
            keys = [k for k in self._data.keys() if k.startswith(prefix)]
            return BindingResponse(data=json.dumps(keys).encode())

        else:
            raise BindingError(
                f"Unknown operation: {operation}",
                binding_name=self._name,
                operation=operation,
            )

    def clear(self) -> None:
        """Clear all data (for testing)."""
        self._data.clear()
