"""Tests for the AtlanAuthClient class."""

import time
from unittest.mock import patch

import pytest

from application_sdk.clients.atlan_auth import AtlanAuthClient


@pytest.fixture
async def auth_client() -> AtlanAuthClient:
    """Create an AtlanAuthClient instance for testing."""
    mock_config = {
        "test_app_client_id": "test-client",
        "test_app_client_secret": "test-secret",
        "workflow_auth_enabled": True,
        "workflow_auth_url": "http://auth.test/token",
    }

    with patch("application_sdk.constants.WORKFLOW_AUTH_ENABLED", True), patch(
        "application_sdk.constants.WORKFLOW_AUTH_URL_KEY", "workflow_auth_url"
    ), patch("application_sdk.constants.APPLICATION_NAME", "test-app"), patch(
        "application_sdk.clients.atlan_auth.APPLICATION_NAME", "test-app"
    ), patch(
        "application_sdk.constants.WORKFLOW_AUTH_CLIENT_ID_KEY", "test_app_client_id"
    ), patch(
        "application_sdk.constants.WORKFLOW_AUTH_CLIENT_SECRET_KEY",
        "test_app_client_secret",
    ), patch(
        "application_sdk.clients.atlan_auth.SecretStoreInput.get_deployment_secret",
        return_value=mock_config,
    ):
        client = AtlanAuthClient()
        return client


@pytest.mark.asyncio
async def test_get_access_token_auth_disabled(auth_client: AtlanAuthClient) -> None:
    """Test token retrieval when auth is disabled."""
    auth_client.auth_enabled = False
    token = await auth_client.get_access_token()
    assert token is None


@pytest.mark.asyncio
async def test_credential_discovery_failure(auth_client: AtlanAuthClient) -> None:
    """Test credential discovery failure handling."""
    # Create an auth client
    with patch("application_sdk.clients.atlan_auth.APPLICATION_NAME", "test-app"):
        auth_client_no_fallback = AtlanAuthClient()

    with patch(
        "application_sdk.clients.atlan_auth.SecretStoreInput.get_deployment_secret",
        return_value={},  # Empty config means no credentials
    ):
        credentials = await auth_client_no_fallback._extract_auth_credentials()
        assert credentials is None


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


def test_get_token_expiry_time(auth_client: AtlanAuthClient) -> None:
    """Test getting token expiry time."""
    # No token
    assert auth_client.get_token_expiry_time() is None

    # With token
    auth_client._access_token = "test-token"
    auth_client._token_expiry = 1234567890.0
    assert auth_client.get_token_expiry_time() == 1234567890.0


def test_get_time_until_expiry(auth_client: AtlanAuthClient) -> None:
    """Test getting time until expiry."""
    # No token
    assert auth_client.get_time_until_expiry() is None

    # With token
    auth_client._access_token = "test-token"
    auth_client._token_expiry = time.time() + 3600
    time_until = auth_client.get_time_until_expiry()
    assert time_until is not None
    assert 0 < time_until <= 3600

    # Expired token
    auth_client._token_expiry = time.time() - 1
    assert auth_client.get_time_until_expiry() == 0


def test_calculate_refresh_interval(auth_client: AtlanAuthClient) -> None:
    """Test calculating refresh interval."""
    # No token - should return default
    interval = auth_client.calculate_refresh_interval()
    assert interval == 14 * 60  # 14 minutes

    # With token
    auth_client._access_token = "test-token"
    auth_client._token_expiry = time.time() + 3600  # 1 hour
    interval = auth_client.calculate_refresh_interval()
    assert 5 * 60 <= interval <= 30 * 60  # Between 5 and 30 minutes
