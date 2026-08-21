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
class EntryPointRequiredError(InvalidInputError):
    """A multi-entry-point app was addressed without naming an entry point.

    Such an app registers ``{app}:{entry-point}`` per entry point and never the
    bare ``{app}``, so a submission without ``entry_point`` targets a workflow
    type no worker has registered. Temporal accepts that start request: the run
    opens, nothing claims it, and the caller awaits a listener that never
    arrives — until the execution timeout, or forever if there is none.

    Raised before submitting, so the mistake surfaces at the call site instead of
    as a hang. Deliberately NOT resolved to the app's default entry point: this
    is a programmatic API, and silently picking one is how a test comes to
    believe it exercised the miner while actually running the crawler.
    """

    code: ClassVar[str] = "INVALID_INPUT_ENTRY_POINT_REQUIRED"
    message: str = "entry_point is required for a multi-entry-point app"
    field: str | None = "entry_point"


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
