import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.base import BaseClient
from application_sdk.test_utils.hypothesis.strategies.clients.sql import (
    sql_credentials_strategy,
)


@pytest.fixture
def base_client():
    """Create a BaseClient instance for testing."""
    return BaseClient()


class TestBaseClient:
    """Test cases for BaseClient."""

    def test_initialization_default(self):
        """Test BaseClient initialization with default values."""
        client = BaseClient()
        assert client.credentials == {}

    def test_initialization_with_credentials(self):
        """Test BaseClient initialization with credentials."""
        credentials = {"username": "test", "password": "secret"}
        client = BaseClient(credentials=credentials)
        assert client.credentials == credentials

    @pytest.mark.asyncio
    async def test_load_not_implemented(self, base_client):
        """Test that load method raises NotImplementedError."""
        credentials = {"username": "test", "password": "secret"}

        with pytest.raises(NotImplementedError, match="load method is not implemented"):
            await base_client.load(credentials=credentials)

    @pytest.mark.asyncio
    async def test_handle_auth_error_not_implemented(self, base_client):
        """Test that _handle_auth_error method raises NotImplementedError by default."""
        with pytest.raises(
            NotImplementedError,
            match="Subclasses must implement _handle_auth_error to handle authentication errors",
        ):
            await base_client._handle_auth_error()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_success(
        self, mock_async_client, base_client
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

        result = await base_client.execute_http_get_request(
            url=url, headers=headers, params=params
        )

        assert result == mock_response
        mock_client_instance.get.assert_called_once_with(
            url, headers=headers, params=params
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_401_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with 401 error."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_success = False

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url)

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_401_error_with_auth_handler_exception(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with 401 error when auth handler raises exception."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_success = False

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        # Mock auth handler to raise an exception
        base_client._handle_auth_error = AsyncMock(side_effect=Exception("Auth failed"))

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url, max_retries=1)

        assert result is None
        base_client._handle_auth_error.assert_called_once()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_429_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with 429 error and retry."""
        # Mock response with 429
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.is_success = False
        mock_response.headers = {"Retry-After": "5"}

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await base_client.execute_http_get_request(
                url=url, max_retries=1, base_wait_time=1
            )

            assert result is None
            mock_sleep.assert_called()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_429_error_invalid_retry_after(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with 429 error and invalid Retry-After header."""
        # Mock response with 429 and invalid Retry-After
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.is_success = False
        mock_response.headers = {"Retry-After": "invalid"}

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await base_client.execute_http_get_request(
                url=url, max_retries=1, base_wait_time=1
            )

            assert result is None
            mock_sleep.assert_called()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_network_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with network error and retry."""
        # Mock async context manager that raises network error
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = Exception("Network error")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await base_client.execute_http_get_request(
                url=url, max_retries=2, base_wait_time=1
            )

            assert result is None
            assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_success_after_retry(
        self, mock_async_client, base_client
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
            result = await base_client.execute_http_get_request(
                url=url, max_retries=2, base_wait_time=1
            )

            assert result == mock_response
            mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_with_auth_error_handler(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with custom auth error handler."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_success = False

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        # Add custom auth error handler
        base_client._handle_auth_error = AsyncMock()

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url, max_retries=1)

        assert result is None
        base_client._handle_auth_error.assert_called_once()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_timeout(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with timeout."""
        # Mock async context manager that raises timeout
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = asyncio.TimeoutError("Request timeout")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await base_client.execute_http_get_request(
                url=url, max_retries=1, timeout=5
            )

            assert result is None
            mock_sleep.assert_called()

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_retry_limit_enforcement(
        self, mock_async_client, base_client
    ):
        """Test that max_retries is properly limited to 10."""
        # Mock response with 429 to trigger retries
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.is_success = False
        mock_response.headers = {"Retry-After": "1"}

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Try with max_retries=15, should be limited to 10
            result = await base_client.execute_http_get_request(
                url=url, max_retries=15, base_wait_time=1
            )

            assert result is None
            # Should have slept 10 times (limited by min(max_retries, 10))
            assert mock_sleep.call_count == 10

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_unknown_status_code(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with unknown status code."""
        # Mock response with unknown status code
        mock_response = MagicMock()
        mock_response.status_code = 999  # Unknown status code
        mock_response.is_success = False
        mock_response.text = "Unknown error"

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url, max_retries=1)

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_401_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with 401 error."""
        # Mock response with 401
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_success = False

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        json_data = {"test": "data"}

        result = await base_client.execute_http_post_request(
            url=url, json_data=json_data
        )

        assert result is None

    @given(credentials=sql_credentials_strategy)
    @settings(
        max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_initialization_with_various_credentials(self, credentials: Dict[str, Any]):
        """Property-based test for initialization with various credentials."""
        client = BaseClient(credentials=credentials)
        assert client.credentials == credentials

    def test_credentials_attribute_access(self, base_client):
        """Test that credentials attribute can be accessed and modified."""
        assert base_client.credentials == {}

        # Test setting credentials
        base_client.credentials = {"new": "credentials"}
        assert base_client.credentials == {"new": "credentials"}

    @pytest.mark.asyncio
    async def test_load_method_signature(self, base_client):
        """Test that load method accepts the correct parameters."""
        credentials = {"username": "test", "password": "secret"}

        # Should not raise TypeError for wrong parameters
        with pytest.raises(NotImplementedError):
            await base_client.load(credentials=credentials)

        # Test with additional kwargs
        with pytest.raises(NotImplementedError):
            await base_client.load(credentials=credentials, extra_param="value")
