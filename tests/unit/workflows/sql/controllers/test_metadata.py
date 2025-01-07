from typing import Dict, Generic, List, TypeVar
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from application_sdk.app.rest.fastapi.models.workflow import MetadataType
from application_sdk.workflows.sql.controllers.metadata import (
    SQLWorkflowMetadataController,
)
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

T = TypeVar("T")


class AsyncIteratorMock(Generic[T]):
    """Helper class to mock async iterators"""

    def __init__(self, items: List[T]) -> None:
        self.items = items.copy()  # Create a copy to avoid modifying original list

    def __aiter__(self) -> "AsyncIteratorMock[T]":
        return self

    async def __anext__(self) -> T:
        try:
            return self.items.pop(0)
        except IndexError:
            raise StopAsyncIteration


@pytest.fixture
def mock_sql_resource() -> MagicMock:
    resource = MagicMock(spec=SQLResource)
    resource.run_query = MagicMock()  # Use regular MagicMock instead of AsyncMock
    resource.fetch_metadata = AsyncMock()
    return resource


class TestSQLWorkflowMetadataController:
    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode(self, mock_sql_resource: MagicMock) -> None:
        """Test fetch_metadata when hierarchical fetching is disabled (MetadataType.ALL)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.METADATA_SQL = "SELECT * FROM test"
        controller.DATABASE_ALIAS_KEY = "db_alias"
        controller.SCHEMA_ALIAS_KEY = "schema_alias"
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        expected_result: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        mock_sql_resource.fetch_metadata.return_value = expected_result

        # Execute with MetadataType.ALL
        result = await controller.fetch_metadata(metadata_type=MetadataType.ALL)

        # Assert
        assert result == expected_result
        # Verify fetch_metadata was called with correct arguments
        mock_sql_resource.fetch_metadata.assert_called_once_with(
            {
                "metadata_sql": "SELECT * FROM test",
                "database_alias_key": "db_alias",
                "schema_alias_key": "schema_alias",
                "database_result_key": "TABLE_CATALOG",
                "schema_result_key": "TABLE_SCHEMA",
            }
        )
        # Verify run_query was not called
        mock_sql_resource.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_hierarchical_mode(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata when hierarchical fetching is enabled (direct method calls)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        # Mock database query results
        mock_sql_resource.run_query.side_effect = [
            AsyncIteratorMock([[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]),
            AsyncIteratorMock(
                [[{"TABLE_SCHEMA": "schema1"}, {"TABLE_SCHEMA": "schema2"}]]
            ),
            AsyncIteratorMock([[{"TABLE_SCHEMA": "schema3"}]]),
        ]

        # First fetch databases directly
        databases = await controller.fetch_databases()
        assert databases == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]

        # Then fetch schemas for each database directly
        schemas_db1 = await controller.fetch_schemas("db1")
        assert schemas_db1 == [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema2"},
        ]

        schemas_db2 = await controller.fetch_schemas("db2")
        assert schemas_db2 == [
            {"TABLE_CATALOG": "db2", "TABLE_SCHEMA": "schema3"},
        ]

        # Assert the calls were made correctly
        expected_calls = [
            call("SELECT database_name FROM databases"),
            call("SELECT schema_name FROM schemas WHERE database = 'db1'"),
            call("SELECT schema_name FROM schemas WHERE database = 'db2'"),
        ]
        assert mock_sql_resource.run_query.call_count == 3
        mock_sql_resource.run_query.assert_has_calls(expected_calls, any_order=False)
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_database_type(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching only databases using MetadataType.DATABASE"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.DATABASE_KEY = "TABLE_CATALOG"

        # Mock database query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Execute with MetadataType.DATABASE
        result = await controller.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )
        assert mock_sql_resource.run_query.call_count == 1
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_type(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching schemas using MetadataType.SCHEMA"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        # Mock schema query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_SCHEMA": "schema1"}, {"TABLE_SCHEMA": "schema2"}]]
        )

        test_database = "test_db"
        # Execute with MetadataType.SCHEMA
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database=test_database
        )

        # Assert
        assert result == [
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema2"},
        ]
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT schema_name FROM schemas WHERE database = 'test_db'"
        )
        assert mock_sql_resource.run_query.call_count == 1
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_invalid_type(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata with invalid/None metadata type"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)

        # Execute and Assert with None type
        with pytest.raises(ValueError, match="Invalid metadata type: None"):
            await controller.fetch_metadata()

        # Verify neither method was called
        mock_sql_resource.run_query.assert_not_called()
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_without_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching schemas without database using MetadataType.SCHEMA"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)

        # Execute and Assert with MetadataType.SCHEMA but no database
        with pytest.raises(
            ValueError, match="Database must be specified when fetching schemas"
        ):
            await controller.fetch_metadata(metadata_type=MetadataType.SCHEMA)

        # Verify neither method was called
        mock_sql_resource.run_query.assert_not_called()
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_empty_databases(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching empty databases using MetadataType.DATABASE"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"

        # Mock empty database result
        mock_sql_resource.run_query.return_value = AsyncIteratorMock([[]])

        # Execute with MetadataType.DATABASE
        result = await controller.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == []
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )
        assert mock_sql_resource.run_query.call_count == 1
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_empty_schemas(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching empty schemas using MetadataType.SCHEMA"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        # Mock empty schema result
        mock_sql_resource.run_query.return_value = AsyncIteratorMock([[]])

        # Execute with MetadataType.SCHEMA
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database="test_db"
        )

        # Assert
        assert result == []
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT schema_name FROM schemas WHERE database = 'test_db'"
        )
        assert mock_sql_resource.run_query.call_count == 1
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_error_handling(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test error handling in metadata fetching"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"

        # Mock query to raise an exception
        class ErrorAsyncIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise Exception("Database query failed")

        mock_sql_resource.run_query.return_value = ErrorAsyncIterator()

        # Execute and Assert with MetadataType.DATABASE
        with pytest.raises(Exception) as exc_info:
            await controller.fetch_metadata(metadata_type=MetadataType.DATABASE)
        assert str(exc_info.value) == "Database query failed"

        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )
        assert mock_sql_resource.run_query.call_count == 1
        # Verify fetch_metadata was not called
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode_without_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata with MetadataType.ALL and no database"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.METADATA_SQL = "SELECT * FROM test"
        controller.DATABASE_ALIAS_KEY = "db_alias"
        controller.SCHEMA_ALIAS_KEY = "schema_alias"
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        expected_result: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        mock_sql_resource.fetch_metadata.return_value = expected_result

        # Execute with MetadataType.ALL and no database
        result = await controller.fetch_metadata(metadata_type=MetadataType.ALL)

        # Assert
        assert result == expected_result
        mock_sql_resource.fetch_metadata.assert_called_once_with(
            {
                "metadata_sql": "SELECT * FROM test",
                "database_alias_key": "db_alias",
                "schema_alias_key": "schema_alias",
                "database_result_key": "TABLE_CATALOG",
                "schema_result_key": "TABLE_SCHEMA",
            }
        )
        mock_sql_resource.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode_with_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata with MetadataType.ALL and database (should ignore database)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.METADATA_SQL = "SELECT * FROM test"
        controller.DATABASE_ALIAS_KEY = "db_alias"
        controller.SCHEMA_ALIAS_KEY = "schema_alias"
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        expected_result: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        mock_sql_resource.fetch_metadata.return_value = expected_result

        # Execute with MetadataType.ALL and database (should ignore database)
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.ALL, database="test_db"
        )

        # Assert
        assert result == expected_result
        mock_sql_resource.fetch_metadata.assert_called_once_with(
            {
                "metadata_sql": "SELECT * FROM test",
                "database_alias_key": "db_alias",
                "schema_alias_key": "schema_alias",
                "database_result_key": "TABLE_CATALOG",
                "schema_result_key": "TABLE_SCHEMA",
            }
        )
        mock_sql_resource.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_database_type_without_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching databases with MetadataType.DATABASE and no database"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.DATABASE_KEY = "TABLE_CATALOG"

        # Mock database query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Execute with MetadataType.DATABASE and no database
        result = await controller.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )
        assert mock_sql_resource.run_query.call_count == 1
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_database_type_with_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching databases with MetadataType.DATABASE and database (should ignore database)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.DATABASE_KEY = "TABLE_CATALOG"

        # Mock database query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Execute with MetadataType.DATABASE and database (should ignore database)
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.DATABASE, database="test_db"
        )

        # Assert
        assert result == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )
        assert mock_sql_resource.run_query.call_count == 1
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_type_without_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching schemas with MetadataType.SCHEMA and no database (should error)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )

        # Execute and Assert with MetadataType.SCHEMA but no database
        with pytest.raises(
            ValueError, match="Database must be specified when fetching schemas"
        ):
            await controller.fetch_metadata(metadata_type=MetadataType.SCHEMA)

        # Verify neither method was called
        mock_sql_resource.run_query.assert_not_called()
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_type_with_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetching schemas with MetadataType.SCHEMA and database"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        # Mock schema query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_SCHEMA": "schema1"}, {"TABLE_SCHEMA": "schema2"}]]
        )

        # Execute with MetadataType.SCHEMA and database
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database="test_db"
        )

        # Assert
        assert result == [
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema2"},
        ]
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT schema_name FROM schemas WHERE database = 'test_db'"
        )
        assert mock_sql_resource.run_query.call_count == 1
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_none_type_without_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata with None type and no database (should error)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)

        # Execute and Assert with None type and no database
        with pytest.raises(ValueError, match="Invalid metadata type: None"):
            await controller.fetch_metadata()

        # Verify neither method was called
        mock_sql_resource.run_query.assert_not_called()
        mock_sql_resource.fetch_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_none_type_with_database(
        self, mock_sql_resource: MagicMock
    ) -> None:
        """Test fetch_metadata with None type and database (should error)"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)

        # Execute and Assert with None type and database
        with pytest.raises(ValueError, match="Invalid metadata type: None"):
            await controller.fetch_metadata(database="test_db")

        # Verify neither method was called
        mock_sql_resource.run_query.assert_not_called()
        mock_sql_resource.fetch_metadata.assert_not_called()
