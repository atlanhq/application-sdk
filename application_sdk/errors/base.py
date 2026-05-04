"""AppError — canonical SDK exception base (kw-only dataclass)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from application_sdk.errors.categories import Audience, FailureCategory

if TYPE_CHECKING:
    from application_sdk.errors.wire import FailureDetails

# Fields present on every AppError — excluded from the wire `evidence` dict.
_BASE_FIELDS: frozenset[str] = frozenset(
    {"message", "retryable", "cause", "app_name", "run_id", "suggested_action"}
)


@dataclass(kw_only=True)
class AppError(Exception):
    """Canonical SDK exception base.

    Subclass one of the categorical leaves (AuthError, AppNotFoundError, …)
    to define a typed error. Add dataclass fields to carry structured
    evidence — they appear automatically in ``to_failure_details()``.
    """

    message: str
    retryable: bool | None = None
    cause: BaseException | None = None
    app_name: str | None = None
    run_id: str | None = None
    suggested_action: str | None = None

    category: ClassVar[FailureCategory] = FailureCategory.INTERNAL
    default_retryable: ClassVar[bool] = False
    code: ClassVar[str] = "INTERNAL"
    audience: ClassVar[Audience] = Audience.UNKNOWN

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        if self.cause is not None and self.__cause__ is None:
            self.__cause__ = self.cause

    def __str__(self) -> str:
        return self.message

    @property
    def effective_retryable(self) -> bool:
        """Per-instance retryable, falling back to class default."""
        return self.default_retryable if self.retryable is None else self.retryable

    @property
    def qualified_code(self) -> str:
        """``CATEGORY.CODE`` string for log lines and human-readable surfaces."""
        return f"{self.category.name}.{self.code}"

    def to_failure_details(self) -> FailureDetails:
        """Build the Pydantic wire envelope from this error's dataclass fields.

        Non-base fields become ``evidence``. The Error dataclass is the schema
        source — no separate model to keep in sync.
        """
        from application_sdk.errors.wire import FailureDetails  # noqa: PLC0415

        evidence: dict[str, Any] = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name not in _BASE_FIELDS
        }
        return FailureDetails(
            category=self.category,
            code=self.code,
            retryable=self.effective_retryable,
            audience=type(self).audience,
            message=self.message,
            suggested_action=self.suggested_action,
            evidence=evidence,
            app_name=self.app_name,
            run_id=self.run_id,
            cause_repr=repr(self.cause) if self.cause else None,
        )
