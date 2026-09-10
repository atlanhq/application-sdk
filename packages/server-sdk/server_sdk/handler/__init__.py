"""Server handler surface — Handler base + typed contracts.

Re-exports the handler base class and the typed contracts the SQL-connector
server path uses, so app code imports them from one place.
"""

from server_sdk.handler.base import DefaultHandler, Handler
from server_sdk.handler.contracts import (
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
