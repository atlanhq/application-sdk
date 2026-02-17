"""Core types for credential declaration system.

This module provides the foundational types for declaring credential requirements
in applications. Developers use these types to specify what credentials their
app needs, and the framework handles storage, injection, and refresh.

Example:
    >>> from application_sdk.credentials import Credential, AuthMode, FieldSpec
    >>>
    >>> # Simple declaration
    >>> Credential(name="stripe", auth=AuthMode.API_KEY)
    >>>
    >>> # With field customization
    >>> Credential(
    ...     name="database",
    ...     auth=AuthMode.DATABASE,
    ...     extra_fields=[
    ...         FieldSpec(name="warehouse", display_name="Data Warehouse"),
    ...     ]
    ... )
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
    from application_sdk.credentials.protocols.base import BaseProtocol


class ProtocolType(Enum):
    """The 6 fundamental authentication protocols.

    Every credential type in existence reduces to one of these 6 patterns:

    - STATIC_SECRET: Single secret value added to request (API keys, PATs, Bearer tokens)
    - IDENTITY_PAIR: Two values combined (Basic Auth, Email+Token)
    - TOKEN_EXCHANGE: Exchange credentials for temporary token (OAuth 2.0, OIDC, JWT)
    - REQUEST_SIGNING: Cryptographically sign each request (AWS SigV4, HMAC)
    - CERTIFICATE: Certificate-based mutual authentication (mTLS, Client certs)
    - CONNECTION: Multi-field params for SDK/driver initialization (Databases, Queues)
    """

    STATIC_SECRET = "static_secret"
    IDENTITY_PAIR = "identity_pair"
    TOKEN_EXCHANGE = "token_exchange"
    REQUEST_SIGNING = "request_signing"
    CERTIFICATE = "certificate"
    CONNECTION = "connection"


class AuthMode(Enum):
    """Developer-friendly authentication modes.

    Each AuthMode maps to an underlying ProtocolType with sensible defaults.
    Use these instead of protocols directly for common use cases.

    Maps to STATIC_SECRET:
        API_KEY, API_KEY_QUERY, API_KEY_HEADER, PAT, BEARER_TOKEN,
        SERVICE_TOKEN, ATLAN_API_KEY

    Maps to IDENTITY_PAIR:
        BASIC_AUTH, EMAIL_TOKEN, HEADER_PAIR, BODY_CREDENTIALS

    Maps to TOKEN_EXCHANGE:
        OAUTH2, OAUTH2_CLIENT_CREDENTIALS, JWT_BEARER, ATLAN_OAUTH

    Maps to REQUEST_SIGNING:
        AWS_SIGV4, HMAC

    Maps to CERTIFICATE:
        MTLS, CLIENT_CERT

    Maps to CONNECTION:
        DATABASE, SDK_CONNECTION

    Escape hatch:
        CUSTOM - Use with a custom protocol implementation
    """

    # Maps to STATIC_SECRET Protocol
    API_KEY = "api_key"  # Authorization: Bearer {key}
    API_KEY_QUERY = "api_key_query"  # ?api_key={key}
    API_KEY_HEADER = "api_key_header"  # X-API-Key: {key} (no prefix)
    PAT = "pat"  # Personal Access Token (Authorization: Bearer {key})
    BEARER_TOKEN = "bearer_token"  # Authorization: Bearer {key}
    SERVICE_TOKEN = "service_token"  # Authorization: {key} (no prefix, raw token)

    # Maps to IDENTITY_PAIR Protocol
    BASIC_AUTH = "basic_auth"
    EMAIL_TOKEN = "email_token"
    HEADER_PAIR = "header_pair"  # Two separate custom headers (e.g., Plaid)
    BODY_CREDENTIALS = "body_credentials"  # Credentials in JSON request body

    # Maps to TOKEN_EXCHANGE Protocol
    OAUTH2 = "oauth2"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    JWT_BEARER = "jwt_bearer"

    # Maps to REQUEST_SIGNING Protocol
    AWS_SIGV4 = "aws_sigv4"
    HMAC = "hmac"

    # Maps to CERTIFICATE Protocol
    MTLS = "mtls"
    CLIENT_CERT = "client_cert"

    # Maps to CONNECTION Protocol
    DATABASE = "database"
    SDK_CONNECTION = "sdk_connection"

    # Atlan-specific authentication modes
    # See: https://docs.atlan.com/get-started/references/api-access
    ATLAN_API_KEY = "atlan_api_key"  # Bearer token in Authorization header
    ATLAN_OAUTH = "atlan_oauth"  # OAuth2 client credentials flow

    # Escape hatch - requires custom protocol implementation
    CUSTOM = "custom"


class FieldType(Enum):
    """Supported field types for UI generation."""

    TEXT = "text"
    PASSWORD = "password"
    TEXTAREA = "textarea"
    SELECT = "select"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    URL = "url"
    EMAIL = "email"


@dataclass
class FieldSpec:
    """Full customization for any credential field.

    Supports UI generation, validation, and SDK compatibility.

    Attributes:
        name: Internal field name (used in code/SDK).
        display_name: What user sees in UI (auto-generated from name if not provided).
        placeholder: Placeholder text in input field.
        help_text: Help text shown below field.
        required: Is this field required?
        sensitive: Should it be masked (password field)?
        field_type: text, password, textarea, select, number, etc.
        options: For select fields, list of allowed values.
        default_value: Pre-filled value.
        validation_regex: Regex pattern for validation.
        min_length: Minimum length constraint.
        max_length: Maximum length constraint.

    Example:
        >>> FieldSpec(
        ...     name="api_key",
        ...     display_name="Stripe Secret Key",
        ...     placeholder="sk_live_...",
        ...     help_text="Find in Dashboard → API Keys",
        ...     sensitive=True,
        ...     validation_regex=r"^sk_(live|test)_[a-zA-Z0-9]+$",
        ... )
    """

    name: str
    display_name: Optional[str] = None
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    required: bool = True
    sensitive: bool = False
    field_type: Union[FieldType, str] = FieldType.TEXT
    options: Optional[List[str]] = None
    default_value: Optional[str] = None
    validation_regex: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None

    def __post_init__(self) -> None:
        """Auto-generate display_name from name if not provided."""
        if self.display_name is None:
            # Convert snake_case to Title Case
            self.display_name = self.name.replace("_", " ").title()

        # Convert string field_type to enum if needed
        if isinstance(self.field_type, str):
            try:
                self.field_type = FieldType(self.field_type)
            except ValueError:
                # Keep as string if not a valid enum value
                pass

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: Dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "required": self.required,
            "sensitive": self.sensitive,
            "field_type": (
                self.field_type.value
                if isinstance(self.field_type, FieldType)
                else self.field_type
            ),
        }
        if self.placeholder:
            result["placeholder"] = self.placeholder
        if self.help_text:
            result["help_text"] = self.help_text
        if self.options:
            result["options"] = self.options
        if self.default_value is not None:
            result["default_value"] = self.default_value
        if self.validation_regex:
            result["validation_regex"] = self.validation_regex
        if self.min_length is not None:
            result["min_length"] = self.min_length
        if self.max_length is not None:
            result["max_length"] = self.max_length
        return result


@dataclass
class Credential:
    """Credential declaration for an application.

    Declares what credentials an app needs (slots/placeholders).
    This is the primary interface for developers to specify credential requirements.

    Attributes:
        name: Slot name (used in code to reference this credential).
        auth: AuthMode enum for developer-friendly auth selection.
        protocol: Direct ProtocolType or custom protocol instance (escape hatch).
        protocol_config: Custom configuration for protocol.
        fields: Customization for standard fields (dict keyed by field name).
        extra_fields: Additional fields beyond standard protocol fields.
        config_override: Override default protocol configuration.
        required: Whether this credential is required for the app.
        description: Optional description of the credential's purpose.
        base_url: Base URL for HTTP requests (e.g., "https://api.stripe.com").
            When set, ctx.http.get(slot, "/path") will use this base URL.
        base_url_field: Name of the field containing the base URL (for dynamic URLs).
            If set, base URL is read from credentials at runtime.
        timeout: Default HTTP timeout in seconds (default: 30.0).
        max_retries: Maximum retry attempts on auth failure (default: 1).

    Example:
        >>> # Simple: Just auth mode
        >>> Credential(name="stripe", auth=AuthMode.API_KEY)
        >>>
        >>> # With base URL (recommended for cleaner API calls)
        >>> Credential(
        ...     name="stripe",
        ...     auth=AuthMode.API_KEY,
        ...     base_url="https://api.stripe.com",
        ... )
        >>> # Then in activity: ctx.http.get("stripe", "/v1/charges")
        >>>
        >>> # With dynamic base URL from credentials
        >>> Credential(
        ...     name="jira",
        ...     auth=AuthMode.EMAIL_TOKEN,
        ...     base_url_field="site_url",  # Read from credentials["site_url"]
        ...     extra_fields=[
        ...         FieldSpec(name="site_url", display_name="Jira Site URL",
        ...                   placeholder="https://yoursite.atlassian.net")
        ...     ]
        ... )
        >>>
        >>> # With field customization
        >>> Credential(
        ...     name="jira",
        ...     auth=AuthMode.EMAIL_TOKEN,
        ...     fields={
        ...         "email": FieldSpec(display_name="Atlassian Email"),
        ...         "api_token": FieldSpec(sensitive=True)
        ...     }
        ... )
        >>>
        >>> # With extra fields
        >>> Credential(
        ...     name="snowflake",
        ...     auth=AuthMode.DATABASE,
        ...     extra_fields=[
        ...         FieldSpec(name="warehouse", display_name="Warehouse"),
        ...         FieldSpec(name="role", default_value="PUBLIC")
        ...     ]
        ... )
        >>>
        >>> # With config override
        >>> Credential(
        ...     name="weather_api",
        ...     auth=AuthMode.API_KEY,
        ...     config_override={"location": "query", "param_name": "appid"}
        ... )
        >>>
        >>> # Escape hatch: Direct protocol
        >>> Credential(
        ...     name="custom_signing",
        ...     protocol=ProtocolType.REQUEST_SIGNING,
        ...     protocol_config={"algorithm": "hmac_sha512"},
        ...     fields=[FieldSpec(name="secret_key", sensitive=True)]
        ... )
    """

    name: str
    auth: Optional[AuthMode] = None
    protocol: Optional[Union[ProtocolType, "BaseProtocol"]] = None
    protocol_config: Optional[Dict[str, Any]] = None
    fields: Optional[Dict[str, FieldSpec]] = field(default_factory=dict)
    extra_fields: Optional[List[FieldSpec]] = field(default_factory=list)
    config_override: Optional[Dict[str, Any]] = None
    required: bool = True
    description: Optional[str] = None
    # HTTP client configuration
    base_url: Optional[str] = None
    base_url_field: Optional[str] = (
        None  # Field name in credentials for dynamic base URL
    )
    timeout: float = 30.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        """Validate credential configuration."""
        if self.auth is None and self.protocol is None:
            raise ValueError("Either 'auth' or 'protocol' must be specified")

        if self.auth is not None and self.protocol is not None:
            raise ValueError(
                "Cannot specify both 'auth' and 'protocol'. "
                "Use 'auth' for standard modes or 'protocol' for escape hatch."
            )

        # Initialize empty containers if None
        if self.fields is None:
            self.fields = {}
        if self.extra_fields is None:
            self.extra_fields = []

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization (e.g., for configmap)."""
        result: Dict[str, Any] = {
            "name": self.name,
            "required": self.required,
        }

        if self.auth:
            result["auth_mode"] = self.auth.value

        if self.protocol:
            if isinstance(self.protocol, ProtocolType):
                result["protocol_type"] = self.protocol.value
            else:
                result["protocol_type"] = "custom"

        if self.description:
            result["description"] = self.description

        if self.protocol_config:
            result["protocol_config"] = self.protocol_config

        if self.config_override:
            result["config_override"] = self.config_override

        # HTTP client configuration
        if self.base_url:
            result["base_url"] = self.base_url

        if self.base_url_field:
            result["base_url_field"] = self.base_url_field

        if self.timeout != 30.0:
            result["timeout"] = self.timeout

        if self.max_retries != 1:
            result["max_retries"] = self.max_retries

        # Fields will be computed by the resolver based on protocol + customizations
        # This is handled in get_credential_schema()

        return result
