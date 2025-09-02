from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import Headers
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
        assert client.http_headers == {}
        assert client.http_retry_transport is not None

    def test_initialization_with_credentials(self):
        """Test BaseClient initialization with credentials."""
        credentials = {"username": "test", "password": "secret"}
        client = BaseClient(credentials=credentials)
        assert client.credentials == credentials

    def test_initialization_with_http_headers(self):
        """Test BaseClient initialization with HTTP headers."""
        headers = {"Authorization": "Bearer token", "User-Agent": "TestApp/1.0"}
        client = BaseClient(http_headers=headers)
        assert client.http_headers == headers

    @pytest.mark.asyncio
    async def test_load_not_implemented(self, base_client):
        """Test that load method raises NotImplementedError."""
        credentials = {"username": "test", "password": "secret"}

        with pytest.raises(NotImplementedError, match="load method is not implemented"):
            await base_client.load(credentials=credentials)

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
        # Verify that headers were merged correctly
        expected_headers = Headers(base_client.http_headers)
        expected_headers.update(headers)
        mock_client_instance.get.assert_called_once_with(
            url, headers=expected_headers, params=params, auth=httpx.USE_CLIENT_DEFAULT
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_with_client_headers(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with client-level headers."""
        # Set client-level headers
        base_client.http_headers = {
            "User-Agent": "TestApp/1.0",
            "Accept": "application/json",
        }

        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        method_headers = {"Authorization": "Bearer token"}

        result = await base_client.execute_http_get_request(
            url=url, headers=method_headers
        )

        assert result == mock_response
        # Verify that headers were merged correctly
        expected_headers = Headers(base_client.http_headers)
        expected_headers.update(method_headers)
        mock_client_instance.get.assert_called_once_with(
            url, headers=expected_headers, params=None, auth=httpx.USE_CLIENT_DEFAULT
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_http_status_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with HTTP status error."""
        # Mock async context manager that raises HTTPStatusError
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=MagicMock(status_code=401)
        )
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url)

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_general_exception(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with general exception."""
        # Mock async context manager that raises general exception
        mock_client_instance = AsyncMock()
        mock_client_instance.get.side_effect = Exception("Network error")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await base_client.execute_http_get_request(url=url)

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_get_request_with_auth(
        self, mock_async_client, base_client
    ):
        """Test HTTP GET request with authentication."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        auth = ("username", "password")

        result = await base_client.execute_http_get_request(url=url, auth=auth)

        assert result == mock_response
        mock_client_instance.get.assert_called_once_with(
            url, headers=Headers(base_client.http_headers), params=None, auth=auth
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_success(
        self, mock_async_client, base_client
    ):
        """Test successful HTTP POST request execution."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        json_data = {"test": "data"}
        headers = {"Content-Type": "application/json"}

        result = await base_client.execute_http_post_request(
            url=url, json_data=json_data, headers=headers
        )

        assert result == mock_response
        # Verify that headers were merged correctly
        expected_headers = Headers(base_client.http_headers)
        expected_headers.update(headers)
        mock_client_instance.post.assert_called_once_with(
            url,
            data=None,
            json=json_data,
            content=None,
            files=None,
            headers=expected_headers,
            params=None,
            cookies=None,
            auth=httpx.USE_CLIENT_DEFAULT,
            follow_redirects=True,
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_with_files(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with file upload."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/upload"
        data = {"description": "Test file"}
        files = {"file": ("test.txt", b"file content", "text/plain")}

        result = await base_client.execute_http_post_request(
            url=url, data=data, files=files
        )

        assert result == mock_response
        mock_client_instance.post.assert_called_once_with(
            url,
            data=data,
            json=None,
            content=None,
            files=files,
            headers=Headers(base_client.http_headers),
            params=None,
            cookies=None,
            auth=httpx.USE_CLIENT_DEFAULT,
            follow_redirects=True,
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_http_status_error(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with HTTP status error."""
        # Mock async context manager that raises HTTPStatusError
        mock_client_instance = AsyncMock()
        mock_client_instance.post.side_effect = httpx.HTTPStatusError(
            "HTTP error", request=MagicMock(), response=MagicMock(status_code=500)
        )
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        json_data = {"test": "data"}

        result = await base_client.execute_http_post_request(
            url=url, json_data=json_data
        )

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_general_exception(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with general exception."""
        # Mock async context manager that raises general exception
        mock_client_instance = AsyncMock()
        mock_client_instance.post.side_effect = Exception("Network error")
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        json_data = {"test": "data"}

        result = await base_client.execute_http_post_request(
            url=url, json_data=json_data
        )

        assert result is None

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_with_custom_timeout(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with custom timeout."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"
        timeout = 60

        result = await base_client.execute_http_post_request(url=url, timeout=timeout)

        assert result == mock_response
        # Verify that timeout was passed to AsyncClient
        mock_async_client.assert_called_once_with(
            timeout=timeout, transport=base_client.http_retry_transport, verify=True
        )

    @pytest.mark.asyncio
    @patch("application_sdk.clients.base.httpx.AsyncClient")
    async def test_execute_http_post_request_with_verify_false(
        self, mock_async_client, base_client
    ):
        """Test HTTP POST request with SSL verification disabled."""
        # Mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock async context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.post.return_value = mock_response
        mock_async_client.return_value.__aenter__.return_value = mock_client_instance

        url = "https://api.example.com/test"

        result = await base_client.execute_http_post_request(url=url, verify=False)

        assert result == mock_response
        # Verify that verify=False was passed to AsyncClient
        mock_async_client.assert_called_once_with(
            timeout=30, transport=base_client.http_retry_transport, verify=False
        )

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

    def test_http_headers_attribute_access(self, base_client):
        """Test that http_headers attribute can be accessed and modified."""
        assert base_client.http_headers == {}

        # Test setting headers
        base_client.http_headers = {"Authorization": "Bearer token"}
        assert base_client.http_headers == {"Authorization": "Bearer token"}

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

    def test_http_retry_transport_initialization(self, base_client):
        """Test that http_retry_transport is properly initialized."""
        assert base_client.http_retry_transport is not None
        # Should be an instance of httpx.AsyncHTTPTransport by default
        assert isinstance(base_client.http_retry_transport, httpx.AsyncHTTPTransport)
