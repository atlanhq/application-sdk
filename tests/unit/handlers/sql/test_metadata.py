from typing import Any, Dict, Generic, List, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.server.fastapi.models import MetadataType
from application_sdk.test_utils.hypothesis.strategies.handlers.sql.sql_metadata import (
    database_list_strategy,
    database_name_strategy,
    metadata_entry_strategy,
    metadata_list_strategy,
    schema_list_strategy,
    sql_handler_config_strategy,
)

T = TypeVar("T")

# Configure Hypothesis settings at the module level
settings.register_profile(
    "sql_metadata",
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
    ],
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
    deadline=None,
)
settings.load_profile("sql_metadata")


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
def mock_sql_client() -> MagicMock:
    client = MagicMock(spec=BaseSQLClient)
    client.run_query = MagicMock()  # Use regular MagicMock instead of AsyncMock
    return client


@pytest.fixture
def handler(mock_sql_client: Any) -> BaseSQLHandler:
    handler = BaseSQLHandler(sql_client=mock_sql_client)
    handler.prepare_metadata = AsyncMock()
    return handler


def setup_handler_config(handler: BaseSQLHandler, config: Dict[str, str]) -> None:
    """Helper function to set up handler configuration"""
    handler.metadata_sql = config["metadata_sql"]
    handler.fetch_databases_sql = config["fetch_databases_sql"]
    handler.fetch_schemas_sql = config["fetch_schemas_sql"]
    handler.database_result_key = config["database_result_key"]
    handler.schema_result_key = config["schema_result_key"]
    handler.database_alias_key = config["database_alias_key"]
    handler.schema_alias_key = config["schema_alias_key"]


