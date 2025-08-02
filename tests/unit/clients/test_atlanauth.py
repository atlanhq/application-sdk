"""Tests for the AtlanAuthClient class."""

import time
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from application_sdk.clients.atlanauth import AtlanAuthClient
from application_sdk.common.error_codes import ClientError


@pytest.fixture
def auth_client() -> AtlanAuthClient:
    """Create an AtlanAuthClient instance for testing."""
    # Mock the constants at the module level where they're imported
    with patch("application_sdk.clients.atlanauth.WORKFLOW_AUTH_ENABLED", True), patch(
        "application_sdk.clients.atlanauth.WORKFLOW_AUTH_URL", "http://auth.test/token"
    ), patch(
        "application_sdk.clients.atlanauth.WORKFLOW_AUTH_CLIENT_ID", "test-client"
    ), patch(
        "application_sdk.clients.atlanauth.WORKFLOW_AUTH_CLIENT_SECRET", "test-secret"
    ), patch("application_sdk.clients.atlanauth.APPLICATION_NAME", "test-app"):
        return AtlanAuthClient()


@pytest.mark.asyncio
async def test_get_access_token_success(auth_client: AtlanAuthClient) -> None:
    """Test successful token retrieval."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token = await auth_client.get_access_token()
        assert token == "test-token"
        assert auth_client._token_expiry > time.time()


@pytest.mark.asyncio
async def test_get_access_token_auth_disabled(auth_client: AtlanAuthClient) -> None:
    """Test token retrieval when auth is disabled."""
    auth_client.auth_enabled = False
    token = await auth_client.get_access_token()
    assert token is None


@pytest.mark.asyncio
async def test_get_access_token_auth_failure(auth_client: AtlanAuthClient) -> None:
    """Test token retrieval failure handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.ok = False
        mock_response.text = AsyncMock(return_value="Invalid credentials")
        mock_post.return_value.__aenter__.return_value = mock_response

        with pytest.raises(ClientError, match="Failed to refresh token"):
            await auth_client.get_access_token()


@pytest.mark.asyncio
async def test_get_access_token_network_error(auth_client: AtlanAuthClient) -> None:
    """Test token retrieval network error handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value.__aenter__.side_effect = aiohttp.ClientError(
            "Network error"
        )

        with pytest.raises(aiohttp.ClientError, match="Network error"):
            await auth_client.get_access_token()


@pytest.mark.asyncio
async def test_credential_discovery_from_secret_store(
    auth_client: AtlanAuthClient,
) -> None:
    """Test credential discovery from secret store."""
    # Clear environment credentials to force secret store fallback
    auth_client._env_client_id = ""
    auth_client._env_client_secret = ""

    # Mock the secret fetching
    with patch(
        "application_sdk.inputs.secretstore.SecretStoreInput.fetch_secret",
        return_value={
            "test_app_client_id": "discovered-client",
            "test_app_client_secret": "discovered-secret",
        },
    ), patch(
        "application_sdk.clients.atlanauth.WORKFLOW_AUTH_CLIENT_ID_KEY",
        "test_app_client_id",
    ), patch(
        "application_sdk.clients.atlanauth.WORKFLOW_AUTH_CLIENT_SECRET_KEY",
        "test_app_client_secret",
    ), patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        # This will trigger credential discovery through get_access_token
        await auth_client.get_access_token()

        # Verify the discovered credentials were used
        call_args = mock_post.call_args
        assert call_args[1]["data"]["client_id"] == "discovered-client"
        assert call_args[1]["data"]["client_secret"] == "discovered-secret"


@pytest.mark.asyncio
async def test_credential_fallback_to_env(auth_client: AtlanAuthClient) -> None:
    """Test credential fallback to environment variables."""
    # Should use environment credentials when secret store fails
    credentials = await auth_client._get_credentials()
    assert credentials["client_id"] == "test-client"
    assert credentials["client_secret"] == "test-secret"


@pytest.mark.asyncio
async def test_credential_discovery_failure(auth_client: AtlanAuthClient) -> None:
    """Test credential discovery failure handling."""
    # Create an auth client without fallback credentials by clearing env vars
    with patch("application_sdk.clients.atlanauth.APPLICATION_NAME", "test-app"):
        auth_client_no_fallback = AtlanAuthClient()
    # Clear environment credentials to force secret store fallback
    auth_client_no_fallback._env_client_id = ""
    auth_client_no_fallback._env_client_secret = ""

    with patch(
        "application_sdk.inputs.secretstore.SecretStoreInput.fetch_secret",
        side_effect=Exception("Secret not found"),
    ):
        with pytest.raises(ClientError, match="OAuth2 credentials not found"):
            await auth_client_no_fallback._get_credentials()


@pytest.mark.asyncio
async def test_token_caching(auth_client: AtlanAuthClient) -> None:
    """Test token caching behavior."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        # First call - should fetch new token
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token1 = await auth_client.get_access_token()
        assert token1 == "test-token"
        assert mock_post.call_count == 1

        # Second call - should use cached token
        token2 = await auth_client.get_access_token()
        assert token2 == "test-token"
        assert mock_post.call_count == 1  # No additional calls


