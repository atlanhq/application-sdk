"""OAuth2 token manager with automatic secret store discovery."""

import time
from typing import Any, Dict, Optional

import aiohttp

from application_sdk.common.error_codes import ClientError
from application_sdk.constants import (
    APPLICATION_NAME,
    WORKFLOW_AUTH_CLIENT_ID_KEY,
    WORKFLOW_AUTH_CLIENT_SECRET_KEY,
    WORKFLOW_AUTH_ENABLED,
    WORKFLOW_AUTH_URL_KEY,
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

        Credentials are always fetched from the configured Dapr secret store component.
        The secret store component can be configured to use various backends
        (environment variables, AWS Secrets Manager, Azure Key Vault, etc.)
        """
        self.application_name = APPLICATION_NAME
        self.auth_config: Dict[str, Any] = SecretStoreInput.get_deployment_secret()
        self.auth_enabled: bool = WORKFLOW_AUTH_ENABLED
        self.auth_url: Optional[str] = None

        # Secret store credentials (cached after first fetch)
        self.credentials: Optional[Dict[str, str]] = None

        # Token data
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

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

        # Get credentials and ensure auth_url is set
        if not self.credentials:
            self.credentials = await self._extract_auth_credentials()
            if not self.credentials:
                raise ClientError(
                    f"{ClientError.AUTH_CREDENTIALS_ERROR}: OAuth2 credentials not found for application '{self.application_name}'. "
                )

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

        # Refresh token
        logger.info("Refreshing OAuth2 token")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials["client_id"],
                    "client_secret": self.credentials["client_secret"],
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

    async def _extract_auth_credentials(self) -> Optional[Dict[str, str]]:
        """Fetch app credentials from secret store - auth-specific logic"""
        if (
            WORKFLOW_AUTH_CLIENT_ID_KEY in self.auth_config
            and WORKFLOW_AUTH_CLIENT_SECRET_KEY in self.auth_config
        ):
            credentials = {
                "client_id": self.auth_config[WORKFLOW_AUTH_CLIENT_ID_KEY],
                "client_secret": self.auth_config[WORKFLOW_AUTH_CLIENT_SECRET_KEY],
            }

            if WORKFLOW_AUTH_URL_KEY in self.auth_config:
                self.auth_url = self.auth_config[WORKFLOW_AUTH_URL_KEY]

            return credentials
        return None

    def clear_cache(self) -> None:
        """Clear cached credentials and token.

        This method clears all cached authentication data, forcing fresh
        credential discovery and token refresh on next access.
        Useful for credential rotation scenarios.
        """
        # we are doing this to force a fetch of the credentials from secret store
        self.credentials = None
        self.auth_url = None
        self._access_token = None
        self._token_expiry = 0
        self.auth_config = {}

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
