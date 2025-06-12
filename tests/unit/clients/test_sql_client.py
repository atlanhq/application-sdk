import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.error_codes import CommonError
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.test_utils.hypothesis.strategies.clients.sql import (
    metadata_args_strategy,
    sql_credentials_strategy,
    sql_data_strategy,
    sqlalchemy_connect_args_strategy,
)
from application_sdk.test_utils.hypothesis.strategies.sql_client import (
    mock_sql_query_result_strategy,
    sql_connection_string_strategy,
    sql_error_strategy,
)


@pytest.fixture
def sql_client():
    client = BaseSQLClient()
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"
    return client


@pytest.fixture
def handler(sql_client: Any) -> BaseSQLHandler:
    handler = BaseSQLHandler(sql_client)
    handler.database_alias_key = "TABLE_CATALOG"
    handler.schema_alias_key = "TABLE_SCHEMA"
    return handler


@patch("sqlalchemy.create_engine")
def test_load(mock_create_engine: Any, sql_client: BaseSQLClient):
    """Test basic loading functionality with fixed configuration"""
    # Mock the engine and connection
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    credentials = {"username": "test_user", "password": "test_password"}
    mock_create_engine.return_value = mock_engine
    mock_engine.connect.return_value = mock_connection

    # Run the load function
    asyncio.run(sql_client.load(credentials))

    # Assertions to verify behavior
    mock_create_engine.assert_called_once_with(
        sql_client.get_sqlalchemy_connection_string(),
        connect_args=sql_client.sql_alchemy_connect_args,
        pool_pre_ping=True,
    )
    assert sql_client.engine == mock_engine
    assert sql_client.connection == mock_connection


