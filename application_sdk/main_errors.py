"""Typed error leaves for the main entry-point module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DependencyUnavailableError, InvalidInputError


@dataclass(kw_only=True)
class MissingAppModuleError(InvalidInputError):
    """App module was not specified via --app or ATLAN_APP_MODULE."""

    code: ClassVar[str] = "INVALID_INPUT_APP_MODULE_MISSING"
    message: str = "App module is required. Use --app or set ATLAN_APP_MODULE."
    field: str | None = "app"


@dataclass(kw_only=True)
class MultiAppModuleError(InvalidInputError):
    """ATLAN_APP_MODULE contains a comma — multi-app pattern is not supported in v3."""

    code: ClassVar[str] = "INVALID_INPUT_APP_MODULE_COMMA"
    message: str = (
        "ATLAN_APP_MODULE must not contain a comma. "
        "The multi-app pattern is not supported in v3. "
        "Define multiple @entrypoint methods on a single App subclass instead."
    )
    field: str | None = "app"
    app_module: str | None = None


@dataclass(kw_only=True)
class DaprNotDetectedError(DependencyUnavailableError):
    """Dapr sidecar is not running (DAPR_HTTP_PORT not set)."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_DAPR_SIDECAR"
    message: str = (
        "Dapr sidecar not detected (DAPR_HTTP_PORT not set). "
        "Run 'poe start-deps' to start local Dapr + Temporal, "
        "or set DAPR_HTTP_PORT if running daprd manually."
    )
    service: str | None = "dapr"


@dataclass(kw_only=True)
class UnknownModeError(InvalidInputError):
    """Unknown execution mode was specified."""

    code: ClassVar[str] = "INVALID_INPUT_MODE"
    message: str = "Unknown mode. Must be 'worker', 'handler', or 'combined'."
    field: str | None = "mode"
    received_mode: str | None = None
