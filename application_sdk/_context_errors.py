"""Context errors raised by code that ships in the api distribution.

Defined here, not in ``application_sdk.app``, so the handler surface can raise
them on an api-only install. ``application_sdk.app.base`` and
``application_sdk.app.base_errors`` re-export them under their usual names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk import _deprecation
from application_sdk.errors import _legacy_codes
from application_sdk.errors._legacy_codes import ErrorCode
from application_sdk.errors.base import _LegacyErrorCodeMixin
from application_sdk.errors.leaves import InternalError as _InternalError
from application_sdk.errors.leaves import PreconditionError


@dataclass(kw_only=True)
class ObjectStoreNotConfiguredError(PreconditionError):
    """Object store required by a @task but not configured in the deployment."""

    code: ClassVar[str] = "PRECONDITION_OBJECT_STORE_NOT_CONFIGURED"
    message: str = (
        "No object store configured. "
        "Ensure the deployment has a storage binding or APP_STORAGE_ROOT set."
    )
    resource: str | None = "object_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class SecretStoreNotConfiguredError(PreconditionError):
    """Secret store required by get_secret / resolve_credential but not configured."""

    code: ClassVar[str] = "PRECONDITION_SECRET_STORE_NOT_CONFIGURED"
    message: str = "No secret store configured"
    resource: str | None = "secret_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class UpstreamObjectStoreNotConfiguredError(PreconditionError):
    """ENABLE_ATLAN_UPLOAD is on, but the upstream object store did not resolve."""

    code: ClassVar[str] = "PRECONDITION_UPSTREAM_OBJECT_STORE_NOT_CONFIGURED"
    message: str = (
        "ENABLE_ATLAN_UPLOAD is true but no upstream object store resolved. "
        "Falling back to the deployment store would report success while the "
        "artifacts never reach Atlan's bucket. Check the Dapr component named "
        "by UPSTREAM_OBJECT_STORE_NAME and the secrets it references."
    )
    resource: str | None = "upstream_object_store"
    expected_state: str | None = "configured"


@dataclass(kw_only=True)
class AppContextError(_LegacyErrorCodeMixin, _InternalError):
    """Raised when App or task context is accessed outside of valid execution scope.

    This is a programming error — it indicates that context-dependent methods
    (e.g. ``self.context``, ``self.heartbeat()``) were called outside of a
    workflow run or @task execution.
    """

    code: ClassVar[str] = "INTERNAL_APP_CONTEXT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        _InternalError.__init__(self, message=message)
        self._set_legacy_error_code(error_code)


#: The AAF-* constants this module imported before FND-4299, kept importable
#: from here with a warning until v4.0.0. Literal per entry: the surface gate
#: and gen-deprecations read this table statically.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "APP_CONTEXT_ERROR": (
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
