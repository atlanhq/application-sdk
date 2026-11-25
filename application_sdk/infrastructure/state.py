"""State store abstraction."""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from application_sdk import _deprecation
from application_sdk.errors import _legacy_codes
from application_sdk.errors._legacy_codes import ErrorCode
from application_sdk.errors.base import _LegacyErrorCodeMixin
from application_sdk.errors.leaves import DependencyUnavailableError


@dataclass(kw_only=True)
class StateStoreError(_LegacyErrorCodeMixin, DependencyUnavailableError):
    """Deprecated: use ``application_sdk.infrastructure.StateStoreUnavailableError`` — removed in v4.0."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STATE_STORE"
    _STR_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("key", "key"),
        ("operation", "operation"),
    )
    key: str | None = None
    operation: str | None = None

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
        self._set_legacy_error_code(error_code)


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


#: The AAF-* constants this module imported before FND-4299, kept importable
#: from here with a warning until v4.0.0. Literal per entry: the surface gate
#: and gen-deprecations read this table statically.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "STATE_STORE_ERROR": (
        "the code ClassVar of a typed application_sdk.errors.AppError subclass",
        "AAF-* codes are a second error vocabulary beside code",
    ),
}

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=lambda name: getattr(_legacy_codes, name),
)
