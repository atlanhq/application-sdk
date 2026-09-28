"""Server handler surface — Handler base + typed contracts.

Re-exports the handler base class and the typed contracts the SQL-connector
server path uses, so app code imports them from one place.
"""

from application_sdk_api.handler.base import DefaultHandler, Handler
from application_sdk_api.handler.contracts import (
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
    flatten_credentials_to_pairs,
)

__all__ = [
    "Handler",
    "DefaultHandler",
    "ApiMetadataObject",
    "ApiMetadataOutput",
    "AuthInput",
    "AuthOutput",
    "AuthStatus",
    "BaseConnectionConfig",
    "BaseMetadataConfig",
    "HandlerCredential",
    "MetadataInput",
    "MetadataOutput",
    "PreflightCheck",
    "PreflightInput",
    "PreflightOutput",
    "PreflightStatus",
    "SqlMetadataObject",
    "SqlMetadataOutput",
    "flatten_credentials_to_pairs",
]
