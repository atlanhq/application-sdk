"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, metadata, and optional warmup endpoints, plus the service factory for
creating FastAPI applications.
"""

from typing import TYPE_CHECKING, Any

from application_sdk._install import lazy_getattr
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
    CheckTier,
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
    WarmupInput,
    WarmupObservation,
    WarmupState,
)

if TYPE_CHECKING:
    from application_sdk.handler.service import (
        create_app_handler_service,
        run_app_handler_service,
    )

#: Names served lazily from :mod:`application_sdk.handler.service` (PEP 562).
#: The HTTP server pulls in FastAPI and the Temporal client, so importing the
#: contracts or the ``Handler`` ABC must not load it (FND-3280). The api
#: distribution ships this ``__init__`` without ``service`` at all.
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
    "CheckTier",
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
    "WarmupInput",
    "WarmupObservation",
    "WarmupState",
    "create_app_handler_service",
    "run_app_handler_service",
]


def __getattr__(name: str) -> Any:
    # No submodule fallback: ``service`` (FastAPI + Temporal) is an attribute
    # only once something imports it, so ``hasattr(handler, "service")`` does
    # not load it.
    return lazy_getattr(__name__, name, _LAZY, globals())


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
