"""STATIC_SECRET protocol - single secret value added to request.

Covers API Keys, PATs, Bearer Tokens (~150+ services).
Pattern: Single secret value added to request header or query param.
"""

from typing import Any, Dict, List

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult, MaterializeResult
from application_sdk.credentials.types import FieldSpec, FieldType


class StaticSecretProtocol(BaseProtocol):
    """Protocol for API keys, PATs, Bearer tokens.

    Pattern: Single secret value added to request header or query param.

    Configuration options (via config or config_override):
        - location: "header" or "query" (default: "header")
        - header_name: Header name (default: "Authorization")
        - prefix: Prefix before token (default: "Bearer ")
        - param_name: Query param name if location="query" (default: "api_key")
        - field_name: Which credential field contains the secret (default: "api_key")

    Examples:
        >>> # Default: Bearer token in Authorization header
        >>> protocol = StaticSecretProtocol()
        >>> result = protocol.apply({"api_key": "sk_xxx"}, {})
        >>> result.headers
        {'Authorization': 'Bearer sk_xxx'}

        >>> # API key in query param
        >>> protocol = StaticSecretProtocol(config={
        ...     "location": "query",
        ...     "param_name": "appid"
        ... })
        >>> result = protocol.apply({"api_key": "xxx"}, {})
        >>> result.query_params
        {'appid': 'xxx'}

        >>> # Custom header without prefix
        >>> protocol = StaticSecretProtocol(config={
        ...     "header_name": "X-API-Key",
        ...     "prefix": ""
        ... })
    """

    default_fields: List[FieldSpec] = [
        FieldSpec(
            name="api_key",
            display_name="API Key",
            sensitive=True,
            field_type=FieldType.PASSWORD,
            required=True,
        )
    ]

    default_config: Dict[str, Any] = {
        "location": "header",  # header or query
        "header_name": "Authorization",
        "prefix": "Bearer ",  # Prefix before token
        "param_name": "api_key",  # For query param location
        "field_name": "api_key",  # Which field contains the secret
    }

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Add API key to request header or query param."""
        field_name = self.config.get("field_name", "api_key")

        # Use alias resolution to find the secret value
        secret = get_field_value(credentials, field_name)
        if not secret:
            # Fallback to common aliases
            secret = (
                credentials.get("api_key")
                or credentials.get("token")
                or credentials.get("secret_key")
                or credentials.get("access_token")
            )

        if not secret:
            return ApplyResult()

        location = self.config.get("location", "header")

        if location == "query":
            param_name = self.config.get("param_name", "api_key")
            return ApplyResult(query_params={param_name: secret})
        else:
            header_name = self.config.get("header_name", "Authorization")
            prefix = self.config.get("prefix", "Bearer ")
            return ApplyResult(headers={header_name: f"{prefix}{secret}"})

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return credentials for SDK usage."""
        field_name = self.config.get("field_name", "api_key")
        secret = get_field_value(credentials, field_name)

        if not secret:
            secret = (
                credentials.get("api_key")
                or credentials.get("token")
                or credentials.get("secret_key")
            )

        return MaterializeResult(credentials={field_name: secret})
