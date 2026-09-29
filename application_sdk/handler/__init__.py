"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, and metadata endpoints, plus the service factory for
creating FastAPI applications.
"""

import importlib
from typing import TYPE_CHECKING, Any

from application_sdk._logging import get_logger
from application_sdk.handler.base import DefaultHandler, Handler, HandlerError
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    BaseConnectionConfig,
    BaseMetadataConfig,
    HandlerCredential,
    MetadataInput,
    MetadataOutput,
    PreflightCheck,
    PreflightGateMode,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)

if TYPE_CHECKING:
    from application_sdk.handler.service import (
        create_app_handler_service,
        run_app_handler_service,
    )

#: Worker-side names: imported on first access, so the api distribution
#: (which ships this ``__init__`` without them) imports cleanly.
_LAZY: dict[str, tuple[str, str]] = {
    "create_app_handler_service": (
        "application_sdk.handler.service",
        "create_app_handler_service",
    ),
    "run_app_handler_service": (
        "application_sdk.handler.service",
        "run_app_handler_service",
    ),
}

__all__ = [
    "ApiMetadataObject",
    "ApiMetadataOutput",
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "BaseConnectionConfig",
    "BaseMetadataConfig",
    "DefaultHandler",
    "Handler",
    "HandlerContext",
    "HandlerCredential",
    "HandlerError",
    "MetadataInput",
    "MetadataOutput",
    "PreflightCheck",
    "PreflightGateMode",
    "PreflightInput",
    "PreflightOutput",
    "PreflightStatus",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "create_app_handler_service",
    "get_logger",
    "run_app_handler_service",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
