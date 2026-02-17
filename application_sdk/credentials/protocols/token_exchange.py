"""TOKEN_EXCHANGE protocol - exchange credentials for temporary token.

Covers OAuth 2.0, OIDC, SAML, JWT Bearer grants (~200+ services).
Pattern: Exchange credentials for temporary token, handle refresh.
"""

import time
from typing import Any, Dict, List, Optional

import httpx

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.exceptions import CredentialRefreshError
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import (
    ApplyResult,
    MaterializeResult,
    ValidationResult,
)
from application_sdk.credentials.types import FieldSpec, FieldType
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class TokenExchangeProtocol(BaseProtocol):
    """Protocol for OAuth 2.0, OIDC, JWT Bearer grants.

    Pattern: Exchange credentials for temporary token, handle refresh.
    Automatically refreshes tokens before expiry or on 401 response.

    Configuration options (via config or config_override):
        - grant_type: OAuth grant type (default: "client_credentials")
        - token_url: Token endpoint URL
        - refresh_url: Refresh endpoint (defaults to token_url)
        - scopes: List of OAuth scopes
        - header_name: Header for token (default: "Authorization")
        - token_prefix: Prefix before token (default: "Bearer ")
        - token_expiry_buffer: Refresh this many seconds before expiry (default: 60)

    Examples:
        >>> # Client credentials flow
        >>> protocol = TokenExchangeProtocol(config={
        ...     "token_url": "https://auth.example.com/oauth/token",
        ...     "grant_type": "client_credentials"
        ... })

        >>> # Authorization code flow (tokens already obtained)
        >>> protocol = TokenExchangeProtocol(config={
        ...     "token_url": "https://auth.example.com/oauth/token",
        ...     "grant_type": "authorization_code"
        ... })
    """

    default_fields: List[FieldSpec] = [
        FieldSpec(
            name="client_id",
            display_name="Client ID",
            required=True,
        ),
        FieldSpec(
            name="client_secret",
            display_name="Client Secret",
            sensitive=True,
            field_type=FieldType.PASSWORD,
            required=True,
        ),
        FieldSpec(
            name="token_url",
            display_name="Token URL",
            field_type=FieldType.URL,
            required=False,  # May be in config
        ),
        FieldSpec(
            name="access_token",
            display_name="Access Token",
            sensitive=True,
            required=False,
        ),
        FieldSpec(
            name="refresh_token",
            display_name="Refresh Token",
            sensitive=True,
            required=False,
        ),
    ]

    default_config: Dict[str, Any] = {
        "grant_type": "client_credentials",
        "token_url": None,
        "refresh_url": None,  # Falls back to token_url
        "scopes": [],
        "header_name": "Authorization",
        "token_prefix": "Bearer ",
        "token_expiry_buffer": 60,  # Refresh this many seconds before expiry
        "max_refresh_retries": 3,
    }

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Add OAuth token to request header."""
        access_token = self._get_valid_token(credentials)

        if not access_token:
            return ApplyResult()

        header_name = self.config.get("header_name", "Authorization")
        prefix = self.config.get("token_prefix", "Bearer ")

        return ApplyResult(headers={header_name: f"{prefix}{access_token}"})

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return credentials with current access token."""
        access_token = self._get_valid_token(credentials)
        expiry = credentials.get("token_expiry")

        return MaterializeResult(
            credentials={"access_token": access_token},
            expiry=expiry,
        )

    def refresh(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh the access token.

        If a refresh token is available, attempts refresh token flow first.
        On failure, falls back to re-authentication using client credentials.

        Raises:
            CredentialRefreshError: If both refresh and re-authentication fail.
        """
        refresh_token = get_field_value(credentials, "refresh_token")

        if refresh_token:
            # Try refresh token flow first - fall back to re-auth on failure
            # This is an intentional fallback pattern: refresh tokens may expire
            # or be revoked, so we attempt re-authentication as a recovery mechanism
            try:
                return self._refresh_with_token(credentials, refresh_token)
            except Exception as e:
                logger.info(
                    f"Refresh token flow failed, falling back to re-authentication: {e}",
                    extra={"fallback": True, "original_error": str(e)},
                )

        # Fall back to re-authentication (may raise CredentialRefreshError)
        return self._authenticate(credentials)

    def validate(self, credentials: Dict[str, Any]) -> ValidationResult:
        """Validate OAuth credentials."""
        errors: List[str] = []

        client_id = get_field_value(credentials, "client_id")
        client_secret = get_field_value(credentials, "client_secret")

        if not client_id:
            errors.append("client_id is required")
        if not client_secret:
            errors.append("client_secret is required")

        token_url = credentials.get("token_url") or self.config.get("token_url")
        access_token = get_field_value(credentials, "access_token")

        # Need either token_url (for authentication) or existing access_token
        if not token_url and not access_token:
            errors.append("Either token_url or access_token is required")

        return ValidationResult(valid=len(errors) == 0, errors=errors)

    def _get_valid_token(self, credentials: Dict[str, Any]) -> Optional[str]:
        """Get a valid access token, refreshing if necessary.

        Raises:
            CredentialRefreshError: If token refresh fails and no existing token available.
        """
        access_token = get_field_value(credentials, "access_token")
        token_expiry = credentials.get("token_expiry", 0)
        buffer = self.config.get("token_expiry_buffer", 60)

        # Check if token is still valid
        if access_token and time.time() < (token_expiry - buffer):
            return access_token

        # Token expired or missing - try to refresh
        try:
            updated = self.refresh(credentials)
            # Update the credentials dict in place for future calls
            credentials.update(updated)
            return updated.get("access_token")
        except Exception as e:
            logger.error(f"Failed to refresh/obtain token: {e}", exc_info=True)
            # If we have an existing token, return it as last resort (may be expired)
            # Otherwise, re-raise to signal authentication failure
            if access_token:
                logger.warning(
                    "Returning existing token as fallback (may be expired)",
                    extra={"has_token": True},
                )
                return access_token
            raise CredentialRefreshError(f"Failed to obtain valid token: {e}") from e

    def _refresh_with_token(
        self, credentials: Dict[str, Any], refresh_token: str
    ) -> Dict[str, Any]:
        """Refresh using refresh token."""
        token_url = (
            credentials.get("refresh_url")
            or credentials.get("token_url")
            or self.config.get("refresh_url")
            or self.config.get("token_url")
        )

        if not token_url:
            raise CredentialRefreshError("No token_url configured for refresh")

        client_id = get_field_value(credentials, "client_id")
        client_secret = get_field_value(credentials, "client_secret")

        try:
            response = httpx.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            token_data = response.json()

            return {
                **credentials,
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", refresh_token),
                "token_expiry": time.time() + token_data.get("expires_in", 3600),
            }
        except httpx.HTTPStatusError as e:
            raise CredentialRefreshError(f"Token refresh failed: {e}")
        except Exception as e:
            raise CredentialRefreshError(f"Token refresh error: {e}")

    def _authenticate(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Perform initial authentication (client credentials flow)."""
        token_url = credentials.get("token_url") or self.config.get("token_url")

        if not token_url:
            raise CredentialRefreshError("No token_url configured for authentication")

        client_id = get_field_value(credentials, "client_id")
        client_secret = get_field_value(credentials, "client_secret")
        grant_type = self.config.get("grant_type", "client_credentials")
        scopes = self.config.get("scopes", [])

        data: Dict[str, Any] = {
            "grant_type": grant_type,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        if scopes:
            data["scope"] = " ".join(scopes)

        try:
            response = httpx.post(
                token_url,
                data=data,
                timeout=30.0,
            )
            response.raise_for_status()
            token_data = response.json()

            return {
                **credentials,
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token"),
                "token_expiry": time.time() + token_data.get("expires_in", 3600),
            }
        except httpx.HTTPStatusError as e:
            raise CredentialRefreshError(f"Authentication failed: {e}")
        except Exception as e:
            raise CredentialRefreshError(f"Authentication error: {e}")
