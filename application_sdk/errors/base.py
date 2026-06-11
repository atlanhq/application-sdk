"""AppError — canonical SDK exception base (kw-only dataclass)."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from application_sdk.errors.categories import Audience, FailureCategory

if TYPE_CHECKING:
    from application_sdk.errors.wire import FailureDetails

# Fields present on every AppError — excluded from the wire `evidence` dict.
_BASE_FIELDS: frozenset[str] = frozenset(
    {"message", "retryable", "cause", "app_name", "run_id", "suggested_action"}
)

_CAUSE_MAX_LEN = 500
# Matches userinfo in URLs for any scheme: https://user:pass@host → https://***@host,
# postgresql://user:pass@host → postgresql://***@host (SQLAlchemy/JDBC-style
# connection strings embed credentials the same way http URLs do).
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^@\s]+@", re.IGNORECASE)
# Matches secret query params: api_key=value → api_key=***
_SECRET_PARAM_RE = re.compile(
    r"(?i)((?:api_key|access_token|auth_token|password|passwd|secret|credential|private_key)=)[^\s&,;#]+",
)


def redact_secrets(text: str) -> str:
    """Redact URL userinfo and known secret query-params from arbitrary text.

    Use this when logging strings that may embed credentials but are not a
    single cause exception — e.g. a formatted traceback whose frames are worth
    keeping but whose driver messages embed connection-string passwords.
    """
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    text = _SECRET_PARAM_RE.sub(r"\1***", text)
    return text


def sanitize_cause_repr(exc: BaseException) -> str:
    """Return a length-capped, secret-redacted string for a cause exception."""
    text = redact_secrets(str(exc))
    if len(text) > _CAUSE_MAX_LEN:
        text = text[:_CAUSE_MAX_LEN] + "…"
    return f"{type(exc).__name__}: {text}"


# Backward-compat alias: the helper is load-bearing across clients/sql.py and
# credentials/errors.py, so it is public. Kept for existing internal/test imports.
_sanitize_cause_repr = sanitize_cause_repr


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
    audience: ClassVar[Audience] = Audience.APP_OWNER

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

        Tenant identity is intentionally NOT included here. The producer
        (the failing app) does not know or carry tenant context; per-tenant
        attribution is the consumer's responsibility (the Automation Engine
        or another consumer reading ``ApplicationError.details`` attaches
        tenant from its own context at ingest time).
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
            cause_repr=sanitize_cause_repr(self.cause) if self.cause else None,
        )
