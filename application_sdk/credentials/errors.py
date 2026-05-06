"""Credential exception hierarchy.

Domain errors from the credentials subsystem.  Each specialised class inherits
from the appropriate categorical leaf (first base, so ``category`` ClassVar
resolves there first) and from ``CredentialError`` (second base, so
``except CredentialError:`` domain-catch blocks keep working).

MRO convention: categorical leaf first, domain base second.
"""

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    CREDENTIAL_ERROR,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_PARSE_ERROR,
    CREDENTIAL_VALIDATION_ERROR,
    ErrorCode,
)
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import AuthError, InvalidInputError, NotFoundError


@dataclass(kw_only=True)
class CredentialError(AuthError):
    """Generic credential-subsystem failure (category=AUTH).

    Use more specific subclasses when the failure mode is known.
    """

    credential_name: str | None = None

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_ERROR
    code: ClassVar[str] = "CREDENTIAL"

    # Intentional: dataclass fields define the wire-evidence schema; custom __init__ preserves positional-message compat.
    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        AuthError.__init__(self, message=message, cause=cause)
        self.credential_name = credential_name
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
        if self.credential_name:
            parts.append(f"credential={self.credential_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class CredentialNotFoundError(NotFoundError, CredentialError):
    """The requested credential was not found in the secret store or registry.

    Categorical parent is ``NotFoundError`` (category=NOT_FOUND); domain
    parent is ``CredentialError`` so ``except CredentialError:`` still catches.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_NOT_FOUND
    code: ClassVar[str] = "CREDENTIAL_NOT_FOUND"
    category: ClassVar[FailureCategory] = FailureCategory.NOT_FOUND
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(self, credential_name: str) -> None:
        NotFoundError.__init__(
            self, message=f"Credential '{credential_name}' not found"
        )
        self.credential_name = credential_name
        self._error_code = CREDENTIAL_NOT_FOUND

    @property
    def error_code(self) -> ErrorCode:
        return self._error_code

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.credential_name:
            parts.append(f"credential={self.credential_name}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class CredentialParseError(InvalidInputError, CredentialError):
    """Credential data could not be parsed (malformed payload).

    Categorical parent is ``InvalidInputError`` (category=INVALID_INPUT);
    domain parent is ``CredentialError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_PARSE_ERROR
    code: ClassVar[str] = "CREDENTIAL_PARSE"
    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        InvalidInputError.__init__(self, message=message, cause=cause)
        self.credential_name = credential_name
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
        if self.credential_name:
            parts.append(f"credential={self.credential_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)


@dataclass(kw_only=True)
class CredentialValidationError(InvalidInputError, CredentialError):
    """Credential failed schema or business-rule validation.

    Categorical parent is ``InvalidInputError`` (category=INVALID_INPUT);
    domain parent is ``CredentialError``.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VALIDATION_ERROR
    code: ClassVar[str] = "CREDENTIAL_VALIDATION"
    category: ClassVar[FailureCategory] = FailureCategory.INVALID_INPUT
    default_retryable: ClassVar[bool] = False
    audience: ClassVar[Audience] = Audience.USER

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        InvalidInputError.__init__(self, message=message, cause=cause)
        self.credential_name = credential_name
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
        if self.credential_name:
            parts.append(f"credential={self.credential_name}")
        if self.cause:
            parts.append(f"caused_by={type(self.cause).__name__}: {self.cause}")
        return " | ".join(parts)
