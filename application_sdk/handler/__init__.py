"""Handler framework for per-app HTTP services.

Provides the Handler ABC and DefaultHandler for implementing auth,
preflight, and metadata endpoints, plus the service factory for
creating FastAPI applications.
"""

from application_sdk.handler.base import DefaultHandler, Handler, HandlerError
from application_sdk.handler.checks import (
    check_atlan_publish_permission,
    check_user_enabled,
)
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
    "PreflightInput",
    "PreflightOutput",
    "PreflightStatus",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "check_atlan_publish_permission",
    "check_user_enabled",
    "create_app_handler_service",
    "run_app_handler_service",
]
