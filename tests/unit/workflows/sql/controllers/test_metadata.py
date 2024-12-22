from unittest.mock import AsyncMock, MagicMock

import pytest

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
        controller.DATABASE_KEY = "database_name"
        controller.SCHEMA_KEY = "schema_name"

        # Mock database query results
        mock_sql_resource.run_query.side_effect = [
            AsyncIteratorMock([[{"database_name": "db1"}, {"database_name": "db2"}]]),
            AsyncIteratorMock(
                [[{"schema_name": "schema1"}, {"schema_name": "schema2"}]]
            ),
            AsyncIteratorMock([[{"schema_name": "schema3"}]]),
        ]

        # Expected result
        expected_result = [
            {"database_name": "db1", "schema_name": "schema1"},
            {"database_name": "db1", "schema_name": "schema2"},
            {"database_name": "db2", "schema_name": "schema3"},
        ]

        # Execute
        result = await controller.fetch_metadata()

        # Assert
        assert result == expected_result
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
        result = await controller.fetch_metadata()

        # Assert
        assert result == []
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
        controller.DATABASE_KEY = "database_name"
        controller.SCHEMA_KEY = "schema_name"

        # Mock results: one database but no schemas
        mock_sql_resource.run_query.side_effect = [
            AsyncIteratorMock([[{"database_name": "db1"}]]),  # Database exists
            AsyncIteratorMock([[]]),  # But no schemas
        ]

        # Execute
        result = await controller.fetch_metadata()

        # Assert
        assert result == []
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
        with pytest.raises(Exception, match="Database query failed"):
            await controller.fetch_metadata()

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
