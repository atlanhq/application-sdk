"""AuthMode to Protocol resolver with sensible defaults.

This module maps developer-friendly AuthMode values to their
underlying protocol implementations with appropriate defaults.
"""

from typing import Any, Dict, Optional, Type

from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.protocols.certificate import CertificateProtocol
from application_sdk.credentials.protocols.connection import ConnectionProtocol
from application_sdk.credentials.protocols.identity_pair import IdentityPairProtocol
from application_sdk.credentials.protocols.request_signing import RequestSigningProtocol
from application_sdk.credentials.protocols.static_secret import StaticSecretProtocol
from application_sdk.credentials.protocols.token_exchange import TokenExchangeProtocol
from application_sdk.credentials.types import AuthMode, Credential, ProtocolType

# AuthMode to ProtocolType mapping
AUTH_MODE_TO_PROTOCOL: Dict[AuthMode, ProtocolType] = {
    # STATIC_SECRET
    AuthMode.API_KEY: ProtocolType.STATIC_SECRET,
    AuthMode.API_KEY_QUERY: ProtocolType.STATIC_SECRET,
    AuthMode.API_KEY_HEADER: ProtocolType.STATIC_SECRET,
    AuthMode.PAT: ProtocolType.STATIC_SECRET,
    AuthMode.BEARER_TOKEN: ProtocolType.STATIC_SECRET,
    AuthMode.SERVICE_TOKEN: ProtocolType.STATIC_SECRET,
    # IDENTITY_PAIR
    AuthMode.BASIC_AUTH: ProtocolType.IDENTITY_PAIR,
    AuthMode.EMAIL_TOKEN: ProtocolType.IDENTITY_PAIR,
    AuthMode.HEADER_PAIR: ProtocolType.IDENTITY_PAIR,
    AuthMode.BODY_CREDENTIALS: ProtocolType.IDENTITY_PAIR,
    # TOKEN_EXCHANGE
    AuthMode.OAUTH2: ProtocolType.TOKEN_EXCHANGE,
    AuthMode.OAUTH2_CLIENT_CREDENTIALS: ProtocolType.TOKEN_EXCHANGE,
    AuthMode.JWT_BEARER: ProtocolType.TOKEN_EXCHANGE,
    # REQUEST_SIGNING
    AuthMode.AWS_SIGV4: ProtocolType.REQUEST_SIGNING,
    AuthMode.HMAC: ProtocolType.REQUEST_SIGNING,
    # CERTIFICATE
    AuthMode.MTLS: ProtocolType.CERTIFICATE,
    AuthMode.CLIENT_CERT: ProtocolType.CERTIFICATE,
    # CONNECTION
    AuthMode.DATABASE: ProtocolType.CONNECTION,
    AuthMode.SDK_CONNECTION: ProtocolType.CONNECTION,
    # Atlan-specific
    AuthMode.ATLAN_API_KEY: ProtocolType.STATIC_SECRET,
    AuthMode.ATLAN_OAUTH: ProtocolType.TOKEN_EXCHANGE,
}

# Validation: Ensure all AuthModes (except CUSTOM) are mapped
_missing_auth_modes = (
    set(AuthMode) - set(AUTH_MODE_TO_PROTOCOL.keys()) - {AuthMode.CUSTOM}
)
assert not _missing_auth_modes, (
    f"Missing AuthMode mappings in AUTH_MODE_TO_PROTOCOL: {_missing_auth_modes}. "
    f"All AuthModes (except CUSTOM) must have a protocol mapping."
)

# ProtocolType to Protocol class mapping
PROTOCOL_TYPE_TO_CLASS: Dict[ProtocolType, Type[BaseProtocol]] = {
    ProtocolType.STATIC_SECRET: StaticSecretProtocol,
    ProtocolType.IDENTITY_PAIR: IdentityPairProtocol,
    ProtocolType.TOKEN_EXCHANGE: TokenExchangeProtocol,
    ProtocolType.REQUEST_SIGNING: RequestSigningProtocol,
    ProtocolType.CERTIFICATE: CertificateProtocol,
    ProtocolType.CONNECTION: ConnectionProtocol,
}

