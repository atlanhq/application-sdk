"""Typed error leaves for the Azure client family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)


@dataclass(kw_only=True)
class AzureCredentialParseError(InvalidInputError):
    """Azure credential parsing or validation failed."""

    code: ClassVar[str] = "INVALID_INPUT_AZURE_CREDENTIALS_PARSE"
    message: str = "Azure credential parsing failed"
    field: str | None = "credentials"
    received_auth_type: str | None = None
    validation_errors: str | None = None


@dataclass(kw_only=True)
class AzureCredentialTypeError(InvalidInputError):
    """Azure credential has invalid parameter types."""

    code: ClassVar[str] = "INVALID_INPUT_AZURE_CREDENTIAL_TYPE"
    message: str = "Invalid credential parameter types"
    field: str | None = "credentials"


@dataclass(kw_only=True)
class AzureCredentialError(AuthError):
    """Azure credential authentication failed."""

    code: ClassVar[str] = "AUTH_AZURE_CREDENTIAL"
    message: str = "Azure credential authentication failed"


@dataclass(kw_only=True)
class AzureClientAuthError(AuthError):
    """Azure client authentication or connection failed."""

    code: ClassVar[str] = "AUTH_AZURE_CLIENT"
    message: str = "Azure client authentication failed"


@dataclass(kw_only=True)
class AzureInputValidationError(InvalidInputError):
    """Azure operation received invalid input parameters."""

    code: ClassVar[str] = "INVALID_INPUT_AZURE_PARAMETERS"
    message: str = "Invalid Azure operation parameters"
    field: str | None = None


@dataclass(kw_only=True)
class AzureConnectionError(DependencyUnavailableError):
    """Azure service is unavailable."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AZURE"
    message: str = "Azure service is unavailable"
    service: str | None = "azure"


@dataclass(kw_only=True)
class AzureServiceError(DependencyUnavailableError):
    """Azure service returned an error."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AZURE_SERVICE"
    message: str = "Azure service error"
    service: str | None = "azure"
