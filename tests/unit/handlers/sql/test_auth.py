from unittest.mock import AsyncMock, Mock, patch

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

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = mock_df

            # Test authentication
            result = await handler.test_auth()

            # Verify success
            assert result is True
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_authentication(self, handler: BaseSQLHandler) -> None:
        """Test failed authentication with invalid credentials"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            # Mock a failed response
            mock_get_dataframe.side_effect = Exception("Authentication failed")

            # Test authentication and expect exception
            with pytest.raises(Exception) as exc_info:
                await handler.test_auth()

            # Verify error
            assert str(exc_info.value) == "Authentication failed"
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_dataframe_authentication(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test authentication with empty DataFrame response"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            # Mock an empty DataFrame
            mock_df = pd.DataFrame({})
            mock_get_dataframe.return_value = mock_df

            # Test authentication should still succeed as DataFrame is valid
            result = await handler.test_auth()

            # Verify success
            assert result is True
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_none_dataframe_authentication(self, handler: BaseSQLHandler) -> None:
        """Test authentication with None DataFrame response"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            # Mock None response
            mock_get_dataframe.return_value = None

            # Test authentication and expect exception
            with pytest.raises(AttributeError) as exc_info:
                await handler.test_auth()

            # Verify error and call
            assert "object has no attribute 'to_dict'" in str(exc_info.value)
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_malformed_dataframe_authentication(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test authentication with malformed DataFrame that raises on to_dict"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            # Create a mock DataFrame that raises on to_dict
            mock_df = Mock(spec=pd.DataFrame)
            mock_df.to_dict.side_effect = Exception("DataFrame conversion error")
            mock_get_dataframe.return_value = mock_df

            # Test authentication and expect exception
            with pytest.raises(Exception) as exc_info:
                await handler.test_auth()

            # Verify error and call
            assert str(exc_info.value) == "DataFrame conversion error"
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_sql_query(self, handler: BaseSQLHandler) -> None:
        """Test authentication with custom SQL query"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            # Set custom test query
            handler.test_authentication_sql = "SELECT version();"
            mock_df = pd.DataFrame({"version": ["test_version"]})
            mock_get_dataframe.return_value = mock_df

            # Test authentication
            result = await handler.test_auth()

            # Verify success and correct query was used
            assert result is True
            mock_get_dataframe.assert_called_once()
            assert handler.test_authentication_sql == "SELECT version();"
