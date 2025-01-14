import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from application_sdk.clients.async_sql_resource import (
    AsyncSQLResource,
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
        sql_alchemy_connect_args={},
    )


@pytest.fixture
def async_sql_resource(config: SQLResourceConfig):
    resource = AsyncSQLResource(config=config)
    resource.get_sqlalchemy_connection_string = lambda: "test_connection_string"
    return resource


def test_init_without_config():
    with pytest.raises(ValueError, match="config is required"):
        AsyncSQLResource()


@patch("application_sdk.clients.async_sql_resource.create_async_engine")
def test_load(create_async_engine: Any, async_sql_resource: AsyncSQLResource):
    # Mock the engine and connection
    mock_engine = AsyncMock()
    mock_connection = MagicMock()
    create_async_engine.return_value = mock_engine
    mock_engine.connect.return_value = mock_connection

    # Run the load function
    asyncio.run(async_sql_resource.load())

    # Assertions to verify behavior
    create_async_engine.assert_called_once_with(
        async_sql_resource.get_sqlalchemy_connection_string(),
        connect_args=async_sql_resource.config.get_sqlalchemy_connect_args(),
        pool_pre_ping=True,
    )
    assert async_sql_resource.engine == mock_engine
    assert async_sql_resource.connection == mock_connection


@patch("application_sdk.inputs.sql_query.AsyncSQLQueryInput.get_dataframe")
async def test_fetch_metadata(
    mock_run_query: Any, async_sql_resource: AsyncSQLResource
):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    args = {
        "metadata_sql": metadata_sql,
        "database_alias_key": "TABLE_CATALOG",
        "schema_alias_key": "TABLE_SCHEMA",
    }
    result = await async_sql_resource.fetch_metadata(args)

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.AsyncSQLQueryInput.get_dataframe")
async def test_fetch_metadata_without_database_alias_key(
    mock_run_query: Any, async_sql_resource: AsyncSQLResource
):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    async_sql_resource.default_database_alias_key = "TABLE_CATALOG"
    async_sql_resource.default_schema_alias_key = "TABLE_SCHEMA"
    args = {
        "metadata_sql": metadata_sql,
    }
    result = await async_sql_resource.fetch_metadata(args)

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.AsyncSQLQueryInput.get_dataframe")
async def test_fetch_metadata_with_result_keys(
    mock_run_query: Any, async_sql_resource: AsyncSQLResource
):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    args = {
        "metadata_sql": metadata_sql,
        "database_alias_key": "TABLE_CATALOG",
        "schema_alias_key": "TABLE_SCHEMA",
        "database_result_key": "DATABASE",
        "schema_result_key": "SCHEMA",
    }
    result = await async_sql_resource.fetch_metadata(args)

    # Assertions
    assert result == [{"DATABASE": "test_db", "SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.AsyncSQLQueryInput.get_dataframe")
async def test_fetch_metadata_with_error(
    mock_run_query: AsyncMock, async_sql_resource: AsyncSQLResource
):
    mock_run_query.side_effect = Exception("Simulated query failure")

    # Sample SQL query
    metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata and expect it to raise an exception
    with pytest.raises(Exception, match="Simulated query failure"):
        args = {
            "metadata_sql": metadata_sql,
            "database_alias_key": "TABLE_CATALOG",
            "schema_alias_key": "TABLE_SCHEMA",
        }
        await async_sql_resource.fetch_metadata(args)

    # Assertions
    mock_run_query.assert_called_once_with()


@pytest.mark.asyncio
@patch(
    "application_sdk.clients.async_sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_client_side_cursor(
    mock_text: MagicMock, async_sql_resource: MagicMock
):
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
    async_sql_resource.connection = MagicMock()
    async_sql_resource.connection.execute = AsyncMock(return_value=mock_result)

    # Set the configuration to NOT use server-side cursor
    async_sql_resource.config = MagicMock()
    async_sql_resource.config.use_server_side_cursor = False

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in async_sql_resource.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    async_sql_resource.connection.execute.assert_called_once_with(query)
    mock_result.cursor.fetchmany.assert_called()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "application_sdk.clients.async_sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_server_side_cursor(
    mock_text: MagicMock, async_sql_resource: MagicMock
):
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
    async_sql_resource.connection = MagicMock()
    async_sql_resource.connection.stream = AsyncMock(return_value=mock_result)
    async_sql_resource.connection.execution_options.side_effect = empty_fn

    # Set the configuration to use server-side cursor
    async_sql_resource.config = MagicMock()
    async_sql_resource.config.use_server_side_cursor = True

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in async_sql_resource.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    async_sql_resource.connection.stream.assert_called_once_with(query)
    mock_result.fetchmany.assert_awaited()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "application_sdk.clients.async_sql_resource.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_with_error(
    mock_text: MagicMock, async_sql_resource: MagicMock
):
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
    async_sql_resource.connection = MagicMock()
    async_sql_resource.connection.stream = AsyncMock(return_value=mock_result)
    async_sql_resource.connection.execution_options.side_effect = empty_fn

    # Set the configuration to use server-side cursor
    async_sql_resource.config = MagicMock()
    async_sql_resource.config.use_server_side_cursor = True

    results: list[dict[str, str]] = []
    with pytest.raises(Exception, match="Simulated query failure"):
        async for batch in async_sql_resource.run_query(query):
            results.extend(batch)
