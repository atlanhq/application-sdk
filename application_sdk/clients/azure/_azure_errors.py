"""Typed Azure error subclasses — stable wire codes for the Azure client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors import (
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
    PreconditionError,
)


@dataclass(kw_only=True)
class AzureAuthError(AuthError):
    """Azure authentication failed (token or service principal)."""

    code: ClassVar[str] = "AUTH_AZURE"


@dataclass(kw_only=True)
class AzureConnectionError(DependencyUnavailableError):
    """Azure service is unreachable or returned an error."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AZURE"


@dataclass(kw_only=True)
class AzureCredentialParseError(InvalidInputError):
    """Azure credential parameters are missing or malformed."""

    code: ClassVar[str] = "INVALID_INPUT_AZURE_CREDENTIALS"


@dataclass(kw_only=True)
class AzureNoCredentialError(PreconditionError):
    """No Azure credential available — call load() first."""

    code: ClassVar[str] = "PRECONDITION_AZURE_NO_CREDENTIAL"
