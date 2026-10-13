"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, metadata, and optional warmup endpoints, plus the service factory for
creating FastAPI applications.
"""

from typing import TYPE_CHECKING

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
#: contracts or the ``Handler`` ABC must not load it (FND-3280).
_SERVICE_NAMES = frozenset({"create_app_handler_service", "run_app_handler_service"})


def __getattr__(name: str) -> object:
    """Load the service names on first access."""
    if name in _SERVICE_NAMES:
        from application_sdk.handler import (  # noqa: PLC0415 — lazy by design: see _SERVICE_NAMES
            service,
        )

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