# AuthMode-specific configuration defaults
# These override the protocol's default_config for specific auth modes
AUTH_MODE_DEFAULTS: Dict[AuthMode, Dict[str, Any]] = {
    # STATIC_SECRET variants
    AuthMode.API_KEY: {
        "location": "header",
        "header_name": "Authorization",
        "prefix": "Bearer ",
    },
    AuthMode.API_KEY_QUERY: {
        "location": "query",
        "param_name": "api_key",
    },
    AuthMode.API_KEY_HEADER: {
        # X-API-Key header pattern (no prefix)
        # Common for many APIs: Datadog, Twilio, etc.
        "location": "header",
        "header_name": "X-API-Key",
        "prefix": "",
    },
    AuthMode.PAT: {
        "location": "header",
        "header_name": "Authorization",
        "prefix": "Bearer ",
    },
    AuthMode.BEARER_TOKEN: {
        "location": "header",
        "header_name": "Authorization",
        "prefix": "Bearer ",
    },
    AuthMode.SERVICE_TOKEN: {
        # Raw token in Authorization header without "Bearer " prefix
        # Used for some internal services or custom auth
        "location": "header",
        "header_name": "Authorization",
        "prefix": "",
    },
    # IDENTITY_PAIR variants
    AuthMode.BASIC_AUTH: {
        "encoding": "basic",
        "identity_field": "username",
        "secret_field": "password",
    },
    AuthMode.EMAIL_TOKEN: {
        "encoding": "basic",
        "identity_field": "email",
        "secret_field": "api_token",
    },
    AuthMode.HEADER_PAIR: {
        # Two separate custom headers (e.g., PLAID-CLIENT-ID, PLAID-SECRET)
        "location": "header_pair",
        "identity_field": "client_id",
        "secret_field": "secret",
        "identity_header": "X-Client-ID",
        "secret_header": "X-Client-Secret",
    },
    AuthMode.BODY_CREDENTIALS: {
        # Credentials in JSON request body
        "location": "body",
        "identity_field": "client_id",
        "secret_field": "secret",
    },
    # TOKEN_EXCHANGE variants
    AuthMode.OAUTH2: {
        "grant_type": "authorization_code",
    },
    AuthMode.OAUTH2_CLIENT_CREDENTIALS: {
        "grant_type": "client_credentials",
    },
    AuthMode.JWT_BEARER: {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
    },
    # REQUEST_SIGNING variants
    AuthMode.AWS_SIGV4: {
        "algorithm": "aws_sigv4",
    },
    AuthMode.HMAC: {
        "algorithm": "hmac_sha256",
    },
    # CERTIFICATE variants
    AuthMode.MTLS: {
        "verify_ssl": True,
    },
    AuthMode.CLIENT_CERT: {
        "verify_ssl": True,
    },
    # CONNECTION variants
    AuthMode.DATABASE: {},
    AuthMode.SDK_CONNECTION: {},
    # Atlan-specific variants
    # See: https://docs.atlan.com/get-started/references/api-access
    AuthMode.ATLAN_API_KEY: {
        "location": "header",
        "header_name": "Authorization",
        "prefix": "Bearer ",
        # Atlan API keys are passed as Bearer tokens
        "field_name": "api_key",
    },
    AuthMode.ATLAN_OAUTH: {
        # OAuth2 client credentials flow for Atlan
        # See: https://docs.atlan.com/get-started/references/api-access/oauth-clients
        "grant_type": "client_credentials",
        "token_endpoint_auth_method": "client_secret_post",
    },
}


