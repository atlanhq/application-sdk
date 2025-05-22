from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
import pytest

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler


@pytest.fixture
def mock_sql_client() -> Mock:
    client = Mock(spec=BaseSQLClient)
    client.engine = Mock()
    return client


@pytest.fixture
def handler(mock_sql_client: Mock) -> BaseSQLHandler:
    handler = BaseSQLHandler(sql_client=mock_sql_client)
    handler.database_alias_key = "TABLE_CATALOG"
    handler.schema_alias_key = "TABLE_SCHEMA"
    handler.database_result_key = "TABLE_CATALOG"
    handler.schema_result_key = "TABLE_SCHEMA"
    handler.metadata_sql = "FILTER_METADATA"
    return handler


class TestCheckSchemasAndDatabases:
    @pytest.mark.asyncio
    async def test_successful_check(self, handler: BaseSQLHandler) -> None:
        """Test successful schema and database check"""
        # Test data
        test_data = pd.DataFrame(
            {"TABLE_CATALOG": ["db1", "db1"], "TABLE_SCHEMA": ["schema1", "schema2"]}
        )

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {"metadata": {"include-filter": '{"^db1$": ["^schema1$"]}'}}
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is True
            assert result["successMessage"] == "Schemas and Databases check successful"
            assert result["failureMessage"] == ""
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_database(self, handler: BaseSQLHandler) -> None:
        """Test check with invalid database"""
        # Test data
        test_data = pd.DataFrame(
            {"TABLE_CATALOG": ["db1"], "TABLE_SCHEMA": ["schema1"]}
        )

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {
                "metadata": {"include-filter": '{"^invalid_db$": ["^schema1$"]}'}
            }
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is False
            assert result["successMessage"] == ""
            assert "invalid_db database" in result["failureMessage"]
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_schema(self, handler: BaseSQLHandler) -> None:
        """Test check with invalid schema"""
        # Test data
        test_data = pd.DataFrame(
            {"TABLE_CATALOG": ["db1"], "TABLE_SCHEMA": ["schema1"]}
        )

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {
                "metadata": {"include-filter": '{"^db1$": ["^invalid_schema$"]}'}
            }
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is False
            assert result["successMessage"] == ""
            assert "db1.invalid_schema schema" in result["failureMessage"]
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_wildcard_schema(self, handler: BaseSQLHandler) -> None:
        """Test check with wildcard schema"""
        # Test data
        test_data = pd.DataFrame(
            {"TABLE_CATALOG": ["db1", "db1"], "TABLE_SCHEMA": ["schema1", "schema2"]}
        )

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {"metadata": {"include-filter": '{"^db1$": "*"}'}}
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is True
            assert result["successMessage"] == "Schemas and Databases check successful"
            assert result["failureMessage"] == ""
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_metadata(self, handler: BaseSQLHandler) -> None:
        """Test check with empty metadata"""
        # Test data - empty DataFrame
        test_data = pd.DataFrame({"TABLE_CATALOG": [], "TABLE_SCHEMA": []})

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {"metadata": {}}
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is True
            assert result["successMessage"] == "Schemas and Databases check successful"
            assert result["failureMessage"] == ""
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_filter(self, handler: BaseSQLHandler) -> None:
        """Test check with invalid JSON in include-filter"""
        # Test data
        test_data = pd.DataFrame({"TABLE_CATALOG": [], "TABLE_SCHEMA": []})

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {"metadata": {"include-filter": "invalid json"}}
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is False
            assert result["successMessage"] == ""
            assert "Schemas and Databases check failed" in result["failureMessage"]
            assert "error" in result
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_metadata_error(self, handler: BaseSQLHandler) -> None:
        """Test check when prepare_metadata raises an error"""
        # Mock the SQLQueryInput.get_dataframe to raise an exception
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
            side_effect=Exception("Database error"),
        ) as mock_get_dataframe:
            payload = {"metadata": {"include-filter": "{}"}}
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is False
            assert result["successMessage"] == ""
            assert "Schemas and Databases check failed" in result["failureMessage"]
            assert result["error"] == "Database error"
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_databases_and_schemas(
        self, handler: BaseSQLHandler
    ) -> None:
        """Test check with multiple databases and schemas"""
        # Test data
        test_data = pd.DataFrame(
            {
                "TABLE_CATALOG": ["db1", "db1", "db2"],
                "TABLE_SCHEMA": ["schema1", "schema2", "schema1"],
            }
        )

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {
                "metadata": {
                    "include-filter": '{"^db1$": ["^schema1$", "^schema2$"], "^db2$": ["^schema1$"]}'
                }
            }
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is True
            assert result["successMessage"] == "Schemas and Databases check successful"
            assert result["failureMessage"] == ""
            mock_get_dataframe.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_metadata_key(self, handler: BaseSQLHandler) -> None:
        """Test check with missing metadata key in payload"""
        # Test data - empty DataFrame
        test_data = pd.DataFrame({"TABLE_CATALOG": [], "TABLE_SCHEMA": []})

        # Mock the SQLQueryInput.get_daft_dataframe to return our test data
        with patch(
            "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe",
            new_callable=AsyncMock,
        ) as mock_get_dataframe:
            mock_get_dataframe.return_value = test_data

            payload = {}  # Missing metadata key
            result = await handler.check_schemas_and_databases(payload)

            assert result["success"] is True  # Should default to empty filter
            assert result["successMessage"] == "Schemas and Databases check successful"
            assert result["failureMessage"] == ""
            mock_get_dataframe.assert_called_once()
