"""OAuth2 token manager with automatic secret store discovery."""

import time
from typing import Dict, Optional

import aiohttp

from application_sdk.constants import (
    WORKFLOW_AUTH_CLIENT_ID,
    WORKFLOW_AUTH_CLIENT_SECRET,
    WORKFLOW_AUTH_ENABLED,
    WORKFLOW_AUTH_URL,
)
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class AuthManager:
    """OAuth2 token manager for cloud service authentication.

    Currently supports Temporal authentication. Future versions will support:
    - Centralized token caching
    - Handling EventIngress authentication
    - Invoking more services with the same token

    The same token (with appropriate scopes) can be used for multiple services
    that the application needs to access.
    """

    def __init__(
        self,
        application_name: str,
        auth_enabled: bool | None = None,
        auth_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        """Initialize the OAuth2 token manager.

        Args:
            application_name: Application name for secret key generation
            auth_enabled: Whether authentication is enabled
            auth_url: OAuth2 token endpoint URL
            client_id: OAuth2 client ID (fallback if secret store fails)
            client_secret: OAuth2 client secret (fallback if secret store fails)
        """
        self.application_name = application_name
        self.auth_enabled = (
            auth_enabled if auth_enabled is not None else WORKFLOW_AUTH_ENABLED
        )
        self.auth_url = auth_url if auth_url else WORKFLOW_AUTH_URL

        # Fallback credentials from environment/constructor
        self._fallback_client_id = client_id if client_id else WORKFLOW_AUTH_CLIENT_ID
        self._fallback_client_secret = (
            client_secret if client_secret else WORKFLOW_AUTH_CLIENT_SECRET
        )

        # Cached data
        self._cached_credentials: Optional[Dict[str, str]] = None
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    async def _get_credentials(self) -> Dict[str, str]:
        """Get credentials using the standardized approach.

        Tries secret store first, falls back to environment variables.

        Returns:
            Dict containing client_id and client_secret

        Raises:
            ValueError: If credentials cannot be obtained from any source
        """
        # Return cached credentials if available
        if self._cached_credentials:
            return self._cached_credentials

        # Try secret store first
        credentials = await self._fetch_app_credentials_from_store()

        # Fall back to environment variables/constructor params
        if not credentials:
            if self._fallback_client_id and self._fallback_client_secret:
                logger.info("Using fallback credentials from environment variables")
                credentials = {
                    "client_id": self._fallback_client_id,
                    "client_secret": self._fallback_client_secret,
                }
            else:
                app_name = self.application_name.lower().replace("-", "_")
                raise ValueError(
                    f"OAuth2 credentials not found for application '{self.application_name}'. "
                    f"Expected either:\n"
                    f"1. Secret store with key 'atlan-deployment-secrets' containing: "
                    f"{app_name}_client_id, {app_name}_client_secret\n"
                    f"2. Environment variables: ATLAN_WORKFLOW_AUTH_CLIENT_ID, ATLAN_WORKFLOW_AUTH_CLIENT_SECRET"
                )

        # Cache the credentials
        self._cached_credentials = credentials
        return credentials

    async def get_access_token(self) -> str:
        """Get a valid access token, refreshing if necessary.

        The token contains all scopes configured for this application in the OAuth2 provider
        and can be used for multiple services (Temporal, API Gateway, Analytics, etc.).

        Returns:
            str: A valid access token

        Raises:
            ValueError: If authentication is disabled or credentials are missing
            Exception: If token refresh fails
        """
        if not self.auth_enabled:
            return ""

        if not self.auth_url:
            raise ValueError("Auth URL is required when auth is enabled")

        # Return existing token if it's still valid (with 30s buffer)
        current_time = time.time()
        if self._access_token and current_time < self._token_expiry - 30:
            return self._access_token

        # Get credentials and refresh token
        credentials = await self._get_credentials()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status != 200:
                    # Clear cached credentials on auth failure in case they're stale
                    self._cached_credentials = None
                    raise Exception(f"Failed to refresh token: {await response.text()}")

                token_data = await response.json()
                self._access_token = token_data["access_token"]
                self._token_expiry = current_time + token_data["expires_in"]
                assert self._access_token is not None
                return self._access_token

    async def get_authenticated_headers(self) -> Dict[str, str]:
        """Get authentication headers for HTTP requests.

        This method returns headers that can be used for any HTTP request
        to services that this application is authorized to access.

        Returns:
            Dict[str, str]: Headers dictionary with Authorization header

        Examples:
            >>> auth_manager = AuthManager("user-management")
            >>> headers = await auth_manager.get_authenticated_headers()
            >>> # Use headers for any HTTP request
            >>> async with aiohttp.ClientSession() as session:
            ...     await session.get("https://api.company.com/users", headers=headers)
        """
        if not self.auth_enabled:
            return {}

        token = await self.get_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def is_token_valid(self) -> bool:
        """Check if current token is valid (not expired).

        Returns:
            bool: True if token exists and is not expired
        """
        if not self.auth_enabled:
            return True  # No auth required

        if not self._access_token:
            return False

        current_time = time.time()
        return current_time < self._token_expiry - 30  # 30s buffer

    async def refresh_token(self) -> str:
        """Force refresh the access token.

        This method forces a token refresh regardless of current token validity.
        Useful for credential rotation scenarios.

        Returns:
            str: New access token
        """
        # Clear cached token to force refresh
        self._access_token = None
        self._token_expiry = 0

        return await self.get_access_token()

    async def _fetch_app_credentials_from_store(self) -> Optional[Dict[str, str]]:
        """Fetch app credentials from secret store - auth-specific logic"""
        component_name = SecretStoreInput.discover_secret_component()
        if not component_name:
            return None

        try:
            secret_data = await SecretStoreInput.fetch_secret(
                component_name, "atlan-deployment-secrets"
            )

            # Auth-specific key generation
            app_name = self.application_name.lower().replace("-", "_")
            client_id_key = f"{app_name}_client_id"
            client_secret_key = f"{app_name}_client_secret"

            if client_id_key in secret_data and client_secret_key in secret_data:
                return {
                    "client_id": secret_data[client_id_key],
                    "client_secret": secret_data[client_secret_key],
                }
            return None
        except Exception:
            return None

    def clear_cache(self) -> None:
        """Clear cached credentials and token.

        This method clears all cached authentication data, forcing fresh
        credential discovery and token refresh on next access.
        Useful for credential rotation scenarios.
        """
        self._cached_credentials = None
        self._access_token = None
        self._token_expiry = 0

    def get_application_name(self) -> str:
        """Get the application name.

        Returns:
            str: Application name used for credential discovery
        """
        return self.application_name

    def is_auth_enabled(self) -> bool:
        """Check if authentication is enabled.

        Returns:
            bool: True if authentication is enabled
        """
        return self.auth_enabled
