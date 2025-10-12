from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler


@pytest.fixture
def async_sql_client():
    client = AsyncBaseSQLClient()
    client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"
    return client


@pytest.fixture
def handler(async_sql_client: Any) -> BaseSQLHandler:
    handler = BaseSQLHandler(async_sql_client)
    handler.database_alias_key = "TABLE_CATALOG"
    handler.schema_alias_key = "TABLE_SCHEMA"
    return handler


@pytest.fixture
def mock_async_engine_with_connection():
    """Common fixture for mocking async engine with proper context manager."""
    mock_engine = AsyncMock()
    mock_connection = AsyncMock()

    # Create a second connection for execution_options return
    mock_connection_with_options = AsyncMock()
    mock_connection_with_options.stream = AsyncMock()
    mock_connection_with_options.execute = AsyncMock()
    mock_connection_with_options.execution_options = MagicMock(
        return_value=mock_connection_with_options
    )

    # Set up the original connection
    mock_connection.stream = AsyncMock()
    mock_connection.execute = AsyncMock()
    mock_connection.execution_options = MagicMock(
        return_value=mock_connection_with_options
    )

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_connection

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    # Make connect() return the context manager directly, not a coroutine
    mock_engine.connect = MagicMock(return_value=MockAsyncContextManager())

    return mock_engine, mock_connection, mock_connection_with_options


@patch("sqlalchemy.ext.asyncio.create_async_engine")
@pytest.mark.asyncio
async def test_load(
    create_async_engine: Any,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    # Use the common fixture
    mock_engine, mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    create_async_engine.return_value = mock_engine

    credentials = {"username": "test_user", "password": "test_password"}

    # Run the load function
    await async_sql_client.load(credentials)

    # Assertions to verify behavior
    assert async_sql_client.DB_CONFIG is not None
    create_async_engine.assert_called_once_with(
        async_sql_client.get_sqlalchemy_connection_string(),
        connect_args=async_sql_client.DB_CONFIG.connect_args,
        pool_pre_ping=True,
    )
    assert async_sql_client.engine == mock_engine
    # AsyncBaseSQLClient doesn't store persistent connection
    assert async_sql_client.connection is None


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata(mock_run_query: Any, handler: BaseSQLHandler):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    import pandas as pd

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    handler.database_alias_key = "TABLE_CATALOG"
    handler.schema_alias_key = "TABLE_SCHEMA"
    result = await handler.prepare_metadata()

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata_without_database_alias_key(
    mock_run_query: Any, handler: BaseSQLHandler
):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]

    import pandas as pd

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    handler.database_alias_key = "TABLE_CATALOG"
    handler.schema_alias_key = "TABLE_SCHEMA"
    result = await handler.prepare_metadata()

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata_with_result_keys(
    mock_run_query: Any, handler: BaseSQLHandler
):
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    import pandas as pd

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    handler.database_result_key = "DATABASE"
    handler.schema_result_key = "SCHEMA"

    # Run fetch_metadata
    result = await handler.prepare_metadata()

    # Assertions
    assert result == [{"DATABASE": "test_db", "SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata_with_error(
    mock_run_query: AsyncMock, handler: BaseSQLHandler
):
    mock_run_query.side_effect = Exception("Simulated query failure")

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata and expect it to raise an exception
    with pytest.raises(Exception, match="Simulated query failure"):
        handler.database_alias_key = "TABLE_CATALOG"
        handler.schema_alias_key = "TABLE_SCHEMA"
        await handler.prepare_metadata()

    # Assertions
    mock_run_query.assert_called_once_with()


@pytest.mark.asyncio
@patch(
    "sqlalchemy.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_client_side_cursor(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    # Use the common fixture
    mock_engine, mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    async_sql_client.engine = mock_engine

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

    mock_connection.execute = AsyncMock(return_value=mock_result)

    # Set the configuration to NOT use server-side cursor
    async_sql_client.use_server_side_cursor = False

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in async_sql_client.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    mock_connection.execute.assert_called_once_with(query)
    mock_result.cursor.fetchmany.assert_called()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "sqlalchemy.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_server_side_cursor(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    # Use the common fixture
    mock_engine, mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    async_sql_client.engine = mock_engine

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

    mock_connection_with_options.stream = AsyncMock(return_value=mock_result)

    # Set the configuration to use server-side cursor
    async_sql_client.use_server_side_cursor = True

    # Call the run_query method
    results: list[dict[str, str]] = []
    async for batch in async_sql_client.run_query(query, batch_size=2):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results
    mock_connection_with_options.stream.assert_called_once_with(query)
    mock_result.fetchmany.assert_awaited()
    mock_result.keys.assert_called_once()


@pytest.mark.asyncio
@patch(
    "sqlalchemy.text",
    side_effect=lambda q: q,  # type: ignore
)
async def test_run_query_with_error(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    # Use the common fixture
    mock_engine, mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    async_sql_client.engine = mock_engine

    # Mock the query
    query = "SELECT * FROM test_table"

    # Mock the result set returned by the query
    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1", "col2"]
    mock_result.fetchmany = AsyncMock(
        side_effect=[Exception("Simulated query failure")]
    )

    mock_connection_with_options.stream = AsyncMock(return_value=mock_result)

    # Set the configuration to use server-side cursor
    async_sql_client.use_server_side_cursor = True

    results: list[dict[str, str]] = []
    with pytest.raises(Exception, match="Simulated query failure"):
        async for batch in async_sql_client.run_query(query):
            results.extend(batch)
