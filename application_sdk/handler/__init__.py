"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, metadata, and optional warmup endpoints, plus the service factory for
creating FastAPI applications.
"""

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
    WarmupState,
    WarmupStatus,
)
from application_sdk.handler.service import (
    create_app_handler_service,
    run_app_handler_service,
)

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
    "WarmupState",
    "WarmupStatus",
    "create_app_handler_service",
    "run_app_handler_service",
]