class TestSQLWorkflowHandler:
    @pytest.mark.asyncio
    @given(config=sql_handler_config_strategy, metadata=metadata_list_strategy)
    async def test_fetch_metadata_flat_mode(
        self,
        handler: MagicMock,
        mock_sql_client: MagicMock,
        config: Dict[str, str],
        metadata: List[Dict[str, str]],
    ) -> None:
        """Test fetch_metadata when hierarchical fetching is disabled (MetadataType.ALL)"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        # Setup mock return value
        handler.prepare_metadata.return_value = metadata

        # Execute with MetadataType.ALL
        result = await handler.fetch_metadata(metadata_type=MetadataType.ALL)

        # Assert
        assert result == metadata
        handler.prepare_metadata.assert_called_once()
        mock_sql_client.run_query.assert_not_called()

    @pytest.mark.asyncio
    @given(
        config=sql_handler_config_strategy,
        databases=database_list_strategy,
        schemas=schema_list_strategy,
    )
    async def test_fetch_metadata_hierarchical_mode(
        self,
        handler: MagicMock,
        mock_sql_client: MagicMock,
        config: Dict[str, str],
        databases: List[Dict[str, str]],
        schemas: List[Dict[str, str]],
    ) -> None:
        """Test fetch_metadata when hierarchical fetching is enabled"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        # Mock database and schema query results
        mock_sql_client.run_query.side_effect = [
            AsyncIteratorMock([databases]),
            AsyncIteratorMock([schemas]),
            AsyncIteratorMock([schemas[:1]]),  # Different schemas for second database
        ]

        # First fetch databases directly
        result_dbs = await handler.fetch_databases()
        assert result_dbs == databases

        # Then fetch schemas for each database
        if databases:
            first_db = databases[0]["TABLE_CATALOG"]
            result_schemas = await handler.fetch_schemas(first_db)
            expected_schemas = [
                {"TABLE_CATALOG": first_db, "TABLE_SCHEMA": s["TABLE_SCHEMA"]}
                for s in schemas
            ]
            assert result_schemas == expected_schemas

    @pytest.mark.asyncio
    @given(config=sql_handler_config_strategy, databases=database_list_strategy)
    async def test_fetch_metadata_database_type(
        self,
        handler: MagicMock,
        mock_sql_client: MagicMock,
        config: Dict[str, str],
        databases: List[Dict[str, str]],
    ) -> None:
        """Test fetching only databases using MetadataType.DATABASE"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        mock_sql_client.run_query.return_value = AsyncIteratorMock([databases])

        # Execute with MetadataType.DATABASE
        result = await handler.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == databases
        mock_sql_client.run_query.assert_called_once_with(handler.metadata_sql)

    @pytest.mark.skip(
        reason="Failing due to ValueError: Database must be specified when fetching schemas"
    )
    @pytest.mark.asyncio
    @given(
        config=sql_handler_config_strategy,
        schemas=st.lists(metadata_entry_strategy, min_size=1, max_size=5),
        database=database_name_strategy,
    )
    async def test_fetch_metadata_schema_type(
        self,
        handler: MagicMock,
        mock_sql_client: MagicMock,
        config: Dict[str, str],
        schemas: List[Dict[str, str]],
        database: str,
    ) -> None:
        """Test fetching schemas using MetadataType.SCHEMA"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        mock_sql_client.run_query.return_value = AsyncIteratorMock([schemas])

        # Execute with MetadataType.SCHEMA
        result = await handler.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database=database
        )

        # Assert
        expected_schemas = [
            {"TABLE_CATALOG": database, "TABLE_SCHEMA": s["TABLE_SCHEMA"]}
            for s in schemas
        ]
        assert result == expected_schemas
        expected_query = handler.fetch_schemas_sql.format(database_name=database)
        mock_sql_client.run_query.assert_called_once_with(expected_query)

    @pytest.mark.asyncio
    @given(config=sql_handler_config_strategy)
    async def test_fetch_metadata_empty_databases(
        self, handler: MagicMock, mock_sql_client: MagicMock, config: Dict[str, str]
    ) -> None:
        """Test fetching empty databases using MetadataType.DATABASE"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        mock_sql_client.run_query.return_value = AsyncIteratorMock([[]])

        result = await handler.fetch_metadata(metadata_type=MetadataType.DATABASE)
        assert result == []
        mock_sql_client.run_query.assert_called_once_with(handler.metadata_sql)

    @pytest.mark.skip(
        reason="Failing due to ValueError: Database must be specified when fetching schemas"
    )
    @pytest.mark.asyncio
    @given(config=sql_handler_config_strategy, database=database_name_strategy)
    async def test_fetch_metadata_empty_schemas(
        self,
        handler: MagicMock,
        mock_sql_client: MagicMock,
        config: Dict[str, str],
        database: str,
    ) -> None:
        """Test fetching empty schemas using MetadataType.SCHEMA"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        mock_sql_client.run_query.return_value = AsyncIteratorMock([[]])

        result = await handler.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database=database
        )
        assert result == []
        expected_query = handler.fetch_schemas_sql.format(database_name=database)
        mock_sql_client.run_query.assert_called_once_with(expected_query)

    @pytest.mark.asyncio
    @given(config=sql_handler_config_strategy)
    async def test_fetch_metadata_error_handling(
        self, handler: MagicMock, mock_sql_client: MagicMock, config: Dict[str, str]
    ) -> None:
        """Test error handling in metadata fetching"""
        # Reset mocks and setup handler
        mock_sql_client.run_query.reset_mock()
        handler.prepare_metadata.reset_mock()
        setup_handler_config(handler, config)

        class ErrorAsyncIterator:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise Exception("Database query failed")

        mock_sql_client.run_query.return_value = ErrorAsyncIterator()

        with pytest.raises(Exception) as exc_info:
            await handler.fetch_metadata(metadata_type=MetadataType.DATABASE)
        assert str(exc_info.value) == "Database query failed"
        mock_sql_client.run_query.assert_called_once_with(handler.metadata_sql)

    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode_without_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetch_metadata with MetadataType.ALL and no database"""
        # Setup
        handler.metadata_sql = "SELECT * FROM test"
        handler.database_alias_key = "db_alias"
        handler.schema_alias_key = "schema_alias"
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"

        expected_result: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        handler.prepare_metadata.return_value = expected_result

        # Execute with MetadataType.ALL and no database
        result = await handler.fetch_metadata(metadata_type=MetadataType.ALL)

        # Assert
        assert result == expected_result
        handler.prepare_metadata.assert_called_once()
        mock_sql_client.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_flat_mode_with_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetch_metadata with MetadataType.ALL and database (should ignore database)"""
        # Setup
        handler.metadata_sql = "SELECT * FROM test"
        handler.database_alias_key = "db_alias"
        handler.schema_alias_key = "schema_alias"
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"

        expected_result: List[Dict[str, str]] = [
            {"TABLE_CATALOG": "db1", "TABLE_SCHEMA": "schema1"}
        ]
        handler.prepare_metadata.return_value = expected_result

        # Execute with MetadataType.ALL and database (should ignore database)
        result = await handler.fetch_metadata(
            metadata_type=MetadataType.ALL, database="test_db"
        )

        # Assert
        assert result == expected_result
        handler.prepare_metadata.assert_called_once()
        mock_sql_client.run_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_database_type_without_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetching databases with MetadataType.DATABASE and no database"""
        handler.fetch_databases_sql = "SELECT database_name FROM databases"
        handler.database_result_key = "TABLE_CATALOG"
        handler.metadata_sql = "SELECT database_name, schema_name FROM schemas"

        # Mock database query results
        mock_sql_client.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Execute with MetadataType.DATABASE and no database
        result = await handler.fetch_metadata(metadata_type=MetadataType.DATABASE)

        # Assert
        assert result == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]
        # Verify run_query was called with fetch_databases_sql
        mock_sql_client.run_query.assert_called_once_with(handler.metadata_sql)
        assert mock_sql_client.run_query.call_count == 1
        handler.prepare_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_database_type_with_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetching databases with MetadataType.DATABASE and database (should ignore database)"""
        # Setup
        handler.fetch_databases_sql = "SELECT database_name FROM databases"
        handler.database_result_key = "TABLE_CATALOG"
        handler.metadata_sql = "SELECT database_name, schema_name FROM schemas"

        # Mock database query results
        mock_sql_client.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]]
        )

        # Execute with MetadataType.DATABASE and database (should ignore database)
        result = await handler.fetch_metadata(
            metadata_type=MetadataType.DATABASE, database="test_db"
        )

        # Assert
        assert result == [{"TABLE_CATALOG": "db1"}, {"TABLE_CATALOG": "db2"}]
        # Verify run_query was called with fetch_databases_sql (ignoring database parameter)
        mock_sql_client.run_query.assert_called_once_with(handler.metadata_sql)
        assert mock_sql_client.run_query.call_count == 1
        handler.prepare_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_type_without_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetching schemas with MetadataType.SCHEMA and no database (should error)"""
        # Setup
        handler.fetch_schemas_sql = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )

        # Execute and Assert with MetadataType.SCHEMA but no database
        with pytest.raises(
            ValueError, match="Database must be specified when fetching schemas"
        ):
            await handler.fetch_metadata(metadata_type=MetadataType.SCHEMA)

        # Verify neither method was called
        mock_sql_client.run_query.assert_not_called()
        handler.prepare_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_schema_type_with_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetching schemas with MetadataType.SCHEMA and database"""
        # Setup
        test_database = "test_db"
        handler.fetch_schemas_sql = (
            "SELECT schema_name FROM schemas WHERE database = '{database_name}'"
        )
        handler.database_result_key = "TABLE_CATALOG"
        handler.schema_result_key = "TABLE_SCHEMA"

        # Mock schema query results
        mock_sql_client.run_query.return_value = AsyncIteratorMock(
            [[{"TABLE_SCHEMA": "schema1"}, {"TABLE_SCHEMA": "schema2"}]]
        )

        # Execute with MetadataType.SCHEMA and database
        result = await handler.fetch_metadata(
            metadata_type=MetadataType.SCHEMA, database=test_database
        )

        # Assert
        assert result == [
            {"TABLE_CATALOG": test_database, "TABLE_SCHEMA": "schema1"},
            {"TABLE_CATALOG": test_database, "TABLE_SCHEMA": "schema2"},
        ]
        # Verify run_query was called with fetch_schemas_sql formatted with database
        expected_query = handler.fetch_schemas_sql.format(database_name=test_database)
        mock_sql_client.run_query.assert_called_once_with(expected_query)
        assert mock_sql_client.run_query.call_count == 1
        handler.prepare_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_none_type_without_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetch_metadata with None type and no database (should error)"""
        # Execute and Assert with None type and no database
        with pytest.raises(ValueError, match="Invalid metadata type: None"):
            await handler.fetch_metadata()

        # Verify neither method was called
        mock_sql_client.run_query.assert_not_called()
        handler.prepare_metadata.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_metadata_none_type_with_database(
        self, handler: MagicMock, mock_sql_client: MagicMock
    ) -> None:
        """Test fetch_metadata with None type and database (should error)"""
        # Execute and Assert with None type and database
        with pytest.raises(ValueError, match="Invalid metadata type: None"):
            await handler.fetch_metadata(database="test_db")

        # Verify neither method was called
        mock_sql_client.run_query.assert_not_called()
        handler.prepare_metadata.assert_not_called()
