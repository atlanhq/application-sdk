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


def test_init_without_config():
    with pytest.raises(ValueError, match="config is required"):
        SQLResource()


@patch("application_sdk.workflows.sql.resources.sql_resource.create_async_engine")
def test_load(create_async_engine: Any, resource: SQLResource):
    # Mock the engine and connection
    mock_engine = AsyncMock()
    mock_connection = MagicMock()
    create_async_engine.return_value = mock_engine
    mock_engine.connect.return_value = mock_connection

    # Run the load function
    asyncio.run(resource.load())

    # Assertions to verify behavior
    create_async_engine.assert_called_once_with(
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
async def test_fetch_metadata_without_database_alias_key(
    mock_run_query: Any, resource: SQLResource
):
    async def async_gen(_):
        yield [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    mock_run_query.side_effect = async_gen

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    resource.default_database_alias_key = "TABLE_CATALOG"
    resource.default_schema_alias_key = "TABLE_SCHEMA"
    result = await resource.fetch_metadata(
        metadata_sql,
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
@patch(
    "application_sdk.workflows.sql.resources.sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_client_side_cursor(mock_text: MagicMock, resource: MagicMock):
    # Mock the query
    query = "SELECT * FROM test_table"

    # Mock the result set returned by the query
    row1 = ("row1_col1", "row1_col2")
    row2 = ("row2_col1", "row2_col2")
    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1", "col2"]
    mock_result.cursor = MagicMock()
    mock_result.cursor.fetchmany = MagicMock(
        side_effect=[
            [row1, row2],  # First batch
            [],  # No more data
        ]
    )

    # Mock the connection and method execution
    resource.connection = MagicMock()
    resource.connection.execute = AsyncMock(return_value=mock_result)

    # Set the configuration to NOT use server-side cursor
    resource.config = MagicMock()
    resource.config.use_server_side_cursor = False

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in resource.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    resource.connection.execute.assert_called_once_with(query)
    mock_result.cursor.fetchmany.assert_called()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "application_sdk.workflows.sql.resources.sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_server_side_cursor(mock_text: MagicMock, resource: MagicMock):
    # Mock the query
    query = "SELECT * FROM test_table"

    # Mock the result set returned by the query
    row1 = ("row1_col1", "row1_col2")
    row2 = ("row2_col1", "row2_col2")
    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1", "col2"]
    mock_result.fetchmany = AsyncMock(
        side_effect=[
            [row1, row2],  # First batch
            [],  # No more data
        ]
    )

    async def empty_fn(*args, **kwargs):  # type: ignore # Mock execution options
        pass

    # Mock the connection and method execution
    resource.connection = MagicMock()
    resource.connection.stream = AsyncMock(return_value=mock_result)
    resource.connection.execution_options.side_effect = empty_fn

    # Set the configuration to use server-side cursor
    resource.config = MagicMock()
    resource.config.use_server_side_cursor = True

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in resource.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    resource.connection.stream.assert_called_once_with(query)
    mock_result.fetchmany.assert_awaited()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "application_sdk.workflows.sql.resources.sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_with_error(mock_text: MagicMock, resource: MagicMock):
    # Mock the query
    query = "SELECT * FROM test_table"

    # Mock the result set returned by the query
    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1", "col2"]
    mock_result.fetchmany = AsyncMock(
        side_effect=[Exception("Simulated query failure")]
    )

    async def empty_fn(*args, **kwargs):  # type: ignore # Mock execution options
        pass

    # Mock the connection and method execution
    resource.connection = MagicMock()
    resource.connection.stream = AsyncMock(return_value=mock_result)
    resource.connection.execution_options.side_effect = empty_fn

    # Set the configuration to use server-side cursor
    resource.config = MagicMock()
    resource.config.use_server_side_cursor = True

    results: list[dict[str, str]] = []
    with pytest.raises(Exception, match="Simulated query failure"):
        async for batch in resource.run_query(query):
            results.extend(batch)
