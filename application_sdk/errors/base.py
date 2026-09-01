"""AppError — canonical SDK exception base (kw-only dataclass)."""

from __future__ import annotations

import dataclasses
import re
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from application_sdk.errors.categories import Audience, FailureCategory

if TYPE_CHECKING:
    from application_sdk.errors.wire import FailureDetails

# Fields present on every AppError — excluded from the wire `evidence` dict.
_BASE_FIELDS: frozenset[str] = frozenset(
    {"message", "retryable", "cause", "app_name", "run_id", "suggested_action"}
)

# Cap for the cause string carried on the wire envelope. Sized so a full
# provider error response survives intact. An object-store error's ``str()`` is
# "[preamble + request URL][provider XML/JSON body][Rust ``Debug source:`` dump]"
# — measured at ~800 chars for a typical artifact key. The previous 500 spent
# 374 on the URL preamble alone and cut the provider's ``<Message>`` in half,
# deleting the only sentence that named what the store rejected. (FND-957)
_CAUSE_MAX_LEN = 2000
# Past the cap, keep BOTH ends rather than the head only: a backend error puts
# the request URL at the head and what the provider said at the tail, so a
# head-only cut spends the whole budget on boilerplate. The two lengths sum to
# less than _CAUSE_MAX_LEN so the elision marker only appears when there is
# genuinely something elided.
_CAUSE_HEAD_LEN = 1200
_CAUSE_TAIL_LEN = 700
_TRACEBACK_MAX_LEN = 8000
# Matches userinfo in URLs for any scheme: https://user:pass@host → https://***@host,
# postgresql://user:pass@host → postgresql://***@host (SQLAlchemy/JDBC-style
# connection strings embed credentials the same way http URLs do).
# `(?:[^@\s]+@)+` consumes *all* userinfo segments greedily so a raw `@` inside
# the password (postgresql://u:p@ss@host) doesn't leave the tail exposed. This
# is greedy up to the last `@` in a whitespace-free run, so it can over-redact a
# trailing `@` in a no-space query string — the safe failure direction for a
# secret redactor.
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)(?:[^@\s]+@)+", re.IGNORECASE)
# Matches secret query params: api_key=value → api_key=***
# ``pwd`` covers ODBC/DSN keyword syntax (``UID=sa;PWD=…``), which no other
# keyword here matches — ODBC connectors do not use ``password=``.
# The value alternation tries a braced value first: ODBC quotes values that
# contain the ``;`` separator as ``PWD={secret;with;semicolons}``, and the
# bare-value class ``[^\s&,;#]+`` would stop at the first ``;`` inside the
# braces and leak the password tail. ``\{[^}]*\}`` consumes the braces as a
# unit so only the closing brace survives; an *escaped* closing brace
# (``}}`` per the ODBC spec) still ends the match at the first ``}`` — the
# residue is then a brace fragment, not usable secret material. The bare
# class still stops at ``;``, so the following key=value pair survives.
# ``uid`` is deliberately absent. It is a user name, not a credential, and
# dropping it would remove "which account failed to log in" from every auth
# failure. It also has no word boundary in this alternation, so it would match
# the tail of ``run_guid=`` and ``correlation_uuid=`` — redacting the exact
# correlation IDs an on-call needs.
# ``signature`` and ``sig`` cover the presigned-URL query params every cloud
# store uses for bearer-equivalent material: ``X-Goog-Signature`` (GCS),
# ``X-Amz-Signature`` (S3) and ``sig`` (Azure SAS). None of them matched the
# keyword list before, so a signed URL embedded in a driver error survived
# redaction intact — while ``X-Goog-Credential`` next to it was redacted,
# because it happens to end in ``credential``. The alternation has no left-hand
# word boundary, so bare ``signature`` also covers the ``X-Goog-``/``X-Amz-``
# prefixed forms. ``signature`` precedes ``sig`` for readability only: the
# mandatory ``=`` means a short alternative cannot shadow a longer one.
#
# A generic ``token`` is deliberately NOT here, for the same reason ``uid`` is
# not: it would redact ``next_token=`` / ``page_token=`` /
# ``continuation_token=``, which are the pagination cursors an on-call needs to
# see. The list stays an enumeration of things that are only ever credentials.
_SECRET_PARAM_RE = re.compile(
    r"(?i)((?:api_key|access_token|auth_token|password|passwd|pwd|secret|credential|private_key|signature|sig)=)(?:\{[^}]*\}|[^\s&,;#]+)",
)


def redact_secrets(text: str) -> str:
    """Redact URL userinfo and known secret query-params from a string.

    Use this when logging strings that may embed credentials but are not a
    single cause exception — e.g. a formatted traceback whose frames are worth
    keeping but whose driver messages embed connection-string passwords.

    ``text`` must be a ``str`` — callers holding an exception or other object
    should stringify first (the sibling :func:`sanitize_cause_repr` does this
    for cause exceptions). Non-``str`` input raises ``TypeError`` via ``re``.
    """
    text = _URL_USERINFO_RE.sub(r"\1***@", text)
    text = _SECRET_PARAM_RE.sub(r"\1***", text)
    return text


# ``object_store`` (via obstore) appends a multi-line Rust ``Debug`` dump to
# every error's ``str()``. It sits *after* the provider's XML/JSON body, so it
# competes with the diagnostic for the budget — and a keep-the-tail truncation
# would preserve the dump and drop the body. Measured on a real GCS write
# failure: [374 chars URL+status][206 chars provider XML][218 chars Debug dump].
# Strip it before capping; the routing-relevant facts reach consumers as typed
# evidence instead. (FND-957)
_DEBUG_SOURCE_TAIL_RE = re.compile(r"\n+Debug source:\n.*\Z", re.DOTALL)


def sanitize_cause_repr(exc: BaseException) -> str:
    """Return a length-capped, secret-redacted string for a cause exception.

    Truncation keeps both ends. A backend error puts the request URL at the
    head and the reason at the tail, so a head-only cut spends the whole
    budget on boilerplate and deletes the diagnostic. Redaction runs *before*
    truncation, so retaining a tail can never expose an unredacted secret.
    (FND-957)
    """
    text = _DEBUG_SOURCE_TAIL_RE.sub("", redact_secrets(str(exc)))
    if len(text) > _CAUSE_MAX_LEN:
        elided = len(text) - _CAUSE_HEAD_LEN - _CAUSE_TAIL_LEN
        text = (
            text[:_CAUSE_HEAD_LEN]
            + f"…[{elided} chars elided]…"
            + text[-_CAUSE_TAIL_LEN:]
        )
    return f"{type(exc).__name__}: {text}"


def safe_traceback(exc: BaseException | None, max_len: int = _TRACEBACK_MAX_LEN) -> str:
    """Return a secret-redacted, length-capped full-frame traceback.

    For logging a traceback whose frames are worth keeping but whose driver
    messages may embed connection-string passwords. Redacts URL userinfo and
    known secret params, then caps the total length with an ellipsis marker.
    Returns ``""`` for ``None``; an exception never raised (no ``__traceback__``)
    yields just its formatted type/message line.
    """
    if exc is None:
        return ""
    text = redact_secrets("".join(traceback.format_exception(exc)))
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


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
