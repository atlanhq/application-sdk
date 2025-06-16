"""Tests for the AuthManager class."""

import time
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from application_sdk.clients.auth import AuthManager


@pytest.fixture
def auth_manager() -> AuthManager:
    """Create an AuthManager instance for testing."""
    return AuthManager(
        application_name="test-app",
        auth_enabled=True,
        auth_url="http://auth.test/token",
        client_id="test-client",
        client_secret="test-secret",
    )


@pytest.fixture
def mock_dapr_client() -> Generator[MagicMock, None, None]:
    """Mock DaprClient for testing."""
    with patch("application_sdk.inputs.secretstore.DaprClient") as mock_client:
        mock_instance = mock_client.return_value
        mock_instance.__enter__.return_value = mock_instance
        mock_instance.__exit__.return_value = None
        yield mock_instance


@pytest.mark.asyncio
async def test_get_access_token_success(auth_manager: AuthManager) -> None:
    """Test successful token retrieval."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token = await auth_manager.get_access_token()
        assert token == "test-token"
        assert auth_manager._token_expiry > time.time()


@pytest.mark.asyncio
async def test_get_access_token_auth_disabled(auth_manager: AuthManager) -> None:
    """Test token retrieval when auth is disabled."""
    auth_manager.auth_enabled = False
    token = await auth_manager.get_access_token()
    assert token == ""


@pytest.mark.asyncio
async def test_get_access_token_auth_failure(auth_manager: AuthManager) -> None:
    """Test token retrieval failure handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Invalid credentials")
        mock_post.return_value.__aenter__.return_value = mock_response

        with pytest.raises(Exception, match="Failed to refresh token"):
            await auth_manager.get_access_token()


@pytest.mark.asyncio
async def test_get_access_token_network_error(auth_manager: AuthManager) -> None:
    """Test token retrieval network error handling."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value.__aenter__.side_effect = aiohttp.ClientError(
            "Network error"
        )

        with pytest.raises(aiohttp.ClientError, match="Network error"):
            await auth_manager.get_access_token()


@pytest.mark.asyncio
async def test_credential_discovery_from_secret_store(
    auth_manager: AuthManager, mock_dapr_client: MagicMock
) -> None:
    """Test credential discovery from secret store."""
    # Clear environment credentials to force secret store fallback
    auth_manager._env_client_id = ""
    auth_manager._env_client_secret = ""

    # Mock component discovery
    mock_metadata = MagicMock()
    mock_metadata.registered_components = [
        MagicMock(type="secretstores.aws", name="aws-secrets")
    ]
    mock_dapr_client.get_metadata.return_value = mock_metadata

    # Mock secret fetching
    mock_secret = MagicMock()
    mock_secret.secret = {
        "test_app_client_id": "discovered-client",
        "test_app_client_secret": "discovered-secret",
    }
    mock_dapr_client.get_secret.return_value = mock_secret

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        # This will trigger credential discovery through get_access_token
        await auth_manager.get_access_token()

        # Verify the discovered credentials were used
        call_args = mock_post.call_args
        assert call_args[1]["data"]["client_id"] == "discovered-client"
        assert call_args[1]["data"]["client_secret"] == "discovered-secret"


@pytest.mark.asyncio
async def test_credential_fallback_to_env(auth_manager: AuthManager) -> None:
    """Test credential fallback to environment variables."""
    # Mock component discovery to return None (no secret store)
    with patch(
        "application_sdk.inputs.secretstore.SecretStoreInput.discover_secret_component"
    ) as mock_discover:
        mock_discover.return_value = None

        credentials = await auth_manager._get_credentials()
        assert credentials["client_id"] == "test-client"
        assert credentials["client_secret"] == "test-secret"


@pytest.mark.asyncio
async def test_credential_discovery_failure(auth_manager: AuthManager) -> None:
    """Test credential discovery failure handling."""
    # Create an auth manager without fallback credentials
    auth_manager_no_fallback = AuthManager(
        application_name="test-app",
        auth_enabled=True,
        auth_url="http://auth.test/token",
    )

    with patch(
        "application_sdk.inputs.secretstore.SecretStoreInput.discover_secret_component"
    ) as mock_discover:
        mock_discover.return_value = None

        with pytest.raises(ValueError, match="OAuth2 credentials not found"):
            await auth_manager_no_fallback._get_credentials()


@pytest.mark.asyncio
async def test_token_caching(auth_manager: AuthManager) -> None:
    """Test token caching behavior."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        # First call - should fetch new token
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "test-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token1 = await auth_manager.get_access_token()
        assert token1 == "test-token"
        assert mock_post.call_count == 1

        # Second call - should use cached token
        token2 = await auth_manager.get_access_token()
        assert token2 == "test-token"
        assert mock_post.call_count == 1  # No additional calls


