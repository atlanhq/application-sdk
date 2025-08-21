"""Unit tests for BaseHandler class."""

import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.base import BaseClient
from application_sdk.handlers.base import BaseHandler
from application_sdk.test_utils.hypothesis.strategies.clients.sql import (
    sql_credentials_strategy,
)


@pytest.fixture
def mock_base_client():
    """Create a mock BaseClient for testing."""
    client = MagicMock(spec=BaseClient)
    client.load = AsyncMock()
    return client


class ConcreteBaseHandler(BaseHandler):
    """Concrete implementation of BaseHandler for testing."""

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        """Mock test auth method."""
        return True

    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock preflight check method."""
        return {"status": "success"}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Mock fetch metadata method."""
        return {"metadata": "test"}


@pytest.fixture
def base_handler(mock_base_client):
    """Create a BaseHandler instance for testing."""
    return ConcreteBaseHandler(client=mock_base_client)


@pytest.fixture
def base_handler_default():
    """Create a BaseHandler instance with default client."""
    return ConcreteBaseHandler()


class TestBaseHandler:
    """Test cases for BaseHandler."""

    def test_initialization_with_client(self, mock_base_client):
        """Test BaseHandler initialization with provided client."""
        handler = ConcreteBaseHandler(client=mock_base_client)
        assert handler.client == mock_base_client

    def test_initialization_without_client(self):
        """Test BaseHandler initialization without client (uses default)."""
        handler = ConcreteBaseHandler()
        assert isinstance(handler.client, BaseClient)

    def test_initialization_with_none_client(self):
        """Test BaseHandler initialization with None client (uses default)."""
        handler = ConcreteBaseHandler(client=None)
        assert isinstance(handler.client, BaseClient)

    @pytest.mark.asyncio
    async def test_load_success(self, base_handler, mock_base_client):
        """Test successful load method execution."""
        credentials = {"username": "test", "password": "secret"}

        await base_handler.load(credentials=credentials)

        mock_base_client.load.assert_called_once_with(credentials=credentials)

    @pytest.mark.asyncio
    async def test_load_with_client_error(self, base_handler, mock_base_client):
        """Test load method when client raises an error."""
        credentials = {"username": "test", "password": "secret"}
        mock_base_client.load.side_effect = Exception("Client error")

        with pytest.raises(Exception, match="Client error"):
            await base_handler.load(credentials=credentials)

    @pytest.mark.asyncio
    async def test_load_logging(self, base_handler, mock_base_client):
        """Test that load method logs appropriately."""
        credentials = {"username": "test", "password": "secret"}

        with patch("application_sdk.handlers.base.logger") as mock_logger:
            await base_handler.load(credentials=credentials)

            # Check that info logs were called
            assert mock_logger.info.call_count == 2
            mock_logger.info.assert_any_call("Loading base handler")
            mock_logger.info.assert_any_call("Base handler loaded successfully")

    @pytest.mark.asyncio
    async def test_load_with_default_client(self, base_handler_default):
        """Test load method with default client."""
        credentials = {"username": "test", "password": "secret"}

        # Default client should raise NotImplementedError
        with pytest.raises(NotImplementedError, match="load method is not implemented"):
            await base_handler_default.load(credentials=credentials)

    @pytest.mark.asyncio
    async def test_load_with_empty_credentials(self, base_handler, mock_base_client):
        """Test load method with empty credentials."""
        credentials = {}

        await base_handler.load(credentials=credentials)

        mock_base_client.load.assert_called_once_with(credentials=credentials)

    @pytest.mark.asyncio
    async def test_load_with_complex_credentials(self, base_handler, mock_base_client):
        """Test load method with complex credential structure."""
        credentials = {
            "username": "test_user",
            "password": "test_pass",
            "api_key": "test_key",
            "extra": {"timeout": 30, "retry_count": 3},
        }

        await base_handler.load(credentials=credentials)

        mock_base_client.load.assert_called_once_with(credentials=credentials)

    @given(credentials=sql_credentials_strategy)
    @settings(
        max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @pytest.mark.asyncio
    async def test_load_with_various_credentials(self, credentials: Dict[str, Any]):
        """Property-based test for load method with various credentials."""
        mock_client = MagicMock(spec=BaseClient)
        mock_client.load = AsyncMock()
        handler = ConcreteBaseHandler(client=mock_client)

        await handler.load(credentials=credentials)

        mock_client.load.assert_called_once_with(credentials=credentials)

    def test_client_attribute_access(self, base_handler, mock_base_client):
        """Test that client attribute can be accessed and modified."""
        assert base_handler.client == mock_base_client

        # Test setting new client
        new_client = MagicMock(spec=BaseClient)
        base_handler.client = new_client
        assert base_handler.client == new_client

    @pytest.mark.asyncio
    async def test_load_method_signature(self, base_handler, mock_base_client):
        """Test that load method accepts the correct parameters."""
        credentials = {"username": "test", "password": "secret"}

        # Should not raise TypeError for correct parameters
        await base_handler.load(credentials=credentials)

        # Check that client.load was called with credentials
        mock_base_client.load.assert_called_once_with(credentials=credentials)

    def test_handler_interface_compliance(self, base_handler):
        """Test that BaseHandler properly implements HandlerInterface."""
        from application_sdk.handlers import HandlerInterface

        # Check inheritance
        assert isinstance(base_handler, HandlerInterface)

        # Check that required methods exist (even if they're abstract)
        assert hasattr(base_handler, "test_auth")
        assert hasattr(base_handler, "preflight_check")
        assert hasattr(base_handler, "fetch_metadata")
        assert hasattr(base_handler, "load")

    @pytest.mark.asyncio
    async def test_load_with_async_client_error(self, base_handler, mock_base_client):
        """Test load method with async client error."""
        credentials = {"username": "test", "password": "secret"}
        mock_base_client.load.side_effect = asyncio.TimeoutError("Client timeout")

        with pytest.raises(asyncio.TimeoutError, match="Client timeout"):
            await base_handler.load(credentials=credentials)

    def test_initialization_with_custom_client_subclass(self):
        """Test initialization with a custom client subclass."""

        class CustomClient(BaseClient):
            async def load(self, credentials: Dict[str, Any]) -> None:
                pass

        custom_client = CustomClient()
        handler = ConcreteBaseHandler(client=custom_client)

        assert handler.client == custom_client
        assert isinstance(handler.client, CustomClient)
        assert isinstance(handler.client, BaseClient)
