"""Typed error leaves for the Temporal backend module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    InvalidInputError,
    NotFoundError,
)


@dataclass(kw_only=True)
class UnknownEntryPointError(NotFoundError):
    code: ClassVar[str] = "NOT_FOUND_ENTRY_POINT"
    message: str = "Unknown entry point"
    resource_type: str | None = "entry_point"
    resource_identifier: str | None = None


@dataclass(kw_only=True)
class MtlsConfigError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_TLS_MTLS_CONFIG"
    message: str = "mTLS requires both client cert and client private key"
    field: str | None = "tls_config"


@dataclass(kw_only=True)
class TlsCertFileNotFoundError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_TLS_CERT_NOT_FOUND"
    message: str = "TLS certificate file not found"
    field: str | None = None


@dataclass(kw_only=True)
class TemporalConnectError(DependencyUnavailableError):
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_TEMPORAL"
    message: str = "Failed to connect to Temporal"
    service: str | None = "temporal"
    target: str | None = None
