from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler


class TestAuthenticationHandler:
    @pytest.fixture
    def mock_sql_client(self) -> Mock:
        client = Mock(spec=BaseSQLClient)
        client.engine = Mock()
        return client

    @pytest.fixture
    def handler(self, mock_sql_client: Mock) -> BaseSQLHandler:
        handler = BaseSQLHandler(sql_client=mock_sql_client)
        handler.test_authentication_sql = "SELECT 1;"
        return handler

    @pytest.mark.asyncio
    async def test_successful_authentication(self, handler: BaseSQLHandler) -> None:
        """Test successful authentication with valid credentials"""
        # Mock a successful DataFrame response
        mock_df = pd.DataFrame({"result": [1]})

        # Mock the sql_client.get_results method directly
        handler.sql_client.get_results = AsyncMock(return_value=mock_df)

        # Test authentication
        result = await handler.test_auth()

        # Verify success
        assert result is True
        handler.sql_client.get_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_authentication(self, handler: BaseSQLHandler) -> None:
        """Test failed authentication with invalid credentials"""
        # Mock the sql_client.get_results method to raise an exception
        handler.sql_client.get_results = AsyncMock(
            side_effect=Exception("Authentication failed")
        )

        # Test authentication and expect exception
        with pytest.raises(Exception) as exc_info:
            await handler.test_auth()

        # Verify error
        assert str(exc_info.value) == "Authentication failed"
        handler.sql_client.get_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_dataframe_authentication(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test authentication with empty DataFrame response"""
        # Mock the sql_client.get_results method directly
        mock_df = pd.DataFrame({})
        handler.sql_client.get_results = AsyncMock(return_value=mock_df)

        # Test authentication should still succeed as DataFrame is valid
        result = await handler.test_auth()

        # Verify success
        assert result is True
        handler.sql_client.get_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_dataframe_authentication(self, handler: BaseSQLHandler) -> None:
        """Test authentication with None DataFrame response"""
        # Mock the sql_client.get_results method to return None
        handler.sql_client.get_results = AsyncMock(return_value=None)

        # Test authentication and expect exception
        with pytest.raises(AttributeError) as exc_info:
            await handler.test_auth()

        # Verify error and call
        assert "object has no attribute 'to_dict'" in str(exc_info.value)
        handler.sql_client.get_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_malformed_dataframe_authentication(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test authentication with malformed DataFrame that raises on to_dict"""
        # Create a mock DataFrame that raises on to_dict
        mock_df = Mock(spec=pd.DataFrame)
        mock_df.to_dict.side_effect = Exception("DataFrame conversion error")
        handler.sql_client.get_results = AsyncMock(return_value=mock_df)

        # Test authentication and expect exception
        with pytest.raises(Exception) as exc_info:
            await handler.test_auth()

        # Verify error and call
        assert str(exc_info.value) == "DataFrame conversion error"
        handler.sql_client.get_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_sql_query(self, handler: BaseSQLHandler) -> None:
        """Test authentication with custom SQL query"""
        # Set custom test query
        handler.test_authentication_sql = "SELECT version();"
        mock_df = pd.DataFrame({"version": ["test_version"]})
        handler.sql_client.get_results = AsyncMock(return_value=mock_df)

        # Test authentication
        result = await handler.test_auth()

        # Verify success and correct query was used
        assert result is True
        handler.sql_client.get_results.assert_called_once()
        assert handler.test_authentication_sql == "SELECT version();"
