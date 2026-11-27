"""Typed error leaves for the Azure client family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from typing_extensions import deprecated

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialParseError,
    CredentialValidationError,
)
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)

__all__ = [
    "AzureClientAuthError",
    "AzureConnectionError",
    "AzureCredentialError",
    "AzureCredentialParseError",
    "AzureCredentialTypeError",
    "AzureInputValidationError",
    "AzureServiceError",
]


@dataclass(kw_only=True)
class AzureCredentialParseError(CredentialParseError):
    """Azure credential parsing or validation failed.

    A :class:`~application_sdk.credentials.errors.CredentialParseError`, so it
    carries that class's code (``INVALID_INPUT_CREDENTIAL_PARSE``) and
    ``except CredentialError`` catches it. Before FND-4298 it had its own code,
    ``INVALID_INPUT_AZURE_CREDENTIALS_PARSE``.
    """

    # Restated, not new: the code CredentialParseError carries. Conformance
    # P003 reads only a class's own body; test_errors pins the two equal.
    code: ClassVar[str] = "INVALID_INPUT_CREDENTIAL_PARSE"

    message: str = "Azure credential parsing failed"
    field: str | None = "credentials"
    received_auth_type: str | None = None
    validation_errors: str | None = None


@dataclass(kw_only=True)
class AzureCredentialTypeError(CredentialValidationError):
    """Azure credential has invalid parameter types.

    A :class:`~application_sdk.credentials.errors.CredentialValidationError`,
    so ``except CredentialError`` catches it; it keeps its own code.
    """

    code: ClassVar[str] = "INVALID_INPUT_AZURE_CREDENTIAL_TYPE"
    message: str = "Invalid credential parameter types"
    field: str | None = "credentials"


@dataclass(kw_only=True)
class AzureCredentialError(CredentialError):
    """Azure credential authentication failed.

    A :class:`~application_sdk.credentials.errors.CredentialError` (category
    ``AUTH``), so ``except CredentialError`` catches it.
    """

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


@deprecated(
    "application_sdk.clients.azure.azure_errors.AzureConnectionError is deprecated and will "
    "be removed in v4.0.0: nothing in the SDK raises it. To keep its wire code, "
    "raise an app-owned DependencyUnavailableError subclass that sets "
    'code = "DEPENDENCY_UNAVAILABLE_AZURE"; plain DependencyUnavailableError '
    "drops the Azure-specific code."
)
@dataclass(kw_only=True)
class AzureConnectionError(DependencyUnavailableError):
    """Azure service is unavailable."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AZURE"
    message: str = "Azure service is unavailable"
    service: str | None = "azure"


@deprecated(
    "application_sdk.clients.azure.azure_errors.AzureServiceError is deprecated and will "
    "be removed in v4.0.0: nothing in the SDK raises it. To keep its wire code, "
    "raise an app-owned DependencyUnavailableError subclass that sets "
    'code = "DEPENDENCY_UNAVAILABLE_AZURE_SERVICE"; plain DependencyUnavailableError '
    "drops the Azure-specific code."
)
@dataclass(kw_only=True)
class AzureServiceError(DependencyUnavailableError):
    """Azure service returned an error."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AZURE_SERVICE"
    message: str = "Azure service error"
    service: str | None = "azure"
