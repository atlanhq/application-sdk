"""IDENTITY_PAIR protocol - two values (identity + secret) combined.

Covers Basic Auth, Email+Token combinations (~120+ services).
Pattern: Two values combined (typically Base64 encoded).
"""

import base64
from typing import Any, Dict, List, Optional

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult, MaterializeResult
from application_sdk.credentials.types import FieldSpec, FieldType


class IdentityPairProtocol(BaseProtocol):
    """Protocol for Basic Auth, Email+Token, Header Pairs, and Body credentials.

    Pattern: Two values combined in various formats depending on location.

    Configuration options (via config or config_override):
        - location: "header" (default), "header_pair", or "body"
        - header_name: Header name for combined auth (default: "Authorization")
        - encoding: "basic" (Base64) or "raw" (default: "basic")
        - identity_field: Field name for identity (default: "username")
        - secret_field: Field name for secret (default: "password")
        - identity_header: Header name for identity when location="header_pair"
        - secret_header: Header name for secret when location="header_pair"

    The protocol dynamically generates fields based on identity_field and secret_field
    config, so EMAIL_TOKEN auth mode automatically uses "email" and "api_token" fields.

    Supported locations:
        - "header": Combined in single Authorization header (Basic Auth, Email+Token)
        - "header_pair": Two separate custom headers (e.g., Plaid's PLAID-CLIENT-ID)
        - "body": Credentials merged into JSON request body

    Examples:
        >>> # Basic Auth with username/password
        >>> protocol = IdentityPairProtocol()
        >>> result = protocol.apply({"username": "user", "password": "pass"}, {})
        >>> result.headers
        {'Authorization': 'Basic dXNlcjpwYXNz'}

        >>> # Two separate headers (Plaid-style)
        >>> protocol = IdentityPairProtocol(config={
        ...     "location": "header_pair",
        ...     "identity_field": "client_id",
        ...     "secret_field": "secret",
        ...     "identity_header": "PLAID-CLIENT-ID",
        ...     "secret_header": "PLAID-SECRET"
        ... })
        >>> result = protocol.apply({"client_id": "xxx", "secret": "yyy"}, {})
        >>> result.headers
        {'PLAID-CLIENT-ID': 'xxx', 'PLAID-SECRET': 'yyy'}

        >>> # Body credentials
        >>> protocol = IdentityPairProtocol(config={
        ...     "location": "body",
        ...     "identity_field": "client_id",
        ...     "secret_field": "secret"
        ... })
        >>> result = protocol.apply({"client_id": "xxx", "secret": "yyy"}, {})
        >>> result.body
        {'client_id': 'xxx', 'secret': 'yyy'}
    """

    # Note: default_fields is not used directly - get_all_fields() generates
    # fields dynamically based on identity_field/secret_field config
    default_fields: List[FieldSpec] = []

    default_config: Dict[str, Any] = {
        "location": "header",  # header, header_pair, or body
        "header_name": "Authorization",
        "encoding": "basic",  # basic, raw (for header location)
        "identity_field": "username",
        "secret_field": "password",
        # For header_pair location:
        "identity_header": "X-Client-ID",
        "secret_header": "X-Client-Secret",
    }

    def get_all_fields(
        self,
        field_overrides: Optional[Dict[str, FieldSpec]] = None,
        extra_fields: Optional[List[FieldSpec]] = None,
    ) -> List[FieldSpec]:
        """Get all fields dynamically based on identity_field/secret_field config.

        The field names are derived from the protocol config, so EMAIL_TOKEN
        auth mode automatically uses "email" and "api_token" fields.
        """
        field_overrides = field_overrides or {}
        extra_fields = extra_fields or []

        identity_field = self.config.get("identity_field", "username")
        secret_field = self.config.get("secret_field", "password")

        # Generate dynamic fields based on config
        dynamic_fields = [
            FieldSpec(
                name=identity_field,
                display_name=identity_field.replace("_", " ").title(),
                required=True,
            ),
            FieldSpec(
                name=secret_field,
                display_name=secret_field.replace("_", " ").title(),
                sensitive=True,
                field_type=FieldType.PASSWORD,
                required=True,
            ),
        ]

        # Apply any overrides
        result: List[FieldSpec] = []
        for default_field in dynamic_fields:
            if default_field.name in field_overrides:
                override = field_overrides[default_field.name]
                merged = FieldSpec(
                    name=override.name,
                    display_name=override.display_name or default_field.display_name,
                    placeholder=override.placeholder or default_field.placeholder,
                    help_text=override.help_text or default_field.help_text,
                    required=(
                        override.required
                        if override.required is not None
                        else default_field.required
                    ),
                    sensitive=(
                        override.sensitive
                        if override.sensitive is not None
                        else default_field.sensitive
                    ),
                    field_type=override.field_type or default_field.field_type,
                    options=override.options or default_field.options,
                    default_value=(
                        override.default_value
                        if override.default_value is not None
                        else default_field.default_value
                    ),
                    validation_regex=(
                        override.validation_regex or default_field.validation_regex
                    ),
                    min_length=(
                        override.min_length
                        if override.min_length is not None
                        else default_field.min_length
                    ),
                    max_length=(
                        override.max_length
                        if override.max_length is not None
                        else default_field.max_length
                    ),
                )
                result.append(merged)
            else:
                result.append(default_field)

        # Add extra fields
        result.extend(extra_fields)

        return result

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Add credentials to request based on location config.

        Supports three location types:
        - "header": Combined Basic Auth or raw in single header
        - "header_pair": Two separate custom headers
        - "body": Credentials merged into JSON request body
        """
        identity_field = self.config.get("identity_field", "username")
        secret_field = self.config.get("secret_field", "password")

        # Get identity (try configured field, then common aliases)
        identity = get_field_value(credentials, identity_field)
        if identity is None:
            identity = (
                credentials.get("username")
                or credentials.get("email")
                or credentials.get("user")
                or credentials.get("client_id")
            )

        # Get secret (try configured field, then common aliases)
        # Note: Empty string is valid (e.g., Stripe uses API key as username with empty password)
        secret = get_field_value(credentials, secret_field)
        if secret is None:
            secret = (
                credentials.get("password")
                or credentials.get("api_token")
                or credentials.get("token")
                or credentials.get("secret")
                or credentials.get("client_secret")
            )

        # Identity is required, but secret can be empty string (e.g., Stripe)
        if identity is None or secret is None:
            return ApplyResult()

        location = self.config.get("location", "header")

        if location == "header_pair":
            # Two separate custom headers
            identity_header = self.config.get("identity_header", "X-Client-ID")
            secret_header = self.config.get("secret_header", "X-Client-Secret")
            return ApplyResult(
                headers={
                    identity_header: identity,
                    secret_header: secret,
                }
            )

        elif location == "body":
            # Credentials in request body
            return ApplyResult(
                body={
                    identity_field: identity,
                    secret_field: secret,
                }
            )

        else:
            # Default: Combined in single Authorization header
            encoding = self.config.get("encoding", "basic")
            header_name = self.config.get("header_name", "Authorization")

            if encoding == "basic":
                credentials_str = f"{identity}:{secret}"
                encoded = base64.b64encode(credentials_str.encode()).decode()
                header_value = f"Basic {encoded}"
            else:
                # Raw encoding - just combine
                header_value = f"{identity}:{secret}"

            return ApplyResult(headers={header_name: header_value})

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return credentials for SDK usage."""
        identity_field = self.config.get("identity_field", "username")
        secret_field = self.config.get("secret_field", "password")

        identity = get_field_value(credentials, identity_field)
        if not identity:
            identity = (
                credentials.get("username")
                or credentials.get("email")
                or credentials.get("user")
            )

        secret = get_field_value(credentials, secret_field)
        if not secret:
            secret = (
                credentials.get("password")
                or credentials.get("api_token")
                or credentials.get("token")
            )

        return MaterializeResult(
            credentials={
                "username": identity,
                "password": secret,
            }
        )