@pytest.mark.asyncio
async def test_token_refresh(auth_manager: AuthManager) -> None:
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

        await auth_manager.get_access_token()

        # Simulate token expiry
        auth_manager._token_expiry = time.time() - 1

        # Should fetch new token
        mock_response2 = AsyncMock()
        mock_response2.status = 200
        mock_response2.json = AsyncMock(
            return_value={"access_token": "test-token-2", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response2

        token = await auth_manager.get_access_token()
        assert token == "test-token-2"
        assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_get_authenticated_headers(auth_manager: AuthManager) -> None:
    """Test authenticated header generation."""
    with patch.object(auth_manager, "get_access_token", return_value="test-token"):
        headers = await auth_manager.get_authenticated_headers()
        assert headers == {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_get_authenticated_headers_auth_disabled(
    auth_manager: AuthManager,
) -> None:
    """Test header generation when auth is disabled."""
    auth_manager.auth_enabled = False
    headers = await auth_manager.get_authenticated_headers()
    assert headers == {}


@pytest.mark.asyncio
async def test_is_token_valid(auth_manager: AuthManager) -> None:
    """Test token validity checking."""
    # No token
    assert not await auth_manager.is_token_valid()

    # Valid token
    auth_manager._access_token = "test-token"
    auth_manager._token_expiry = time.time() + 3600
    assert await auth_manager.is_token_valid()

    # Expired token
    auth_manager._token_expiry = time.time() - 1
    assert not await auth_manager.is_token_valid()


@pytest.mark.asyncio
async def test_refresh_token(auth_manager: AuthManager) -> None:
    """Test forced token refresh."""
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(
            return_value={"access_token": "new-token", "expires_in": 3600}
        )
        mock_post.return_value.__aenter__.return_value = mock_response

        token = await auth_manager.refresh_token()
        assert token == "new-token"
        assert auth_manager._access_token == "new-token"


def test_clear_cache(auth_manager: AuthManager) -> None:
    """Test cache clearing."""
    # Set some cached values
    auth_manager._cached_credentials = {"test": "credentials"}
    auth_manager._access_token = "test-token"
    auth_manager._token_expiry = time.time() + 3600

    auth_manager.clear_cache()

    assert auth_manager._cached_credentials is None
    assert auth_manager._access_token is None
    assert auth_manager._token_expiry == 0


def test_get_application_name(auth_manager: AuthManager) -> None:
    """Test application name getter."""
    assert auth_manager.get_application_name() == "test-app"


def test_is_auth_enabled(auth_manager: AuthManager) -> None:
    """Test auth enabled status getter."""
    assert auth_manager.is_auth_enabled() is True
    auth_manager.auth_enabled = False
    assert auth_manager.is_auth_enabled() is False


@pytest.mark.asyncio
async def test_credential_discovery_secret_store_failure(
    auth_manager: AuthManager, mock_dapr_client: MagicMock
) -> None:
    """Test credential discovery when secret store fails."""
    # Mock component discovery to succeed
    mock_metadata = MagicMock()
    mock_metadata.registered_components = [
        MagicMock(type="secretstores.aws", name="aws-secrets")
    ]
    mock_dapr_client.get_metadata.return_value = mock_metadata

    # Mock secret fetching to fail
    mock_dapr_client.get_secret.side_effect = Exception("Secret not found")

    # Should fallback to environment variables
    credentials = await auth_manager._get_credentials()
    assert credentials["client_id"] == "test-client"
    assert credentials["client_secret"] == "test-secret"


@pytest.mark.asyncio
async def test_credential_discovery_no_matching_keys(
    auth_manager: AuthManager, mock_dapr_client: MagicMock
) -> None:
    """Test credential discovery when secret store has no matching keys."""
    # Mock component discovery
    mock_metadata = MagicMock()
    mock_metadata.registered_components = [
        MagicMock(type="secretstores.aws", name="aws-secrets")
    ]
    mock_dapr_client.get_metadata.return_value = mock_metadata

    # Mock secret fetching with wrong keys
    mock_secret = MagicMock()
    mock_secret.secret = {
        "other_app_client_id": "other-client",
        "other_app_client_secret": "other-secret",
    }
    mock_dapr_client.get_secret.return_value = mock_secret

    # Should fallback to environment variables
    credentials = await auth_manager._get_credentials()
    assert credentials["client_id"] == "test-client"
    assert credentials["client_secret"] == "test-secret"


@pytest.mark.asyncio
async def test_token_refresh_clears_credential_cache_on_failure(
    auth_manager: AuthManager,
) -> None:
    """Test that credential cache is cleared on auth failure."""
    # Set cached credentials
    auth_manager._cached_credentials = {
        "client_id": "cached",
        "client_secret": "cached",
    }

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.text = AsyncMock(return_value="Invalid credentials")
        mock_post.return_value.__aenter__.return_value = mock_response

        with pytest.raises(Exception, match="Failed to refresh token"):
            await auth_manager.get_access_token()

        # Credential cache should be cleared
        assert auth_manager._cached_credentials is None