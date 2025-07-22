"""OAuth2 token manager with automatic secret store discovery."""

import time
from typing import Dict, Optional

import aiohttp

from application_sdk.constants import (
    APPLICATION_NAME,
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

    def __init__(self, application_name: str | None = None):
        """Initialize the OAuth2 token manager.

        Args:
            application_name: Application name for secret key generation (optional)
        """
        self.application_name = (
            application_name if application_name else APPLICATION_NAME
        )
        self.auth_enabled = WORKFLOW_AUTH_ENABLED
        self.auth_url = WORKFLOW_AUTH_URL

        # Environment-based credentials (will fall back to secret store if empty)
        self._env_client_id = WORKFLOW_AUTH_CLIENT_ID
        self._env_client_secret = WORKFLOW_AUTH_CLIENT_SECRET

        # Cached data
        self._cached_credentials: Optional[Dict[str, str]] = None
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

    async def _get_credentials(self) -> Dict[str, str]:
        """Get credentials using the standardized approach.

        Tries environment variables first, falls back to secret store.

        Returns:
            Dict containing client_id and client_secret

        Raises:
            ValueError: If credentials cannot be obtained from any source
        """

        logger.debug("Fetching credentials for token refresh")

        # Return cached credentials if available
        if self._cached_credentials:
            return self._cached_credentials

        # Try environment variables/constructor params first
        if self._env_client_id and self._env_client_secret:
            logger.info("Using credentials from environment variables")
            credentials = {
                "client_id": self._env_client_id,
                "client_secret": self._env_client_secret,
            }
        else:
            # Fall back to secret store
            credentials = await self._fetch_app_credentials_from_store()

            if not credentials:
                app_name = self.application_name.lower().replace("-", "_")
                raise ValueError(
                    f"OAuth2 credentials not found for application '{self.application_name}'. "
                    f"Expected either:\n"
                    f"1. Environment variables: ATLAN_WORKFLOW_AUTH_CLIENT_ID, ATLAN_WORKFLOW_AUTH_CLIENT_SECRET\n"
                    f"2. Secret store with key 'atlan-deployment-secrets' containing: "
                    f"{app_name}_client_id, {app_name}_client_secret"
                )

        # Cache the credentials
        self._cached_credentials = credentials
        return credentials

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """Get a valid access token, refreshing if necessary.

        The token contains all scopes configured for this application in the OAuth2 provider
        and can be used for multiple services (Temporal, API Gateway, Analytics, etc.).

        Args:
            force_refresh: If True, forces token refresh regardless of expiry

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

        # Return existing token if it's still valid (with 30s buffer) and not forcing refresh
        current_time = time.time()
        if (
            not force_refresh
            and self._access_token
            and current_time < self._token_expiry - 30
        ):
            return self._access_token

        # Get credentials and refresh token
        credentials = await self._get_credentials()
        logger.info("Refreshing OAuth2 token")

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
                    error_text = await response.text()
                    raise Exception(
                        f"Failed to refresh token (HTTP {response.status}): {error_text}"
                    )

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

    def get_token_expiry_time(self) -> Optional[float]:
        """Get the expiry time of the current token.

        Returns:
            Optional[float]: Unix timestamp of token expiry, or None if no token
        """
        return self._token_expiry if self._access_token else None

    def get_time_until_expiry(self) -> Optional[float]:
        """Get the time remaining until token expires.

        Returns:
            Optional[float]: Seconds until expiry, or None if no token
        """
        if not self._access_token or not self._token_expiry:
            return None

        return max(0, self._token_expiry - time.time())

    async def refresh_token(self) -> str:
        """Force refresh the access token.

        This method forces a token refresh regardless of current token validity.
        Useful for credential rotation scenarios.

        Returns:
            str: New access token
        """
        return await self.get_access_token(force_refresh=True)

    async def _fetch_app_credentials_from_store(self) -> Optional[Dict[str, str]]:
        """Fetch app credentials from secret store - auth-specific logic"""
        component_name = SecretStoreInput.discover_secret_component()
        if not component_name:
            return None

        try:
            secret_data = await SecretStoreInput.fetch_secret(
                "atlan-deployment-secrets", component_name
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