@pytest.mark.asyncio
async def test_token_refresh(auth_client: AtlanAuthClient) -> None:
    """Test token refresh behavior."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        # Initial token
        mock_response1 = AsyncMock()
        mock_response1.status = 200
        mock_response1.json = AsyncMock(
            return_value={
                "access_token": "test-token-1",
                "expires_in": 1,  # Short expiry
            }
        )
        mock_post.return_value.__aenter__.return_value = mock_response1

        await auth_client.get_access_token()

        # Simulate token expiry
        auth_client._token_expiry = time.time() - 1

        # Should fetch new token
        mock_response2 = AsyncMock()
        mock_response2.status = 200
        mock_response2.json = AsyncMock(
            return_value={"access_token": "test-token-2", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response2

        token = await auth_client.get_access_token()
        assert token == "test-token-2"
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_get_authenticated_headers(auth_client: AtlanAuthClient) -> None:
    """Test authenticated header generation."""
    with patch.object(auth_client, "get_access_token", return_value="test-token"):
        headers = await auth_client.get_authenticated_headers()
        assert headers == {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_get_authenticated_headers_auth_disabled(
    auth_client: AtlanAuthClient,
) -> None:
    """Test header generation when auth is disabled."""
    auth_client.auth_enabled = False
    headers = await auth_client.get_authenticated_headers()
    assert headers == {}


@pytest.mark.asyncio
async def test_get_authenticated_headers_no_token(auth_client: AtlanAuthClient) -> None:
    """Test header generation when token is None."""
    with patch.object(auth_client, "get_access_token", return_value=None):
        headers = await auth_client.get_authenticated_headers()
        assert headers == {}


@pytest.mark.asyncio
async def test_is_token_valid(auth_client: AtlanAuthClient) -> None:
    """Test token validity checking."""
    # No token
    assert not await auth_client.is_token_valid()

    # Valid token
    auth_client._access_token = "test-token"
    auth_client._token_expiry = time.time() + 3600
    assert await auth_client.is_token_valid()

    # Expired token
    auth_client._token_expiry = time.time() - 1
    assert not await auth_client.is_token_valid()

    # Auth disabled
    auth_client.auth_enabled = False
    assert await auth_client.is_token_valid()


@pytest.mark.asyncio
async def test_refresh_token(auth_client: AtlanAuthClient) -> None:
    """Test forced token refresh."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "new-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token = await auth_client.refresh_token()
        assert token == "new-token"
        assert auth_client._access_token == "new-token"


def test_clear_cache(auth_client: AtlanAuthClient) -> None:
    """Test cache clearing."""
    # Set some cached values
    auth_client.credentials = {"client_id": "test", "client_secret": "credentials"}
    auth_client._access_token = "test-token"
    auth_client._token_expiry = time.time() + 3600

    auth_client.clear_cache()

    assert auth_client.credentials is None
    assert auth_client._access_token is None
    assert auth_client._token_expiry == 0


@pytest.mark.asyncio
async def test_token_refresh_clears_credential_cache_on_failure(
    auth_client: AtlanAuthClient,
) -> None:
    """Test that credential cache is cleared on auth failure."""
    # Set cached credentials
    auth_client.credentials = {
        "client_id": "cached",
        "client_secret": "cached",
    }

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.ok = False
        mock_response.text = AsyncMock(return_value="Invalid credentials")
        mock_post.return_value.__aenter__.return_value = mock_response

        with pytest.raises(ClientError, match="Failed to refresh token"):
            await auth_client.get_access_token()

        # Credential cache should be cleared
        assert auth_client.credentials is None


@pytest.mark.asyncio
async def test_auth_config_error(auth_client: AtlanAuthClient) -> None:
    """Test auth configuration error when URL is missing."""
    auth_client.auth_url = None
    with pytest.raises(ClientError, match="Auth URL is required"):
        await auth_client.get_access_token()


@pytest.mark.asyncio
async def test_null_token_handling(auth_client: AtlanAuthClient) -> None:
    """Test handling of null token from server."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": None, "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        with pytest.raises(ClientError, match="Received null access token"):
            await auth_client.get_access_token()
