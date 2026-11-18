"""Credential exception hierarchy.

Domain errors from the credentials subsystem.  Each specialised class inherits
from the appropriate categorical leaf (first base, so ``category`` ClassVar
resolves there first) and from ``CredentialError`` (second base, so
``except CredentialError:`` domain-catch blocks keep working).

MRO convention: categorical leaf first, domain base second.
"""

from dataclasses import dataclass
from typing import ClassVar, TypedDict, Unpack

from application_sdk.errors import (
    CREDENTIAL_ERROR,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_PARSE_ERROR,
    CREDENTIAL_VALIDATION_ERROR,
    ErrorCode,
)
from application_sdk.errors.base import AppError, _LegacyErrorCodeMixin
from application_sdk.errors.categories import Audience
from application_sdk.errors.leaves import AuthError, InvalidInputError, NotFoundError

# ---------------------------------------------------------------------------
# Routing / type-check errors (simple typed leaves — no custom __init__)
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class CredentialResolvableTypeError(InvalidInputError):
    """source is not a CredentialResolvable (missing extraction_method / agent_json / credential_guid)."""

    code: ClassVar[str] = "INVALID_INPUT_CREDENTIAL_RESOLVABLE_TYPE"
    message: str = "Expected a CredentialResolvable (with extraction_method, agent_json, credential_guid)"
    field: str | None = "source"


@dataclass(kw_only=True)
class CredentialRoutingError(InvalidInputError):
    """No routable credential source present in input."""

    code: ClassVar[str] = "INVALID_INPUT_CREDENTIAL_ROUTING"
    message: str = (
        "No routable credential source: need extraction_method='agent' with a "
        "non-empty agent_json, or extraction_method='direct' (or a miner's "
        "'query_history' / 's3') with a non-empty credential_guid"
    )
    field: str | None = "extraction_method"


@dataclass(kw_only=True)
class AtlanCredentialTypeError(InvalidInputError):
    """Credential is not an AtlanApiToken or AtlanOAuthClient."""

    code: ClassVar[str] = "INVALID_INPUT_ATLAN_CREDENTIAL_TYPE"
    message: str = (
        "Unsupported Atlan credential type; expected AtlanApiToken or AtlanOAuthClient"
    )
    field: str | None = "credential"


class CredentialErrorKwargs(TypedDict, total=False):
    """The keyword arguments of :meth:`CredentialError.__init__`, for delegating subclasses."""

    credential_name: str | None
    cause: Exception | None
    error_code: ErrorCode | None


@dataclass(kw_only=True)
class CredentialError(_LegacyErrorCodeMixin, AuthError):
    """Generic credential-subsystem failure (category=AUTH).

    Use more specific subclasses when the failure mode is known. A subclass
    with the same constructor sets ``_CATEGORICAL_BASE`` to its categorical
    leaf and delegates through :class:`CredentialErrorKwargs` instead of
    copying the body (the leaf comes first in the MRO, so inheriting would
    pick the leaf's generated keyword-only ``__init__``).
    """

    credential_name: str | None = None

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_ERROR
    code: ClassVar[str] = "AUTH_CREDENTIAL"
    _STR_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("credential_name", "credential"),
    )
    #: The categorical leaf whose generated ``__init__`` this one runs.
    _CATEGORICAL_BASE: ClassVar[type[AppError]] = AuthError

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ preserves positional-message compat.
    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        self._CATEGORICAL_BASE.__init__(self, message=message, cause=cause)
        self.credential_name = credential_name
        self._error_code = error_code


@dataclass(kw_only=True)
class CredentialNotFoundError(NotFoundError, CredentialError):
    """The requested credential was not found in the secret store or registry.

    Categorical parent is ``NotFoundError`` (category=NOT_FOUND); domain
    parent is ``CredentialError`` so ``except CredentialError:`` still catches.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_NOT_FOUND
    code: ClassVar[str] = "NOT_FOUND_CREDENTIAL"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(self, credential_name: str) -> None:
        NotFoundError.__init__(
            self, message=f"Credential '{credential_name}' not found"
        )
        self.credential_name = credential_name


@dataclass(kw_only=True)
class CredentialParseError(InvalidInputError, CredentialError):
    """Credential data could not be parsed (malformed payload).

    Categorical parent is ``InvalidInputError`` (category=INVALID_INPUT);
    domain parent is ``CredentialError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_PARSE_ERROR
    code: ClassVar[str] = "INVALID_INPUT_CREDENTIAL_PARSE"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER
    _CATEGORICAL_BASE: ClassVar[type[AppError]] = InvalidInputError

    def __init__(self, message: str, **kwargs: Unpack[CredentialErrorKwargs]) -> None:
        CredentialError.__init__(self, message, **kwargs)


@dataclass(kw_only=True)
class CredentialValidationError(InvalidInputError, CredentialError):
    """Credential failed schema or business-rule validation.

    Categorical parent is ``InvalidInputError`` (category=INVALID_INPUT);
    domain parent is ``CredentialError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VALIDATION_ERROR
    code: ClassVar[str] = "INVALID_INPUT_CREDENTIAL_VALIDATION"
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER
    _CATEGORICAL_BASE: ClassVar[type[AppError]] = InvalidInputError

    def __init__(self, message: str, **kwargs: Unpack[CredentialErrorKwargs]) -> None:
        CredentialError.__init__(self, message, **kwargs)
