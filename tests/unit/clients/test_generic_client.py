import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.generic import GenericClient
from application_sdk.test_utils.hypothesis.strategies.clients.sql import (
    sql_credentials_strategy,
)


@pytest.fixture
def generic_client():
    """Create a GenericClient instance for testing."""
    return GenericClient()


class TestGenericClient:
    """Test cases for GenericClient."""

    def test_initialization_default(self):
        """Test GenericClient initialization with default values."""
        client = GenericClient()
        assert client.credentials == {}

    def test_initialization_with_credentials(self):
        """Test GenericClient initialization with credentials."""
        credentials = {"username": "test", "password": "secret"}
        client = GenericClient(credentials=credentials)
        assert client.credentials == credentials

    @pytest.mark.asyncio
    async def test_load_not_implemented(self, generic_client):
        """Test that load method raises NotImplementedError."""
        credentials = {"username": "test", "password": "secret"}

        with pytest.raises(NotImplementedError, match="load method is not implemented"):
            await generic_client.load(credentials=credentials)

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_success(
        self, mock_async_client, generic_client
    ):
        """Test successful HTTP GET request execution."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "test"}

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        headers = {"Authorization": "Bearer token"}
        params = {"limit": 10}

        result = await generic_client.execute_http_get_request(
            url=url, headers=headers, params=params
        )

        assert result == mock_response
        mock_client_instance.get.assert_called_once_with(
            url, headers=headers, params=params
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_401_error(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request with 401 error."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await generic_client.execute_http_get_request(url=url)

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_429_error(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request with 429 error and retry."""
        # Mock response with 429
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "5"}

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generic_client.execute_http_get_request(
                url=url, max_retries=1, base_wait_time=1
            )

            assert result is None
            mock_sleep.assert_called()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_network_error(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request with network error and retry."""
        # Mock async context manager that raises network error
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = Exception("Network error")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generic_client.execute_http_get_request(
                url=url, max_retries=2, base_wait_time=1
            )

            assert result is None
            assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_success_after_retry(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request that succeeds after retry."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager that fails first, then succeeds
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = [
            Exception("Network error"),  # First call fails
            mock_response,  # Second call succeeds
        ]
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generic_client.execute_http_get_request(
                url=url, max_retries=2, base_wait_time=1
            )

            assert result == mock_response
            mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_with_auth_error_handler(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request with custom auth error handler."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        # Add custom auth error handler
        generic_client._handle_auth_error = AsyncMock()

        url = "https://api.example.com/test"

        result = await generic_client.execute_http_get_request(url=url, max_retries=1)

        assert result is None
        generic_client._handle_auth_error.assert_called_once()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.generic.httpx.AsyncClient")
    async def test_execute_http_get_request_timeout(
        self, mock_async_client, generic_client
    ):
        """Test HTTP GET request with timeout."""
        # Mock async context manager that raises timeout
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = asyncio.TimeoutError("Request timeout")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generic_client.execute_http_get_request(
                url=url, max_retries=1, timeout=5
            )

            assert result is None
            mock_sleep.assert_called()

    @given(credentials=sql_credentials_strategy)
    @settings(
        max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_initialization_with_various_credentials(self, credentials: Dict[str, Any]):
        """Property-based test for initialization with various credentials."""
        client = GenericClient(credentials=credentials)
        assert client.credentials == credentials

    def test_credentials_attribute_access(self, generic_client):
        """Test that credentials attribute can be accessed and modified."""
        assert generic_client.credentials == {}

        # Test setting credentials
        generic_client.credentials = {"new": "credentials"}
        assert generic_client.credentials == {"new": "credentials"}

    @pytest.mark.asyncio
    async def test_load_method_signature(self, generic_client):
        """Test that load method accepts the correct parameters."""
        credentials = {"username": "test", "password": "secret"}

        # Should not raise TypeError for wrong parameters
        with pytest.raises(NotImplementedError):
            await generic_client.load(credentials=credentials)

        # Test with additional kwargs
        with pytest.raises(NotImplementedError):
            await generic_client.load(credentials=credentials, extra_param="value")
