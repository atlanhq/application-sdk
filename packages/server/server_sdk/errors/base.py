"""Error base.

Each error carries a :class:`FailureCategory` (the HTTP boundary reads it to
pick a status code), a stable ``code``, and a ``retryable`` flag;
``to_failure_details`` projects those into the typed :class:`FailureDetails`
wire model that ``PreflightCheck.error`` holds. Arbitrary keyword arguments are
accepted and retained as ``evidence``.
"""

from __future__ import annotations

import warnings
from typing import Any

from server_sdk.errors.categories import FailureCategory
from server_sdk.errors.redaction import sanitize_cause_repr
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

    def __init__(
        self,
        message: str = "",
        *,
        retryable: bool | None = None,
        cause: BaseException | None = None,
        app_name: str | None = None,
        run_id: str | None = None,
        **context: Any,
    ) -> None:
        # These four are named explicitly rather than left to **context. They
        # are real fields on application_sdk's AppError, so the spelling
        # `AuthError("x", retryable=True, cause=exc)` is valid Python in both
        # packages -- and here it used to land in `evidence` as the string
        # "True" while the wire said retryable: false, reporting a retryable
        # source failure as terminal. No error, no warning.
        self.message = message
        self.context = context
        if retryable is not None:
            # Shadow the class attribute per instance. NOT renamed to
            # `default_retryable`: five leaves spell it `retryable = True`, and
            # renaming only the base would silently make every one of them
            # non-retryable.
            self.retryable = retryable
        self.cause = cause
        self.app_name = app_name
        self.run_id = run_id
        super().__init__(message)
        if cause is not None and self.__cause__ is None:
            self.__cause__ = cause

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
            app_name=self.app_name,
            run_id=self.run_id,
            # Redacted and capped here; _summarize_check strips it from the
            # HTTP response, so it reaches the log and the Temporal payload only.
            cause_repr=sanitize_cause_repr(self.cause) if self.cause else None,
        )


class HandlerError(AppError):
    """Deprecated-but-supported error that carries an explicit HTTP status.

    .. deprecated:: 0.1
       Use a typed :class:`~server_sdk.errors.base.AppError` subclass from
       ``server_sdk.errors.leaves`` instead. Removed in v4.0.

    The service boundary catches ``HandlerError`` first so a handler that has
    already decided on a status code keeps it; plain :class:`AppError` leaves
    map through the category table instead.

    Deliberately absent from ``server_sdk.__all__`` and
    ``server_sdk.errors.__all__``: application_sdk has it scheduled for removal
    in v4.0, and this package is what apps are being told to move *to*, so
    advertising it here would mint new call sites at exactly the point the SDK
    is retiring it. B001 cannot catch those -- the conformance manifest is
    rooted at ``application_sdk`` -- which leaves this warning as the only
    signal. Still importable, so the three ``except HandlerError`` sites and any
    app that already imports it keep working.
    """

    def __init__(
        self, message: str = "", *, http_status: int = 500, **context: Any
    ) -> None:
        warnings.warn(
            "HandlerError is deprecated; use a typed server_sdk.errors.AppError "
            "subclass instead — will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        self.http_status = http_status
        super().__init__(message, **context)
