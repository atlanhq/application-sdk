from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
import pytest

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler


class TestPrepareMetadata:
    @pytest.fixture
    def mock_sql_client(self) -> Mock:
        client = Mock(spec=BaseSQLClient)
        client.engine = Mock()
        return client

    @pytest.fixture
    def handler(self, mock_sql_client: Mock) -> BaseSQLHandler:
        handler = BaseSQLHandler(sql_client=mock_sql_client)
        handler.database_alias_key = "TABLE_CATALOG"
        handler.schema_alias_key = "TABLE_SCHEMA"
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"
        handler.metadata_sql = "FILTER_METADATA"
        return handler

    @pytest.mark.asyncio
    async def test_successful_metadata_preparation(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test successful metadata preparation with valid input"""
        # Create test DataFrame
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1", "db1", "db2"],
                "TABLE_SCHEMA": ["schema1", "schema2", "schema1"],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 3
            assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
            assert result[1] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"}
            assert result[2] == {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"}
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_dataframe(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with empty DataFrame"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": [],
                "TABLE_SCHEMA": [],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 0
            assert isinstance(result, list)
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_alias_keys(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with custom alias keys"""
        handler.database_alias_key = "DB_NAME"
        handler.schema_alias_key = "SCHEMA_NAME"

        df = pd.DataFrame({"DB_NAME": ["db1"], "SCHEMA_NAME": ["schema1"]})

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 1
            assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_result_keys(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with custom result keys"""
        handler.database_result_key = "DATABASE"
        handler.schema_result_key = "SCHEMA"

        df = pd.DataFrame({"TABLE_CATALOG": ["db1"], "TABLE_SCHEMA": ["schema1"]})

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 1
            assert result[0] == {"DATABASE": "db1", "SCHEMA": "schema1"}
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_columns(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with missing required columns"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1"]  # Missing TABLE_SCHEMA column
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            with pytest.raises(KeyError) as exc_info:
                await handler.prepare_metadata()
            assert "TABLE_SCHEMA" in str(exc_info.value)
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_null_values(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with null values"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1", None, "db2"],
                "TABLE_SCHEMA": ["schema1", "schema2", None],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 3
            assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
            assert result[1] == {"TABLE_CATALOG": None, "TABLE_SCHEMA": "schema2"}
            assert result[2] == {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": None}
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_special_characters(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with special characters in names"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db-1", "db.2", "db@3"],
                "TABLE_SCHEMA": ["schema-1", "schema.2", "schema@3"],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 3
            assert result[0] == {"TABLE_CATALOG": "db-1", "TABLE_SCHEMA": "schema-1"}
            assert result[1] == {"TABLE_CATALOG": "db.2", "TABLE_SCHEMA": "schema.2"}
            assert result[2] == {"TABLE_CATALOG": "db@3", "TABLE_SCHEMA": "schema@3"}
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_entries(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with duplicate entries"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1", "db1", "db1"],
                "TABLE_SCHEMA": ["schema1", "schema1", "schema1"],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert (
                len(result) == 3
            )  # Should preserve duplicates as they might be meaningful
            assert all(
                entry == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
                for entry in result
            )
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_dataframe(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with invalid DataFrame input"""
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = None
            with pytest.raises(Exception):
                await handler.prepare_metadata()
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_extra_columns(self, handler: BaseSQLHandler) -> None:
        """Test metadata preparation with extra columns (should be ignored)"""
        df = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1"],
                "TABLE_SCHEMA": ["schema1"],
                "EXTRA_COLUMN": ["extra"],
            }
        )

        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = df
            result = await handler.prepare_metadata()

            assert len(result) == 1
            assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
            assert "EXTRA_COLUMN" not in result[0]
            mock_get_dataframe.assert_called_once()
