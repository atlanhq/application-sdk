from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.app.rest.fastapi.models.workflow import MetadataType
from application_sdk.workflows.sql.controllers.metadata import (
    SQLWorkflowMetadataController,
)
from application_sdk.workflows.sql.resources.sql_resource import SQLResource


class AsyncIteratorMock:
    """Helper class to mock async iterators"""

    def __init__(self, items):
        self.items = items.copy()  # Create a copy to avoid modifying original list

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return self.items.pop(0)
        except IndexError:
            raise StopAsyncIteration


@pytest.fixture
def mock_sql_resource():
    resource = MagicMock(spec=SQLResource)
    resource.run_query = MagicMock()  # Use regular MagicMock instead of AsyncMock
    resource.fetch_metadata = AsyncMock()
    return resource


class TestSQLWorkflowMetadataController:
    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode(self, mock_sql_resource):
        """Test fetch_metadata when hierarchical fetching is disabled"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.METADATA_SQL = "SELECT * FROM test"
        controller.USE_HIERARCHICAL_FETCH = False
        expected_result = [{"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}]
        mock_sql_resource.fetch_metadata.return_value = expected_result

        # Execute
        result = await controller.fetch_metadata()

        # Assert
        assert result == expected_result
        mock_sql_resource.fetch_metadata.assert_called_once_with(
            {
                "metadata_sql": "SELECT * FROM test",
                "database_alias_key": None,
                "schema_alias_key": None,
                "database_result_key": "TABLE_CATALOG",
                "schema_result_key": "TABLE_SCHEMA",
            }
        )
        mock_sql_resource.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_hierarchical_mode(self, mock_sql_resource):
        """Test fetch_metadata when hierarchical fetching is enabled"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
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

        # First fetch databases
        databases = await controller.fetch_databases()
        assert databases == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]

        # Then fetch schemas for each database
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
        assert mock_sql_resource.run_query.call_count == 3
        mock_sql_resource.run_query.assert_any_call(
            "SELECT database_name FROM databases"
        )
        mock_sql_resource.run_query.assert_any_call(
            "SELECT schema_name FROM schemas WHERE database = 'db1'"
        )
        mock_sql_resource.run_query.assert_any_call(
            "SELECT schema_name FROM schemas WHERE database = 'db2'"
        )

    @pytest.mark.asyncio
    async def test_fetch_databases_only(self, mock_sql_resource):
        """Test fetching only databases"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.DATABASE_KEY = "TABLE_CATALOG"

        # Mock database query results
        mock_sql_resource.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Expected result
        expected_result = [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]

        # Execute
        result = await controller.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == expected_result
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )

    @pytest.mark.asyncio
    async def test_fetch_schemas_for_database(self, mock_sql_resource):
        """Test fetching schemas for a specific database"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
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
        # Expected result
        expected_result = [
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "schema2"},
        ]

        # Execute
        result = await controller.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database=test_database
        )

        # Assert
        assert result == expected_result
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT schema_name FROM schemas WHERE database = 'test_db'"
        )

    @pytest.mark.asyncio
    async def test_fetch_metadata_invalid_type(self, mock_sql_resource):
        """Test fetch_metadata with invalid metadata type"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True

        # Execute and Assert
        with pytest.raises(ValueError, match="Invalid metadata type: invalid_type"):
            await controller.fetch_metadata(metadata_type="invalid_type")

    @pytest.mark.asyncio
    async def test_fetch_schemas_without_database(self, mock_sql_resource):
        """Test fetching schemas without specifying a database"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True

        # Execute and Assert
        with pytest.raises(
            ValueError, match="Database must be specified when fetching schemas"
        ):
            await controller.fetch_metadata(metadata_type=MetadataType.SCHEMA)

    @pytest.mark.asyncio
    async def test_fetch_metadata_hierarchical_mode_empty_databases(
        self, mock_sql_resource
    ):
        """Test hierarchical fetching when no databases are found"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )

        # Mock empty database result
        mock_sql_resource.run_query.return_value = AsyncIteratorMock([[]])

        # Execute
        databases = await controller.fetch_databases()

        # Assert
        assert databases == []
        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )

    @pytest.mark.asyncio
    async def test_fetch_metadata_hierarchical_mode_empty_schemas(
        self, mock_sql_resource
    ):
        """Test hierarchical fetching when databases exist but have no schemas"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        controller.DATABASE_KEY = "TABLE_CATALOG"
        controller.SCHEMA_KEY = "TABLE_SCHEMA"

        # Mock results: one database but no schemas
        mock_sql_resource.run_query.side_effect = [
            AsyncIteratorMock([[{"TABLE_CATALOG": "db1"}]]),  # Database exists
            AsyncIteratorMock([[]]),  # But no schemas
        ]

        # First fetch databases
        databases = await controller.fetch_databases()
        assert databases == [{"TABLE_CATALOG": "db1"}]

        # Then fetch schemas (should be empty)
        schemas = await controller.fetch_schemas("db1")
        assert schemas == []

        # Assert the calls were made correctly
        assert mock_sql_resource.run_query.call_count == 2
        mock_sql_resource.run_query.assert_any_call(
            "SELECT database_name FROM databases"
        )
        mock_sql_resource.run_query.assert_any_call(
            "SELECT schema_name FROM schemas WHERE database = 'db1'"
        )

    @pytest.mark.asyncio
    async def test_fetch_metadata_hierarchical_mode_error_handling(
        self, mock_sql_resource
    ):
        """Test error handling in hierarchical fetching"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)
        controller.USE_HIERARCHICAL_FETCH = True
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )

        # Mock database query to raise an exception
        class ErrorAsyncIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise Exception("Database query failed")

        mock_sql_resource.run_query.return_value = ErrorAsyncIterator()

        # Execute and Assert
        with pytest.raises(Exception) as exc_info:
            await controller.fetch_databases()
        assert str(exc_info.value) == "Database query failed"

        mock_sql_resource.run_query.assert_called_once_with(
            "SELECT database_name FROM databases"
        )

    @pytest.mark.asyncio
    async def test_use_hierarchical_fetch_property(self, mock_sql_resource):
        """Test the use_hierarchical_fetch property behavior"""
        # Setup
        controller = SQLWorkflowMetadataController(sql_resource=mock_sql_resource)

        # Test when both queries are empty
        controller.FETCH_DATABASES_SQL = ""
        controller.FETCH_SCHEMAS_SQL = ""
        assert not controller.use_hierarchical_fetch

        # Test when only database query is set
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = ""
        assert not controller.use_hierarchical_fetch

        # Test when only schema query is set
        controller.FETCH_DATABASES_SQL = ""
        controller.FETCH_SCHEMAS_SQL = "SELECT schema_name FROM schemas"
        assert not controller.use_hierarchical_fetch

        # Test when both queries are set
        controller.FETCH_DATABASES_SQL = "SELECT database_name FROM databases"
        controller.FETCH_SCHEMAS_SQL = "SELECT schema_name FROM schemas"
        assert controller.use_hierarchical_fetch
