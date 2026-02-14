"""Credential declaration and injection system.

This module provides a comprehensive credential management system for
applications built on the Atlan platform. It supports:

- **Declaration**: Code-defined credential requirements using AuthModes
- **Storage**: Centralized credential library with ACL
- **Binding**: App profiles mapping slots to credentials
- **Injection**: Runtime credential bootstrap and auto-injection

Quick Start:
    >>> from application_sdk.credentials import Credential, AuthMode
    >>>
    >>> # Declare credentials in your handler
    >>> class MyHandler(HandlerInterface):
    ...     @classmethod
    ...     def declare_credentials(cls):
    ...         return [
    ...             Credential(name="api", auth=AuthMode.API_KEY),
    ...             Credential(name="database", auth=AuthMode.DATABASE),
    ...         ]

    >>> # Use credentials in activities
    >>> from application_sdk.credentials import get_credential_context
    >>>
    >>> async def my_activity():
    ...     ctx = get_credential_context()
    ...
    ...     # Option 1: Make authenticated HTTP requests
    ...     response = await ctx.http.get("api", "/v1/data")
    ...
    ...     # Option 2: Materialize for SDK usage
    ...     creds = await ctx.credentials.materialize("database")
    ...     conn = connect(
    ...         host=creds.get("host"),
    ...         password=creds.get("password"),  # Secure, audit-logged
    ...     )

Security Features:
    - Credentials are never serialized to Temporal history
    - CredentialHandle blocks dangerous operations (dict, print, json.dumps)
    - All credential access is audit-logged
    - Automatic cleanup on workflow completion

Supported AuthModes:
    - STATIC_SECRET: API_KEY, API_KEY_QUERY, PAT, BEARER_TOKEN
    - IDENTITY_PAIR: BASIC_AUTH, EMAIL_TOKEN
    - TOKEN_EXCHANGE: OAUTH2, OAUTH2_CLIENT_CREDENTIALS, JWT_BEARER
    - REQUEST_SIGNING: AWS_SIGV4, HMAC
    - CERTIFICATE: MTLS, CLIENT_CERT
    - CONNECTION: DATABASE, SDK_CONNECTION
    - CUSTOM: For custom protocol implementations
"""

# Context access
from application_sdk.credentials.context import (
    CredentialAccessor,
    CredentialContext,
    CredentialHTTPAccessor,
    get_credential_context,
)

# Exceptions
from application_sdk.credentials.exceptions import (
    CredentialBootstrapError,
    CredentialError,
    CredentialHandleError,
    CredentialNotFoundError,
    CredentialRefreshError,
    CredentialResolutionError,
    CredentialValidationError,
    ProtocolError,
)

# Handle (secure credential wrapper)
from application_sdk.credentials.handle import CredentialHandle

# Protocol base class (for custom protocols)
from application_sdk.credentials.protocols.base import BaseProtocol

# Custom protocol base (for escape hatch scenarios)
from application_sdk.credentials.protocols.custom import CustomProtocol

# Resolver
from application_sdk.credentials.resolver import CredentialResolver

# Result types
from application_sdk.credentials.results import (
    ApplyResult,
    MaterializeResult,
    ValidationResult,
)

# Core types for credential declaration
from application_sdk.credentials.types import (
    AuthMode,
    Credential,
    FieldSpec,
    FieldType,
    ProtocolType,
)

__all__ = [
    # Core types
    "AuthMode",
    "Credential",
    "FieldSpec",
    "FieldType",
    "ProtocolType",
    # Result types
    "ApplyResult",
    "MaterializeResult",
    "ValidationResult",
    # Exceptions
    "CredentialBootstrapError",
    "CredentialError",
    "CredentialHandleError",
    "CredentialNotFoundError",
    "CredentialRefreshError",
    "CredentialResolutionError",
    "CredentialValidationError",
    "ProtocolError",
    # Context
    "CredentialAccessor",
    "CredentialContext",
    "CredentialHTTPAccessor",
    "get_credential_context",
    # Handle
    "CredentialHandle",
    # Resolver
    "CredentialResolver",
    # Protocol base classes
    "BaseProtocol",
    "CustomProtocol",
]
