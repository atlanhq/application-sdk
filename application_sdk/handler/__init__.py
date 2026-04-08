"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, and metadata endpoints, plus the service factory for
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
    Credential,
    MetadataField,
    MetadataInput,
    MetadataObject,
    MetadataOutput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from application_sdk.handler.service import (
    create_app_handler_service,
    run_app_handler_service,
)

__all__ = [
    "Handler",
    "DefaultHandler",
    "HandlerError",
    "HandlerContext",
    "ApiMetadataObject",
    "ApiMetadataOutput",
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "Credential",
    "MetadataField",
    "MetadataInput",
    "MetadataObject",
    "MetadataOutput",
    "PreflightCheck",
    "PreflightInput",
    "PreflightOutput",
    "PreflightStatus",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "create_app_handler_service",
    "run_app_handler_service",
]
