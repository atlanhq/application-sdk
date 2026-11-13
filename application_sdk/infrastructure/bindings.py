"""I/O bindings abstraction."""

import warnings
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from typing_extensions import deprecated

from application_sdk.errors import BINDING_ERROR, ErrorCode
from application_sdk.errors.leaves import DependencyUnavailableError


class BindingError(DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.BindingUnavailableError`` — removed in v4.0."""

    #: Leaf instances never run this ``__init__``; the class default keeps the
    #: inherited ``error_code`` and ``__str__`` working on them.
    _error_code: ErrorCode | None = None
    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = BINDING_ERROR
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_BINDING"

    def __init__(
        self,
        message: str,
        *,
        binding_name: str | None = None,
        operation: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "BindingError is deprecated; use application_sdk.infrastructure.BindingUnavailableError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        DependencyUnavailableError.__init__(self, message=message, cause=cause)
        self.binding_name = binding_name
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
        if self.binding_name:
            parts.append(f"binding={self.binding_name}")
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class BindingUnavailableError(BindingError):
    """A binding invocation failed: the sidecar or the bound resource did not answer.

    Raised by the SDK's own ``Binding`` implementations. It subclasses the deprecated
    ``BindingError`` only until v4.0, so ``except BindingError:`` clauses keep matching
    what the SDK raises; constructing it does not warn. v4.0 deletes
    ``BindingError`` and re-bases this class onto ``DependencyUnavailableError``.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_BINDING"
    binding_name: str | None = None
    operation: str | None = None


@deprecated(
    "BindingRequest is deprecated and will be removed in v4.0.0. Nothing in the "
    "SDK accepts one; pass operation, data and metadata to Binding.invoke() "
    "instead."
)
@dataclass(frozen=True)
class BindingRequest:
    """Deprecated: request to invoke a binding — removed in v4.0.0.

    Nothing accepts one. Pass the fields to ``Binding.invoke()`` directly.
    """

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
            BindingUnavailableError: If invocation fails.
        """
        ...


@deprecated(
    "InputBinding is deprecated and will be removed in v4.0.0. The SDK ships no "
    "implementation and has no input-binding replacement; to receive messages, "
    "use SubscriptionConfig with create_app_handler_service(subscriptions=...)."
)
class InputBinding(Protocol):
    """Deprecated: protocol for input bindings — removed in v4.0.0.

    The SDK ships no implementation and has no input-binding replacement.
    To receive messages, pass a ``SubscriptionConfig`` to
    ``create_app_handler_service(subscriptions=...)``.
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


@deprecated(
    "OutputBinding is deprecated and will be removed in v4.0.0. The SDK ships no "
    "implementation; use Binding (DaprBinding.invoke) instead."
)
class OutputBinding(Protocol):
    """Deprecated: protocol for output bindings — removed in v4.0.0.

    The SDK ships no implementation. Use :class:`Binding`.
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
