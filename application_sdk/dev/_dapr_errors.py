"""Typed error leaves for the embedded Dapr dev runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AppTimeoutError,
    InternalError,
    InvalidInputError,
)


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


# Caller-misuse errors (bad embedded_dapr arguments) are InvalidInputError, not
# InternalError: they carry field/constraint and are addressed to the caller.
# (The Unsupported*/BinaryMissing errors above are genuine runtime/environment
# failures, hence InternalError — a different category.)
@dataclass(kw_only=True)
class DaprComponentsConfigError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_DAPR_COMPONENTS"
    message: str = (
        "embedded_dapr: pass either secrets_file or components_dir, not both — "
        "a caller-supplied components_dir already defines its own secret store."
    )
    field: str | None = "components_dir"
    constraint: str | None = "mutually exclusive with secrets_file"


@dataclass(kw_only=True)
class DaprComponentsDirNotFoundError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_DAPR_COMPONENTS_DIR"
    message: str = "embedded_dapr: components_dir does not exist or is not a directory"
    field: str | None = "components_dir"
    constraint: str | None = "must be an existing directory"


@dataclass(kw_only=True)
class DaprReadinessTimeoutError(AppTimeoutError):
    code: ClassVar[str] = "TIMEOUT_DAPR_READINESS"
    message: str = "Embedded Dapr did not become ready within deadline"
    operation: str | None = "embedded_dapr_readiness"
    timeout_seconds: float | None = None
