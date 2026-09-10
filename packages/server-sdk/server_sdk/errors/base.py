"""Error base.

Each error carries a :class:`FailureCategory` (the HTTP boundary reads it to
pick a status code), a stable ``code``, and a ``retryable`` flag;
``to_failure_details`` projects those into the typed :class:`FailureDetails`
wire model that ``PreflightCheck.error`` holds. Arbitrary keyword arguments are
accepted and retained as ``evidence``.
"""

from __future__ import annotations

from typing import Any

from server_sdk.errors.categories import FailureCategory
from server_sdk.errors.wire import FailureDetails


class AppError(Exception):
    """Base application error.

    Accepts ``message`` plus arbitrary context kwargs (``component``,
    ``invariant``, ``field``, ``constraint``, ``service``, ``target``,
    ``auth_method``, ``failure_reason``, ``suggested_action``, ...) which are
    retained for logging and surfaced as ``evidence`` via
    :meth:`to_failure_details`. Subclasses override ``category`` / ``code`` /
    ``retryable``.
    """

    category: FailureCategory = FailureCategory.INTERNAL
    code: str = "INTERNAL"
    retryable: bool = False

    def __init__(self, message: str = "", **context: Any) -> None:
        self.message = message
        self.context = context
        super().__init__(message)

    @property
    def suggested_action(self) -> str:
        return str(self.context.get("suggested_action", ""))

    def to_failure_details(self) -> FailureDetails:
        # Everything that isn't a first-class FailureDetails field is stringified
        # into evidence, so the diagnostic context is preserved.
        evidence = {
            k: str(v) for k, v in self.context.items() if k != "suggested_action"
        }
        return FailureDetails(
            category=self.category,
            code=self.code,
            retryable=self.retryable,
            message=self.message,
            suggested_action=self.suggested_action or None,
            evidence=evidence,
        )


class HandlerError(AppError):
    """Deprecated-but-supported error that carries an explicit HTTP status.

    The service boundary catches ``HandlerError`` first so a handler that has
    already decided on a status code keeps it; plain :class:`AppError` leaves
    map through the category table instead.
    """

    def __init__(
        self, message: str = "", *, http_status: int = 500, **context: Any
    ) -> None:
        self.http_status = http_status
        super().__init__(message, **context)
