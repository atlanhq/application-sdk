from unittest.mock import Mock

import pandas as pd
import pytest

from application_sdk.clients.sql import SQLClient
from application_sdk.handlers.sql import SQLHandler


class TestPrepareMetadata:
    @pytest.fixture
    def mock_sql_client(self) -> Mock:
        client = Mock(spec=SQLClient)
        client.engine = Mock()
        return client

    @pytest.fixture
    def handler(self, mock_sql_client: Mock) -> SQLHandler:
        handler = SQLHandler(sql_client=mock_sql_client)
        handler.database_alias_key = "TABLE_CATALOG"
        handler.schema_alias_key = "TABLE_SCHEMA"
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"
        return handler

    @pytest.mark.asyncio
    async def test_successful_metadata_preparation(self, handler: SQLHandler) -> None:
        """Test successful metadata preparation with valid input"""
        # Create test DataFrame
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1", "db1", "db2"],
            "TABLE_SCHEMA": ["schema1", "schema2", "schema1"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 3
        assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        assert result[1] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"}
        assert result[2] == {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema1"}

    @pytest.mark.asyncio
    async def test_empty_dataframe(self, handler: SQLHandler) -> None:
        """Test metadata preparation with empty DataFrame"""
        df = pd.DataFrame(columns=["TABLE_CATALOG", "TABLE_SCHEMA"])

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 0
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_custom_alias_keys(self, handler: SQLHandler) -> None:
        """Test metadata preparation with custom alias keys"""
        handler.database_alias_key = "DB_NAME"
        handler.schema_alias_key = "SCHEMA_NAME"

        df = pd.DataFrame({
            "DB_NAME": ["db1"],
            "SCHEMA_NAME": ["schema1"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 1
        assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}

    @pytest.mark.asyncio
    async def test_custom_result_keys(self, handler: SQLHandler) -> None:
        """Test metadata preparation with custom result keys"""
        handler.database_result_key = "DATABASE"
        handler.schema_result_key = "SCHEMA"

        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1"],
            "TABLE_SCHEMA": ["schema1"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 1
        assert result[0] == {"DATABASE": "db1", "SCHEMA": "schema1"}

    @pytest.mark.asyncio
    async def test_missing_columns(self, handler: SQLHandler) -> None:
        """Test metadata preparation with missing required columns"""
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1"]  # Missing TABLE_SCHEMA column
        })

        with pytest.raises(KeyError) as exc_info:
            await handler.prepare_metadata({"sql_input": df})
        assert "TABLE_SCHEMA" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_null_values(self, handler: SQLHandler) -> None:
        """Test metadata preparation with null values"""
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1", None, "db2"],
            "TABLE_SCHEMA": ["schema1", "schema2", None]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 3
        assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        assert result[1] == {"TABLE_CATALOG": None, "TABLE_SCHEMA": "schema2"}
        assert result[2] == {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": None}

    @pytest.mark.asyncio
    async def test_special_characters(self, handler: SQLHandler) -> None:
        """Test metadata preparation with special characters in names"""
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db-1", "db.2", "db@3"],
            "TABLE_SCHEMA": ["schema-1", "schema.2", "schema@3"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 3
        assert result[0] == {"TABLE_CATALOG": "db-1", "TABLE_SCHEMA": "schema-1"}
        assert result[1] == {"TABLE_CATALOG": "db.2", "TABLE_SCHEMA": "schema.2"}
        assert result[2] == {"TABLE_CATALOG": "db@3", "TABLE_SCHEMA": "schema@3"}

    @pytest.mark.asyncio
    async def test_duplicate_entries(self, handler: SQLHandler) -> None:
        """Test metadata preparation with duplicate entries"""
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1", "db1", "db1"],
            "TABLE_SCHEMA": ["schema1", "schema1", "schema1"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 3  # Should preserve duplicates as they might be meaningful
        assert all(entry == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"} 
                  for entry in result)

    @pytest.mark.asyncio
    async def test_invalid_dataframe(self, handler: SQLHandler) -> None:
        """Test metadata preparation with invalid DataFrame input"""
        with pytest.raises(Exception):
            await handler.prepare_metadata({"sql_input": None})  # type: ignore

    @pytest.mark.asyncio
    async def test_extra_columns(self, handler: SQLHandler) -> None:
        """Test metadata preparation with extra columns (should be ignored)"""
        df = pd.DataFrame({
            "TABLE_CATALOG": ["db1"],
            "TABLE_SCHEMA": ["schema1"],
            "EXTRA_COLUMN": ["extra"]
        })

        result = await handler.prepare_metadata({"sql_input": df})

        assert len(result) == 1
        assert result[0] == {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        assert "EXTRA_COLUMN" not in result[0]
