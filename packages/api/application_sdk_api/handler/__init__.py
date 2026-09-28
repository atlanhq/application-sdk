"""The handler surface: the ``Handler`` base, its typed contracts and request context.

This is the single definition shared by the worker (``application_sdk``) and the
consolidated API host. App handler code imports from here; everything else an
app needs comes from ``application_sdk``.
"""

from application_sdk_api.handler.base import DefaultHandler, Handler, HandlerError
from application_sdk_api.handler.context import HandlerContext
from application_sdk_api.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    BaseConnectionConfig,
    BaseMetadataConfig,
    CloudEventEnvelope,
    EventFilterRule,
    EventTriggerConfig,
    FileUploadResponse,
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
    SubscriptionConfig,
    flatten_credentials_to_pairs,
    normalize_credentials,
    unverifiable_preflight_result,
)

__all__ = [
    "ApiMetadataObject",
    "ApiMetadataOutput",
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "BaseConnectionConfig",
    "BaseMetadataConfig",
    "CloudEventEnvelope",
    "DefaultHandler",
    "EventFilterRule",
    "EventTriggerConfig",
    "FileUploadResponse",
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
    "SubscriptionConfig",
    "flatten_credentials_to_pairs",
    "normalize_credentials",
    "unverifiable_preflight_result",
]
