import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)


@pytest.fixture
def config():
    # Create a sample SQLResourceConfig object with mock credentials
    return SQLResourceConfig(
        credentials={
            "user": "test_user",
            "password": "test_password",
            "host": "localhost",
            "port": 5432,
            "database": "test_db",
        },
        database_driver="psycopg2",
        database_dialect="postgresql",
        sql_alchemy_connect_args={},
    )


@pytest.fixture
def resource(config: SQLResourceConfig):
    # Create a SQLResource object with the above config
    return SQLResource(config=config)


@patch("application_sdk.workflows.sql.resources.sql_resource.create_engine")
def test_load(mock_create_engine: Any, resource: SQLResource):
    # Mock the engine and connection
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    mock_create_engine.return_value = mock_engine
    mock_engine.connect.return_value = mock_connection

    # Run the load function
    asyncio.run(resource.load())

    # Assertions to verify behavior
    mock_create_engine.assert_called_once_with(
        resource.config.get_sqlalchemy_connection_string(),
        connect_args=resource.config.get_sqlalchemy_connect_args(),
        pool_pre_ping=True,
    )
    assert resource.engine == mock_engine
    assert resource.connection == mock_connection


@patch("application_sdk.workflows.sql.resources.sql_resource.SQLResource.run_query")
async def test_fetch_metadata(mock_run_query: Any, resource: SQLResource):
    async def async_gen(_):
        yield [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    mock_run_query.side_effect = async_gen

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    result = await resource.fetch_metadata(
        metadata_sql,
        database_alias_key="TABLE_CATALOG",
        schema_alias_key="TABLE_SCHEMA",
    )

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with(metadata_sql)


@patch("application_sdk.workflows.sql.resources.sql_resource.SQLResource.run_query")
async def test_fetch_metadata_with_result_keys(
    mock_run_query: Any, resource: SQLResource
):
    async def async_gen(_):
        yield [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    mock_run_query.side_effect = async_gen

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    result = await resource.fetch_metadata(
        metadata_sql,
        database_alias_key="TABLE_CATALOG",
        schema_alias_key="TABLE_SCHEMA",
        database_result_key="DATABASE",
        schema_result_key="SCHEMA",
    )

    # Assertions
    assert result == [{"DATABASE": "test_db", "SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with(metadata_sql)


@patch("application_sdk.workflows.sql.resources.sql_resource.SQLResource.run_query")
async def test_fetch_metadata_with_error(
    mock_run_query: AsyncMock, resource: SQLResource
):
    mock_run_query.side_effect = Exception("Simulated query failure")

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata and expect it to raise an exception
    with pytest.raises(Exception, match="Simulated query failure"):
        await resource.fetch_metadata(
            metadata_sql,
            database_alias_key="TABLE_CATALOG",
            schema_alias_key="TABLE_SCHEMA",
        )

    # Assertions
    mock_run_query.assert_called_once_with(metadata_sql)


@pytest.mark.asyncio
@patch("application_sdk.workflows.sql.resources.sql_resource.text")
@patch(
    "application_sdk.workflows.sql.resources.sql_resource.asyncio.get_running_loop",
    new_callable=MagicMock,
)
async def test_run_query(
    mock_get_running_loop: MagicMock, mock_text: Any, resource: SQLResource
):
    # Mock the query text
    query = "SELECT * FROM test_table"
    mock_text.return_value = query

    def get_item_gen(arr: list[str]):
        def get_item(idx: int):
            return arr[idx]

        return get_item

    # Create MagicMock rows with `_fields` and specific attribute values
    row1 = MagicMock()
    row1.col1 = "row1_col1"
    row1.col2 = "row1_col2"
    row1.__iter__.return_value = iter(["row1_col1", "row1_col2"])
    row1.__getitem__.side_effect = get_item_gen(["row1_col1", "row1_col2"])

    row2 = MagicMock()
    row2.col1 = "row2_col1"
    row2.col2 = "row2_col2"
    row2.__iter__.return_value = iter(["row2_col1", "row2_col2"])
    row2.__getitem__.side_effect = get_item_gen(["row2_col1", "row2_col2"])

    # Mock the connection execute and cursor
    mock_cursor = MagicMock()

    col1 = MagicMock()
    col1.name = "COL1"

    col2 = MagicMock()
    col2.name = "COL2"

    mock_cursor.cursor.description = [col1, col2]
    mock_cursor.fetchmany = MagicMock(
        side_effect=[
            [row1, row2],  # First batch
            [],  # End of data
        ]
    )

    resource.connection = MagicMock()
    resource.connection.execute.return_value = mock_cursor

    # Mock run_in_executor to return cursor and then batches
    mock_get_running_loop.return_value.run_in_executor = AsyncMock(
        side_effect=[
            mock_cursor,  # Simulate connection.execute
            [row1, row2],  # First batch from `fetchmany`
            [],  # End of data from `fetchmany`
        ]
    )

    # Run run_query and collect all results
    results: list[dict[str, str]] = []
    async for batch in resource.run_query(query):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
