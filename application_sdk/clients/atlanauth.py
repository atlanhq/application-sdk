"""OAuth2 token manager with automatic secret store discovery."""

import time
from typing import Dict, Optional

import aiohttp

from application_sdk.common.error_codes import ClientError
from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_SECRET_COMPONENT,
    DEPLOYMENT_SECRET_NAME,
    WORKFLOW_AUTH_CLIENT_ID,
    WORKFLOW_AUTH_CLIENT_ID_KEY,
    WORKFLOW_AUTH_CLIENT_SECRET,
    WORKFLOW_AUTH_CLIENT_SECRET_KEY,
    WORKFLOW_AUTH_ENABLED,
    WORKFLOW_AUTH_URL,
)
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class AtlanAuthClient:
    """OAuth2 token manager for cloud service authentication.

    Currently supports Temporal authentication. Future versions will support:
    - Centralized token caching
    - Handling EventIngress authentication
    - Invoking more services with the same token

    The same token (with appropriate scopes) can be used for multiple services
    that the application needs to access.
    """

    def __init__(self):
        """Initialize the OAuth2 token manager.

        Args:
            application_name: Application name for secret key generation (optional)
        """
        self.application_name = APPLICATION_NAME
        self.auth_enabled = WORKFLOW_AUTH_ENABLED
        self.auth_url = WORKFLOW_AUTH_URL

        # Environment-based credentials (will fall back to secret store if empty)
        self._env_client_id = WORKFLOW_AUTH_CLIENT_ID
        self._env_client_secret = WORKFLOW_AUTH_CLIENT_SECRET

        # Secret store credentials (cached after first fetch)
        self.credentials: Optional[Dict[str, str]] = None

        # Token data
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

        # Return credentials if available
        if self.credentials:
            return self.credentials

        # Try environment variables first
        if self._env_client_id and self._env_client_secret:
            logger.info("Using credentials from environment variables")
            self.credentials = {
                "client_id": self._env_client_id,
                "client_secret": self._env_client_secret,
            }
            return self.credentials

        # Fall back to secret store
        credentials = await self._fetch_app_credentials_from_store()
        if not credentials:
            raise ClientError(
                f"{ClientError.AUTH_CREDENTIALS_ERROR}: OAuth2 credentials not found for application '{self.application_name}'. Expected either: 1. Environment variables: ATLAN_WORKFLOW_AUTH_CLIENT_ID, ATLAN_WORKFLOW_AUTH_CLIENT_SECRET 2. Secret store with key '{DEPLOYMENT_SECRET_NAME}' containing: {WORKFLOW_AUTH_CLIENT_ID_KEY}, {WORKFLOW_AUTH_CLIENT_SECRET_KEY} (or configure ATLAN_WORKFLOW_AUTH_CLIENT_ID_KEY, ATLAN_WORKFLOW_AUTH_CLIENT_SECRET_KEY)"
            )

        # Store the credentials from secret store
        self.credentials = credentials
        logger.info("Using credentials from secret store")
        return self.credentials

    async def get_access_token(self, force_refresh: bool = False) -> Optional[str]:
        """Get a valid access token, refreshing if necessary.

        The token contains all scopes configured for this application in the OAuth2 provider
        and can be used for multiple services (Temporal, Data transfer, etc.).

        Args:
            force_refresh: If True, forces token refresh regardless of expiry

        Returns:
            Optional[str]: A valid access token, or None if authentication is disabled

        Raises:
            ValueError: If authentication is disabled or credentials are missing
            AtlanAuthError: If token refresh fails
        """
        if not self.auth_enabled:
            return None

        if not self.auth_url:
            raise ClientError(
                f"{ClientError.AUTH_CONFIG_ERROR}: Auth URL is required when auth is enabled"
            )

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
                if not response.ok:
                    # Clear cached credentials and token on auth failure in case they're stale
                    self.clear_cache()
                    error_text = await response.text()
                    raise ClientError(
                        f"{ClientError.AUTH_TOKEN_REFRESH_ERROR}: Failed to refresh token (HTTP {response.status}): {error_text}"
                    )

                token_data = await response.json()

                # Validate required fields exist
                if "access_token" not in token_data or "expires_in" not in token_data:
                    raise ClientError(
                        f"{ClientError.AUTH_TOKEN_REFRESH_ERROR}: Missing required fields in OAuth2 response"
                    )

                self._access_token = token_data["access_token"]
                self._token_expiry = current_time + token_data["expires_in"]

                if self._access_token is None:
                    raise ClientError(
                        f"{ClientError.AUTH_TOKEN_REFRESH_ERROR}: Received null access token from server"
                    )
                return self._access_token

    async def get_authenticated_headers(self) -> Dict[str, str]:
        """Get authentication headers for HTTP requests.

        This method returns headers that can be used for any HTTP request
        to services that this application is authorized to access.

        Returns:
            Dict[str, str]: Headers dictionary with Authorization header

        Examples:
            >>> auth_client = AtlanAuthClient("user-management")
            >>> headers = await auth_client.get_authenticated_headers()
            >>> # Use headers for any HTTP request
            >>> async with aiohttp.ClientSession() as session:
            ...     await session.get("https://api.company.com/users", headers=headers)
        """
        if not self.auth_enabled:
            return {}

        token = await self.get_access_token()
        if token is None:
            return {}
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

    async def refresh_token(self) -> Optional[str]:
        """Force refresh the access token.

        This method forces a token refresh regardless of current token validity.
        Useful for credential rotation scenarios.

        Returns:
            Optional[str]: New access token, or None if authentication is disabled
        """
        return await self.get_access_token(force_refresh=True)

    async def _fetch_app_credentials_from_store(self) -> Optional[Dict[str, str]]:
        """Fetch app credentials from secret store - auth-specific logic"""
        try:
            secret_data = await SecretStoreInput.fetch_secret(
                DEPLOYMENT_SECRET_NAME, DEPLOYMENT_SECRET_COMPONENT
            )

            # Use configured key names from constants
            client_id_key = WORKFLOW_AUTH_CLIENT_ID_KEY
            client_secret_key = WORKFLOW_AUTH_CLIENT_SECRET_KEY

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
        self.credentials = None
        self._access_token = None
        self._token_expiry = 0

    def calculate_refresh_interval(self) -> int:
        """Calculate the optimal token refresh interval based on token expiry.

        Returns:
            int: Refresh interval in seconds
        """
        # Try to get token expiry time
        expiry_time = self.get_token_expiry_time()
        if expiry_time:
            # Calculate time until expiry
            time_until_expiry = self.get_time_until_expiry()
            if time_until_expiry and time_until_expiry > 0:
                # Refresh at 80% of the token lifetime, but at least every 5 minutes
                # and at most every 30 minutes
                refresh_interval = max(
                    5 * 60,  # Minimum 5 minutes
                    min(
                        30 * 60,  # Maximum 30 minutes
                        int(time_until_expiry * 0.8),  # 80% of token lifetime
                    ),
                )
                return refresh_interval

        # Default fallback: refresh every 14 minutes
        logger.info("Using default token refresh interval: 14 minutes")
        return 14 * 60