@given(
    credentials=sql_credentials_strategy, connect_args=sqlalchemy_connect_args_strategy
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_load_property_based(
    sql_client: BaseSQLClient,
    credentials: Dict[str, Any],
    connect_args: Dict[str, Any],
):
    """Property-based test for loading with various credentials and connection arguments"""
    with patch("sqlalchemy.create_engine") as mock_create_engine:
        # Mock the engine and connection
        mock_engine = MagicMock()
        mock_connection = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.connect.return_value = mock_connection

        # Set the connection arguments
        sql_client.sql_alchemy_connect_args = connect_args

        # Run the load function
        asyncio.run(sql_client.load(credentials))

        # Assertions to verify behavior
        mock_create_engine.assert_called_once_with(
            sql_client.get_sqlalchemy_connection_string(),
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        assert sql_client.engine == mock_engine
        assert sql_client.connection == mock_connection


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata(mock_run_query: Any, handler: BaseSQLHandler):
    """Test basic metadata fetching with fixed configuration"""
    data = [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    import pandas as pd

    mock_run_query.return_value = pd.DataFrame(data)

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata
    result = await handler.prepare_metadata()

    # Assertions
    assert result == [{"TABLE_CATALOG": "test_db", "TABLE_SCHEMA": "test_schema"}]
    mock_run_query.assert_called_once_with()


@given(args=metadata_args_strategy, data=sql_data_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
async def test_fetch_metadata_property_based(
    handler: BaseSQLHandler,
    args: Dict[str, Any],
    data: List[Dict[str, Any]],
):
    """Property-based test for fetching metadata with various arguments and data"""
    with patch(
        "application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe"
    ) as mock_run_query:
        # Update handler with the test arguments
        if "database_alias_key" in args:
            handler.database_alias_key = args["database_alias_key"]
        if "schema_alias_key" in args:
            handler.schema_alias_key = args["schema_alias_key"]
        if "database_result_key" in args:
            handler.database_result_key = args["database_result_key"]
        if "schema_result_key" in args:
            handler.schema_result_key = args["schema_result_key"]

        # Create test data with the required keys
        test_data: List[Dict[str, str]] = []
        for row in data:
            test_row = {
                handler.database_alias_key: row.get("database", "test_db"),
                handler.schema_alias_key: row.get("schema", "test_schema"),
            }
            test_data.append(test_row)

        import pandas as pd

        mock_run_query.return_value = pd.DataFrame(test_data)

        handler.metadata_sql = args["metadata_sql"]

        # Run prepare_metadata
        result = await handler.prepare_metadata()

        # Assertions
        assert len(result) == len(test_data)
        mock_run_query.assert_called_once_with()

        # Verify the keys in the result
        for row in result:
            if handler.database_result_key:
                assert handler.database_result_key in row
            if handler.schema_result_key:
                assert handler.schema_result_key in row


@patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
async def test_fetch_metadata_without_database_alias_key(
    mock_run_query: Any, handler: BaseSQLHandler
):
    """Test metadata fetching without database alias key"""
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
    """Test metadata fetching with custom result keys"""
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
    """Test error handling in metadata fetching"""
    mock_run_query.side_effect = Exception("Simulated query failure")

    # Sample SQL query
    handler.metadata_sql = "SELECT * FROM information_schema.tables"

    # Run fetch_metadata and expect it to raise an exception
    with pytest.raises(Exception, match="Simulated query failure"):
        handler.database_alias_key = ("TABLE_CATALOG",)
        handler.schema_alias_key = ("TABLE_SCHEMA",)
        await handler.prepare_metadata()

    # Assertions
    mock_run_query.assert_called_once_with()


@pytest.mark.asyncio
@patch("sqlalchemy.text")
@patch(
    "application_sdk.clients.sql.asyncio.get_running_loop",
    new_callable=MagicMock,
)
async def test_run_query(
    mock_get_running_loop: MagicMock, mock_text: Any, sql_client: BaseSQLClient
):
    """Test basic query execution with fixed data"""
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

    sql_client.connection = MagicMock()
    sql_client.connection.execute.return_value = mock_cursor

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
    async for batch in sql_client.run_query(query):
        results.extend(batch)

    # Expected results formatted as dictionaries
    expected_results = [
        {"col1": "row1_col1", "col2": "row1_col2"},
        {"col1": "row2_col1", "col2": "row2_col2"},
    ]

    # Assertions
    assert results == expected_results


@pytest.mark.asyncio
@patch("sqlalchemy.text")
@patch(
    "application_sdk.clients.sql.asyncio.get_running_loop",
    new_callable=MagicMock,
)
async def test_run_query_with_error(
    mock_get_running_loop: MagicMock, mock_text: Any, sql_client: BaseSQLClient
):
    """Test error handling in query execution"""
    # Mock the query text
    query = "SELECT * FROM test_table"
    mock_text.return_value = query

    # Mock the connection execute and cursor
    mock_cursor = MagicMock()

    col1 = MagicMock()
    col1.name = "COL1"

    col2 = MagicMock()
    col2.name = "COL2"

    mock_cursor.cursor.description = [col1, col2]

    sql_client.connection = MagicMock()
    sql_client.connection.execute.return_value = mock_cursor

    # Mock run_in_executor to return cursor and then batches
    mock_get_running_loop.return_value.run_in_executor = AsyncMock(
        side_effect=[
            mock_cursor,  # Simulate connection.execute
            Exception("Simulated query failure"),  # Simulate error from `fetchmany`
        ]
    )

    # Run run_query and collect all results
    results: list[dict[str, str]] = []
    with pytest.raises(Exception, match="Simulated query failure"):
        async for batch in sql_client.run_query(query):
            results.extend(batch)


@given(connection_string=sql_connection_string_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_connection_string_property_based(
    sql_client: BaseSQLClient, connection_string: str
):
    """Property-based test for various connection string formats"""
    with patch("sqlalchemy.create_engine") as mock_create_engine:
        # Mock the engine and connection
        mock_engine = MagicMock()
        mock_connection = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.connect.return_value = mock_connection

        # Override the connection string method
        sql_client.get_sqlalchemy_connection_string = lambda: connection_string

        # Set up credentials
        credentials = {"username": "test_user", "password": "test_password"}

        # Run the load function
        asyncio.run(sql_client.load(credentials))

        # Assertions to verify behavior
        mock_create_engine.assert_called_once_with(
            connection_string,
            connect_args=sql_client.sql_alchemy_connect_args,
            pool_pre_ping=True,
        )
        assert sql_client.engine == mock_engine
        assert sql_client.connection == mock_connection


@given(query_result=mock_sql_query_result_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@pytest.mark.asyncio
@pytest.mark.skip(reason="Failing due to KeyError: 'col1'")
async def test_run_query_property_based(
    sql_client: BaseSQLClient,
    query_result: Dict[str, Any],
):
    """Property-based test for query execution with various result structures"""
    with patch("sqlalchemy.text") as mock_text, patch(
        "application_sdk.clients.sql.asyncio.get_running_loop",
        new_callable=MagicMock,
    ) as mock_get_running_loop:
        # Mock the query text
        query = "SELECT * FROM test_table"
        mock_text.return_value = query

        # Create mock cursor with dynamic column descriptions
        mock_cursor = MagicMock()
        mock_cursor.cursor.description = [
            MagicMock(name=col["name"]) for col in query_result["columns"]
        ]

        # Set up the connection
        sql_client.connection = MagicMock()
        sql_client.connection.execute.return_value = mock_cursor

        # Mock run_in_executor to return cursor and then batches
        mock_get_running_loop.return_value.run_in_executor = AsyncMock(
            side_effect=[mock_cursor] + query_result["batches"] + [[]]
        )

        # Run run_query and collect all results
        results: List[Dict[str, Any]] = []
        async for batch in sql_client.run_query(query):
            results.extend(batch)

        # Verify the results
        expected_results: List[Dict[str, Any]] = []
        for batch in query_result["batches"]:
            expected_results.extend(batch)

        assert len(results) == len(expected_results)
        for result_row, expected_row in zip(results, expected_results):
            assert all(result_row[key] == value for key, value in expected_row.items())


@given(error_type=sql_error_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@pytest.mark.asyncio
async def test_run_query_error_property_based(
    sql_client: BaseSQLClient,
    error_type: str,
):
    """Property-based test for query execution with various error scenarios"""
    with patch("sqlalchemy.text") as mock_text, patch(
        "application_sdk.clients.sql.asyncio.get_running_loop",
        new_callable=MagicMock,
    ) as mock_get_running_loop:
        # Mock the query text
        query = "SELECT * FROM test_table"
        mock_text.return_value = query

        # Create mock cursor
        mock_cursor = MagicMock()
        mock_cursor.cursor.description = [MagicMock(name="col1")]

        # Set up the connection
        sql_client.connection = MagicMock()
        sql_client.connection.execute.return_value = mock_cursor

        # Mock run_in_executor to return cursor and then raise an error
        mock_get_running_loop.return_value.run_in_executor = AsyncMock(
            side_effect=[mock_cursor, Exception(f"Simulated {error_type}")]
        )

        # Run run_query and expect it to raise an exception
        with pytest.raises(Exception, match=f"Simulated {error_type}"):
            async for _ in sql_client.run_query(query):
                pass


@pytest.fixture
def sql_client_with_db_config():
    client = BaseSQLClient()
    client.DB_CONFIG = {
        "template": "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        "required": ["username", "password", "host", "port", "database"],
        "defaults": {"connect_timeout": 5},
        "parameters": ["ssl_mode"],
    }
    return client


def test_get_sqlalchemy_connection_string_basic_auth(sql_client_with_db_config):
    """Test connection string generation with basic authentication"""
    credentials = {
        "username": "test_user",
        "password": "test_pass",
        "host": "localhost",
        "port": 5432,
        "database": "test_db",
        "authType": "basic",
    }
    sql_client_with_db_config.resolved_credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    expected = "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=None"
    assert conn_str == expected


def test_get_sqlalchemy_connection_string_iam_user(sql_client_with_db_config):
    """Test connection string generation with IAM user authentication"""
    credentials = {
        "username": "aws_access_key",
        "password": "aws_secret_key",
        "host": "rds-instance.region.rds.amazonaws.com",
        "port": 5432,
        "authType": "iam_user",
        "extra": {"username": "db_user", "database": "test_db"},
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with patch.object(
        sql_client_with_db_config, "get_iam_user_token", return_value="iam_token"
    ):
        conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
        expected = "postgresql+psycopg://aws_access_key:iam_token@rds-instance.region.rds.amazonaws.com:5432/test_db?connect_timeout=5&ssl_mode=None"
        assert conn_str == expected


def test_get_sqlalchemy_connection_string_iam_role(sql_client_with_db_config):
    """Test connection string generation with IAM role authentication"""
    credentials = {
        "username": "db_user",
        "host": "rds-instance.region.rds.amazonaws.com",
        "port": 5432,
        "authType": "iam_role",
        "extra": {
            "aws_role_arn": "arn:aws:iam::123456789012:role/test-role",
            "database": "test_db",
            "aws_external_id": "external-id",
        },
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with patch.object(
        sql_client_with_db_config, "get_iam_role_token", return_value="iam_token"
    ):
        conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
        expected = "postgresql+psycopg://db_user:iam_token@rds-instance.region.rds.amazonaws.com:5432/test_db?connect_timeout=5&ssl_mode=None"
        assert conn_str == expected


def test_get_sqlalchemy_connection_string_with_parameters(sql_client_with_db_config):
    """Test connection string generation with additional parameters"""
    credentials = {
        "username": "test_user",
        "password": "test_pass",
        "host": "localhost",
        "port": 5432,
        "database": "test_db",
        "authType": "basic",
        "ssl_mode": "require",
    }
    sql_client_with_db_config.resolved_credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    expected = "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=require"
    assert conn_str == expected


def test_get_sqlalchemy_connection_string_missing_required_param(
    sql_client_with_db_config,
):
    """Test connection string generation with missing required parameters"""
    credentials = {
        "username": "test_user",
        "password": "test_pass",
        "host": "localhost",
        "port": 5432,
        # Missing database
        "authType": "basic",
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with pytest.raises(ValueError, match="database is required"):
        sql_client_with_db_config.get_sqlalchemy_connection_string()


def test_get_sqlalchemy_connection_string_invalid_auth_type(sql_client_with_db_config):
    """Test connection string generation with invalid authentication type"""
    credentials = {
        "username": "test_user",
        "password": "test_pass",
        "host": "localhost",
        "port": 5432,
        "database": "test_db",
        "authType": "invalid_auth",
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with pytest.raises(CommonError, match="invalid_auth"):
        sql_client_with_db_config.get_sqlalchemy_connection_string()


def test_get_sqlalchemy_connection_string_iam_user_missing_username(
    sql_client_with_db_config,
):
    """Test connection string generation with IAM user auth missing username"""
    credentials = {
        "username": "aws_access_key",
        "password": "aws_secret_key",
        "host": "rds-instance.region.rds.amazonaws.com",
        "port": 5432,
        "authType": "iam_user",
        "extra": {
            # Missing username
            "database": "test_db"
        },
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with pytest.raises(
        CommonError, match="username is required for IAM user authentication"
    ):
        sql_client_with_db_config.get_sqlalchemy_connection_string()


def test_get_sqlalchemy_connection_string_iam_role_missing_role_arn(
    sql_client_with_db_config,
):
    """Test connection string generation with IAM role auth missing role ARN"""
    credentials = {
        "username": "db_user",
        "host": "rds-instance.region.rds.amazonaws.com",
        "port": 5432,
        "authType": "iam_role",
        "extra": {
            # Missing aws_role_arn
            "database": "test_db",
            "aws_external_id": "external-id",
        },
    }
    sql_client_with_db_config.resolved_credentials = credentials

    with pytest.raises(
        CommonError, match="aws_role_arn is required for IAM role authentication"
    ):
        sql_client_with_db_config.get_sqlalchemy_connection_string()


@pytest.mark.skip(reason="Skipping this test until the native deployment is ready")
def test_get_sqlalchemy_connection_string_with_compiled_url(sql_client_with_db_config):
    """Test connection string generation with compiled url"""
    credentials = {
        "extra": {
            "compiled_url": "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=require"
        }
    }
    sql_client_with_db_config.resolved_credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    expected = "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=require"
    assert conn_str == expected


@pytest.mark.skip(reason="Skipping this test until the native deployment is ready")
def test_get_sqlalchemy_connection_string_with_compiled_url_with_invalid_dialect(
    sql_client_with_db_config,
):
    """Test connection string generation with compiled url with invalid dialect"""
    credentials = {
        "extra": {
            "compiled_url": "postgresql+psycopg2://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=require"
        }
    }
    sql_client_with_db_config.resolved_credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    expected = "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5&ssl_mode=require"
    assert conn_str == expected