class CredentialResolver:
    """Resolves Credential declarations to protocol instances.

    This class handles the mapping from Credential declarations to
    concrete protocol implementations, applying appropriate defaults
    and configuration overrides.

    Example:
        >>> from application_sdk.credentials import Credential, AuthMode
        >>>
        >>> cred = Credential(name="stripe", auth=AuthMode.API_KEY)
        >>> protocol = CredentialResolver.resolve(cred)
        >>> # protocol is now a StaticSecretProtocol instance
    """

    @classmethod
    def resolve(cls, credential: Credential) -> BaseProtocol:
        """Resolve a Credential declaration to a protocol instance.

        Args:
            credential: The Credential declaration.

        Returns:
            Configured BaseProtocol instance.

        Raises:
            ValueError: If the credential cannot be resolved to a protocol.
        """
        # Handle custom protocol instance (escape hatch)
        if isinstance(credential.protocol, BaseProtocol):
            return credential.protocol

        # Handle direct ProtocolType specification
        if isinstance(credential.protocol, ProtocolType):
            protocol_class = PROTOCOL_TYPE_TO_CLASS[credential.protocol]
            config = credential.protocol_config or {}
            return protocol_class(config=config)

        # Handle AuthMode resolution
        if credential.auth is not None:
            if credential.auth == AuthMode.CUSTOM:
                raise ValueError(
                    "AuthMode.CUSTOM requires a protocol instance. "
                    "Use: Credential(name='x', protocol=MyCustomProtocol())"
                )

            protocol_type = AUTH_MODE_TO_PROTOCOL[credential.auth]
            protocol_class = PROTOCOL_TYPE_TO_CLASS[protocol_type]

            # Build config from defaults + overrides
            config = {
                **AUTH_MODE_DEFAULTS.get(credential.auth, {}),
                **(credential.config_override or {}),
            }

            return protocol_class(config=config)

        raise ValueError(
            "Could not resolve credential to protocol. "
            "Specify either 'auth' (AuthMode) or 'protocol' (ProtocolType or instance)."
        )

    @classmethod
    def get_protocol_type(cls, credential: Credential) -> Optional[ProtocolType]:
        """Get the ProtocolType for a credential.

        Args:
            credential: The Credential declaration.

        Returns:
            ProtocolType or None for custom protocol instances.
        """
        if isinstance(credential.protocol, ProtocolType):
            return credential.protocol

        if isinstance(credential.protocol, BaseProtocol):
            return None  # Custom protocol instance

        if credential.auth is not None and credential.auth != AuthMode.CUSTOM:
            return AUTH_MODE_TO_PROTOCOL[credential.auth]

        return None

    @classmethod
    def get_credential_schema(cls, credential: Credential) -> Dict[str, Any]:
        """Get the full schema for a credential declaration.

        Used for generating configmap/UI schema. Includes all fields
        with their specifications.

        Args:
            credential: The Credential declaration.

        Returns:
            Dictionary with credential schema suitable for JSON serialization.
        """
        protocol = cls.resolve(credential)
        protocol_type = cls.get_protocol_type(credential)

        # Get all fields with customizations applied
        fields_schema = protocol.get_fields_schema(
            field_overrides=credential.fields,
            extra_fields=credential.extra_fields,
        )

        result: Dict[str, Any] = {
            "name": credential.name,
            "required": credential.required,
            "fields": fields_schema,
        }

        if credential.auth:
            result["auth_mode"] = credential.auth.value

        if protocol_type:
            result["protocol_type"] = protocol_type.value
        else:
            result["protocol_type"] = "custom"

        if credential.description:
            result["description"] = credential.description

        return result

    @classmethod
    def get_configmap(cls, credential: Credential) -> Dict[str, Any]:
        """Generate app-playground compatible configmap from credential declaration.

        This generates the JSON schema format expected by the app-playground
        UI for rendering credential input forms.

        Args:
            credential: The Credential declaration.

        Returns:
            Dictionary in app-playground configmap format with properties and steps.
        """
        schema = cls.get_credential_schema(credential)
        fields_list = schema.get("fields", [])

        # Build properties from fields (fields_list is a list of field dicts)
        properties: Dict[str, Any] = {}
        property_names = []

        for field_spec in fields_list:
            field_name = field_spec.get("name", "unknown")

            # Determine widget type
            widget = "text"
            if field_spec.get("sensitive", False):
                widget = "password"
            elif field_spec.get("field_type") == "url":
                widget = "text"
            elif field_spec.get("field_type") == "email":
                widget = "email"

            prop: Dict[str, Any] = {
                "type": "string",
                "ui": {
                    "widget": widget,
                    "label": field_spec.get("display_name", field_name),
                    "placeholder": field_spec.get("placeholder", ""),
                    "hidden": False,
                },
            }

            if field_spec.get("help_text"):
                prop["ui"]["help"] = field_spec["help_text"]

            # Add default value if specified
            if field_spec.get("default_value"):
                prop["default"] = field_spec["default_value"]

            properties[field_name] = prop
            property_names.append(field_name)

        return {
            "id": credential.name,
            "name": credential.name,
            "description": credential.description or f"{credential.name} credentials",
            "config": {
                "properties": properties,
                "steps": [
                    {
                        "id": "credentials",
                        "title": "Credentials",
                        "description": f"Configure {credential.name} credentials",
                        "properties": property_names,
                    }
                ],
            },
        }
