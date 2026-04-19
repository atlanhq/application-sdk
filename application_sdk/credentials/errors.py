"""Credential exception hierarchy."""

from typing import ClassVar

from application_sdk.errors import (
    CREDENTIAL_ERROR,
    CREDENTIAL_NOT_FOUND,
    CREDENTIAL_PARSE_ERROR,
    CREDENTIAL_VALIDATION_ERROR,
    ErrorCode,
)


class CredentialError(Exception):
    """Base exception for credential errors."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        credential_name: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.credential_name = credential_name
        self.cause = cause
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
    """Raised when a credential ref could not be resolved from the secret store."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_NOT_FOUND

    def __init__(self, credential_name: str) -> None:
        super().__init__(
            f"Credential '{credential_name}' not found",
            credential_name=credential_name,
        )


class CredentialParseError(CredentialError):
    """Raised when JSON from the secret store doesn't match the expected schema."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_PARSE_ERROR


class CredentialValidationError(CredentialError):
    """Raised when a typed credential fails its validate() check."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CREDENTIAL_VALIDATION_ERROR
