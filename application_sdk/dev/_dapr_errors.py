"""Typed error leaves for the embedded Dapr dev runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import AppTimeoutError, InternalError


@dataclass(kw_only=True)
class UnsupportedArchitectureError(InternalError):
    code: ClassVar[str] = "INTERNAL_DAPR_UNSUPPORTED_ARCH"
    message: str = "Unsupported architecture for embedded Dapr"
    component: str | None = "embedded_dapr"
    architecture: str | None = None


@dataclass(kw_only=True)
class UnsupportedOsError(InternalError):
    code: ClassVar[str] = "INTERNAL_DAPR_UNSUPPORTED_OS"
    message: str = "Unsupported OS for embedded Dapr"
    component: str | None = "embedded_dapr"
    os_name: str | None = None


@dataclass(kw_only=True)
class DaprdBinaryMissingError(InternalError):
    code: ClassVar[str] = "INTERNAL_DAPR_BINARY_NOT_FOUND"
    message: str = "daprd binary not found in downloaded archive"
    component: str | None = "embedded_dapr"
    archive_url: str | None = None
    archive_format: str | None = None


@dataclass(kw_only=True)
class DaprReadinessTimeoutError(AppTimeoutError):
    code: ClassVar[str] = "TIMEOUT_DAPR_READINESS"
    message: str = "Embedded Dapr did not become ready within deadline"
    operation: str | None = "embedded_dapr_readiness"
    timeout_seconds: float | None = None
