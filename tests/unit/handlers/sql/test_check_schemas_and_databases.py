from unittest.mock import AsyncMock

import pytest

from application_sdk.handlers.sql import SQLHandler


class TestCheckSchemasAndDatabases:
    @pytest.fixture
    def handler(self) -> SQLHandler:
        handler = SQLHandler()
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"
        return handler

    @pytest.mark.asyncio
    async def test_successful_check(self, handler: SQLHandler) -> None:
        """Test successful schema and database check"""
        # Mock prepare_metadata to return test data
        handler.prepare_metadata = AsyncMock(
            return_value=[
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
            ]
        )

        payload = {"metadata": {"include-filter": '{"^db1$": ["^schema1$"]}'}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is True
        assert result["successMessage"] == "Schemas and Databases check successful"
        assert result["failureMessage"] == ""
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_database(self, handler: SQLHandler) -> None:
        """Test check with invalid database"""
        handler.prepare_metadata = AsyncMock(
            return_value=[{"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}]
        )

        payload = {"metadata": {"include-filter": '{"^invalid_db$": ["^schema1$"]}'}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is False
        assert result["successMessage"] == ""
        assert "invalid_db database" in result["failureMessage"]
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_schema(self, handler: SQLHandler) -> None:
        """Test check with invalid schema"""
        handler.prepare_metadata = AsyncMock(
            return_value=[{"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}]
        )

        payload = {"metadata": {"include-filter": '{"^db1$": ["^invalid_schema$"]}'}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is False
        assert result["successMessage"] == ""
        assert "db1.invalid_schema schema" in result["failureMessage"]
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_wildcard_schema(self, handler: SQLHandler) -> None:
        """Test check with wildcard schema"""
        handler.prepare_metadata = AsyncMock(
            return_value=[
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
            ]
        )

        payload = {"metadata": {"include-filter": '{"^db1$": "*"}'}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is True
        assert result["successMessage"] == "Schemas and Databases check successful"
        assert result["failureMessage"] == ""
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_metadata(self, handler: SQLHandler) -> None:
        """Test check with empty metadata"""
        handler.prepare_metadata = AsyncMock(return_value=[])

        payload = {"metadata": {}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is True
        assert result["successMessage"] == "Schemas and Databases check successful"
        assert result["failureMessage"] == ""
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_filter(self, handler: SQLHandler) -> None:
        """Test check with invalid JSON in include-filter"""
        handler.prepare_metadata = AsyncMock(return_value=[])

        payload = {"metadata": {"include-filter": "invalid json"}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is False
        assert result["successMessage"] == ""
        assert "Schemas and Databases check failed" in result["failureMessage"]
        assert "error" in result
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_metadata_error(self, handler: SQLHandler) -> None:
        """Test check when prepare_metadata raises an error"""
        handler.prepare_metadata = AsyncMock(side_effect=Exception("Database error"))

        payload = {"metadata": {"include-filter": "{}"}}
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is False
        assert result["successMessage"] == ""
        assert "Schemas and Databases check failed" in result["failureMessage"]
        assert result["error"] == "Database error"
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_databases_and_schemas(self, handler: SQLHandler) -> None:
        """Test check with multiple databases and schemas"""
        handler.prepare_metadata = AsyncMock(
            return_value=[
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
                {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
                {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"},
            ]
        )

        payload = {
            "metadata": {
                "include-filter": '{"^db1$": ["^schema1$", "^schema2$"], "^db2$": ["^schema1$"]}'
            }
        }
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is True
        assert result["successMessage"] == "Schemas and Databases check successful"
        assert result["failureMessage"] == ""
        handler.prepare_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_metadata_key(self, handler: SQLHandler) -> None:
        """Test check with missing metadata key in payload"""
        handler.prepare_metadata = AsyncMock(return_value=[])

        payload = {}  # Missing metadata key
        result = await handler.check_schemas_and_databases(payload)

        assert result["success"] is True  # Should default to empty filter
        assert result["successMessage"] == "Schemas and Databases check successful"
        assert result["failureMessage"] == ""
        handler.prepare_metadata.assert_called_once()
