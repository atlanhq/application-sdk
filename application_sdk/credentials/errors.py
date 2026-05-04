"""Credential exception hierarchy.

Legacy classes below are deprecated shims that preserve back-compat for v3.x.
Migrate to ``application_sdk.errors.AuthError`` / ``InvalidInputError``.
All legacy names are removed in v4.0.
"""

import warnings
from typing import ClassVar

from application_sdk.errors import (
    CREDENTIAL_ERROR,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_PARSE_ERROR,
    CREDENTIAL_VALIDATION_ERROR,
    ErrorCode,
)
from application_sdk.errors.leaves import AuthError


class CredentialError(AuthError):
    """Deprecated: use ``application_sdk.errors.AuthError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_ERROR
    code: ClassVar[str] = "CREDENTIAL"

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "CredentialError is deprecated; use application_sdk.errors.AuthError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
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


class CredentialNotFoundError(CredentialError):
    """Deprecated: use ``application_sdk.errors.AuthError`` with ``code='CREDENTIAL_NOT_FOUND'``
    — removed in v4.0.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_NOT_FOUND
    code: ClassVar[str] = "CREDENTIAL_NOT_FOUND"

    def __init__(self, credential_name: str) -> None:
        warnings.warn(
            "CredentialNotFoundError is deprecated; use application_sdk.errors.AuthError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        # Call the AppError leaf directly to avoid double-warning from CredentialError.__init__.
        AuthError.__init__(
            self,
            message=f"Credential '{credential_name}' not found",
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


class CredentialParseError(CredentialError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_PARSE_ERROR
    code: ClassVar[str] = "CREDENTIAL_PARSE"

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "CredentialParseError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
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


class CredentialValidationError(CredentialError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VALIDATION_ERROR
    code: ClassVar[str] = "CREDENTIAL_VALIDATION"

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "CredentialValidationError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
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
