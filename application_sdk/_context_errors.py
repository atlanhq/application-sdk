"""Context errors raised by code that ships in the api distribution.

Defined here, not in ``application_sdk.app``, so the handler surface can raise
them on an api-only install. ``application_sdk.app.base`` and
``application_sdk.app.base_errors`` re-export them under their usual names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import APP_CONTEXT_ERROR, ErrorCode
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


class AppContextError(_InternalError):
    """Raised when App or task context is accessed outside of valid execution scope.

    This is a programming error — it indicates that context-dependent methods
    (e.g. ``self.context``, ``self.heartbeat()``) were called outside of a
    workflow run or @task execution.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_CONTEXT_ERROR
    code: ClassVar[str] = "INTERNAL_APP_CONTEXT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        _InternalError.__init__(self, message=message)
        self._legacy_error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._legacy_error_code
            if self._legacy_error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"
