import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.clients._sql_errors import (
    DBConfigNotSetError,
    EngineNotInitializedError,
    EngineWrongTypeError,
    InvalidAuthTypeError,
    MissingRequiredFieldError,
    SqlClientAuthFailedError,
    UnsupportedCursorError,
)
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient, BaseSQLClient
from application_sdk.testing.hypothesis.strategies.clients.sql import (
    sql_credentials_strategy,
    sqlalchemy_connect_args_strategy,
)
from application_sdk.testing.hypothesis.strategies.sql_client import (
    mock_sql_query_result_strategy,
    sql_connection_string_strategy,
    sql_error_strategy,
)


@pytest.fixture
def sql_client():
    client = BaseSQLClient()
    client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"
    return client


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
    assert sql_client.DB_CONFIG is not None
    mock_create_engine.assert_called_once_with(
        sql_client.get_sqlalchemy_connection_string(),
        connect_args=sql_client.DB_CONFIG.connect_args,
        pool_pre_ping=True,
    )
    assert sql_client.engine == mock_engine
    # BaseSQLClient doesn't store persistent connection
    assert sql_client.connection is None


@patch("sqlalchemy.create_engine")
def test_load_respects_pool_pre_ping_override(
    mock_create_engine: Any, sql_client: BaseSQLClient
):
    assert sql_client.DB_CONFIG is not None
    sql_client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
        pool_pre_ping=False,
    )
    mock_engine = MagicMock()
    mock_create_engine.return_value = mock_engine

    asyncio.run(sql_client.load({"username": "test_user", "password": "test_password"}))

    mock_create_engine.assert_called_once_with(
        sql_client.get_sqlalchemy_connection_string(),
        connect_args=sql_client.DB_CONFIG.connect_args,
        pool_pre_ping=False,
    )
    assert sql_client.engine == mock_engine
    assert sql_client.connection is None


@pytest.mark.asyncio
@patch("application_sdk.clients.sql.asyncio.to_thread")
@patch("sqlalchemy.create_engine")
async def test_load_uses_asyncio_to_thread_for_ping(
    mock_create_engine: Any, mock_to_thread: AsyncMock, sql_client: BaseSQLClient
):
    """load() must delegate the blocking engine.connect() ping to asyncio.to_thread.

    The ping closes the event loop for the duration of the ODBC handshake; running
    it on the event loop directly starves Temporal's auto-heartbeat. Verify that
    asyncio.to_thread is called (not a direct engine.connect() on the loop).
    """
    from unittest.mock import AsyncMock as _AsyncMock

    mock_engine = MagicMock()
    mock_create_engine.return_value = mock_engine
    mock_to_thread.return_value = None  # AsyncMock already returns a coroutine
    mock_to_thread.__class__ = _AsyncMock

    # Replace with a true AsyncMock so await works
    actual_async_mock = _AsyncMock(return_value=None)
    with patch("application_sdk.clients.sql.asyncio.to_thread", actual_async_mock):
        await sql_client.load({"username": "u", "password": "p"})

    actual_async_mock.assert_called_once()
    # The callable passed to to_thread should trigger engine.connect when called
    ping_fn = actual_async_mock.call_args[0][0]
    ping_fn()
    mock_engine.connect.assert_called_once()


@given(
    credentials=sql_credentials_strategy, connect_args=sqlalchemy_connect_args_strategy
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_load_property_based(
    sql_client: BaseSQLClient,
    credentials: dict[str, Any],
    connect_args: dict[str, Any],
):
    """Property-based test for loading with various credentials and connection arguments"""
    with patch("sqlalchemy.create_engine") as mock_create_engine:
        # Mock the engine and connection
        mock_engine = MagicMock()
        mock_connection = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_engine.connect.return_value = mock_connection

        # Set the connection arguments in DB_CONFIG
        assert sql_client.DB_CONFIG is not None
        sql_client.DB_CONFIG.connect_args = connect_args

        # Run the load function
        asyncio.run(sql_client.load(credentials))

        # Assertions to verify behavior
        mock_create_engine.assert_called_once_with(
            sql_client.get_sqlalchemy_connection_string(),
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        assert sql_client.engine == mock_engine
        # BaseSQLClient doesn't store persistent connection
        assert sql_client.connection is None


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
    # Mock the engine to avoid "Engine is not initialized" error
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    sql_client.engine = mock_engine

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

    # Mock engine.connect() to return the connection
    mock_engine.connect.return_value = mock_connection
    mock_connection.execute.return_value = mock_cursor

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
    # Mock the engine to avoid "Engine is not initialized" error
    mock_engine = MagicMock()
    mock_connection = MagicMock()
    sql_client.engine = mock_engine

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

    # Mock engine.connect() to return the connection
    mock_engine.connect.return_value = mock_connection
    mock_connection.execute.return_value = mock_cursor

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
        assert sql_client.DB_CONFIG is not None
        mock_create_engine.assert_called_once_with(
            connection_string,
            connect_args=sql_client.DB_CONFIG.connect_args,
            pool_pre_ping=True,
        )
        assert sql_client.engine == mock_engine
        # BaseSQLClient doesn't store persistent connection
        assert sql_client.connection is None


@given(query_result=mock_sql_query_result_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@pytest.mark.asyncio
@pytest.mark.skip(reason="Failing due to KeyError: 'col1'")
async def test_run_query_property_based(
    sql_client: BaseSQLClient,
    query_result: dict[str, Any],
):
    """Property-based test for query execution with various result structures"""
    with (
        patch("sqlalchemy.text") as mock_text,
        patch(
            "application_sdk.clients.sql.asyncio.get_running_loop",
            new_callable=MagicMock,
        ) as mock_get_running_loop,
    ):
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
        results: list[dict[str, Any]] = []
        async for batch in sql_client.run_query(query):
            results.extend(batch)

        # Verify the results
        expected_results: list[dict[str, Any]] = []
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
    with (
        patch("sqlalchemy.text") as mock_text,
        patch(
            "application_sdk.clients.sql.asyncio.get_running_loop",
            new_callable=MagicMock,
        ) as mock_get_running_loop,
    ):
        # Mock the engine to avoid "Engine is not initialized" error
        mock_engine = MagicMock()
        mock_connection = MagicMock()
        sql_client.engine = mock_engine

        # Mock the query text
        query = "SELECT * FROM test_table"
        mock_text.return_value = query

        # Create mock cursor
        mock_cursor = MagicMock()
        mock_cursor.cursor.description = [MagicMock(name="col1")]

        # Mock engine.connect() to return the connection
        mock_engine.connect.return_value = mock_connection
        mock_connection.execute.return_value = mock_cursor

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
    client.DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        defaults={"connect_timeout": 5},
        parameters=["ssl_mode"],
    )
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
    sql_client_with_db_config.credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    expected = "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db?connect_timeout=5"
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
    sql_client_with_db_config.credentials = credentials

    with patch.object(
        sql_client_with_db_config, "get_iam_user_token", return_value="iam_token"
    ):
        conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
        expected = "postgresql+psycopg://aws_access_key:iam_token@rds-instance.region.rds.amazonaws.com:5432/test_db?connect_timeout=5"
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
    sql_client_with_db_config.credentials = credentials

    with patch.object(
        sql_client_with_db_config, "get_iam_role_token", return_value="iam_token"
    ):
        conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
        expected = "postgresql+psycopg://db_user:iam_token@rds-instance.region.rds.amazonaws.com:5432/test_db?connect_timeout=5"
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
    sql_client_with_db_config.credentials = credentials

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
    sql_client_with_db_config.credentials = credentials

    with pytest.raises(MissingRequiredFieldError, match="database is required"):
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
    sql_client_with_db_config.credentials = credentials

    with pytest.raises(InvalidAuthTypeError, match="invalid_auth"):
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
    sql_client_with_db_config.credentials = credentials

    with pytest.raises(
        MissingRequiredFieldError,
        match="username is required for IAM user authentication",
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
    sql_client_with_db_config.credentials = credentials

    with pytest.raises(
        MissingRequiredFieldError,
        match="aws_role_arn is required for IAM role authentication",
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


def test_get_sqlalchemy_connection_string_omits_none_parameters(
    sql_client_with_db_config,
):
    """Parameters missing from credentials should not appear as key=None in the connection string."""
    credentials = {
        "username": "test_user",
        "password": "test_pass",
        "host": "localhost",
        "port": 5432,
        "database": "test_db",
        "authType": "basic",
        # ssl_mode intentionally omitted — should NOT appear as ssl_mode=None
    }
    sql_client_with_db_config.credentials = credentials

    conn_str = sql_client_with_db_config.get_sqlalchemy_connection_string()
    assert "None" not in conn_str
    assert "ssl_mode" not in conn_str


# -----------------------------------------------------------------------------
# BLDX-1129 — additional coverage for BaseSQLClient + AsyncBaseSQLClient.
#
# Goal: exercise every function-local (inline) import in
# `application_sdk/clients/sql.py` so a future rename / removal of one of those
# symbols fails at unit-test time instead of at runtime in production.
# -----------------------------------------------------------------------------


# ---------- BaseSQLClient.load — error / edge paths ----------


@pytest.mark.asyncio
async def test_load_raises_when_db_config_missing():
    """load() must validate DB_CONFIG before attempting any sqlalchemy import."""
    client = BaseSQLClient()
    client.DB_CONFIG = None
    with pytest.raises(DBConfigNotSetError, match="DB_CONFIG is not configured"):
        await client.load({"username": "u", "password": "p"})


@pytest.mark.asyncio
@patch("sqlalchemy.create_engine")
async def test_load_wraps_engine_failure_as_client_error(
    mock_create_engine: Any, sql_client: BaseSQLClient
):
    """A sqlalchemy create_engine failure must be wrapped as SqlClientAuthFailedError and
    the (partially constructed) engine disposed so connections do not leak."""
    mock_engine = MagicMock()
    mock_create_engine.return_value = mock_engine

    # Force the connect-test inside asyncio.to_thread to fail
    actual_async_mock = AsyncMock(side_effect=RuntimeError("auth handshake failed"))
    with patch("application_sdk.clients.sql.asyncio.to_thread", actual_async_mock):
        with pytest.raises(SqlClientAuthFailedError):
            await sql_client.load({"username": "u", "password": "p"})

    # Engine must be disposed and reset to None so a retry can rebuild it.
    mock_engine.dispose.assert_called_once()
    assert sql_client.engine is None


# ---------- BaseSQLClient.close ----------


@pytest.mark.asyncio
async def test_close_disposes_engine_and_clears_state(sql_client: BaseSQLClient):
    """close() must dispose the underlying engine and null out engine/connection."""
    mock_engine = MagicMock()
    sql_client.engine = mock_engine
    sql_client.connection = MagicMock()

    await sql_client.close()

    mock_engine.dispose.assert_called_once()
    assert sql_client.engine is None
    assert sql_client.connection is None


@pytest.mark.asyncio
async def test_close_is_idempotent_when_engine_none(sql_client: BaseSQLClient):
    """close() must be safe to call when engine was never initialized."""
    sql_client.engine = None
    sql_client.connection = None
    await sql_client.close()  # must not raise
    assert sql_client.engine is None


# ---------- IAM token paths ----------


@patch("application_sdk.clients.sql.generate_aws_rds_token_with_iam_user")
def test_get_iam_user_token_happy_path(mock_gen: MagicMock, sql_client: BaseSQLClient):
    """get_iam_user_token threads creds through to generate_aws_rds_token_with_iam_user."""
    mock_gen.return_value = "iam-user-token"
    sql_client.credentials = {
        "username": "AKIA",
        "password": "secret",
        "host": "rds.local",
        "port": 5432,
        "region": "us-east-1",
        "extra": {"username": "db_user", "database": "db1"},
    }
    token = sql_client.get_iam_user_token()
    assert token == "iam-user-token"
    mock_gen.assert_called_once_with(
        aws_access_key_id="AKIA",
        aws_secret_access_key="secret",
        host="rds.local",
        user="db_user",
        port=5432,
        region="us-east-1",
    )


def test_get_iam_user_token_missing_database(sql_client: BaseSQLClient):
    sql_client.credentials = {"username": "k", "extra": {"username": "u"}}
    with pytest.raises(MissingRequiredFieldError, match="database is required"):
        sql_client.get_iam_user_token()


@patch("application_sdk.clients.sql.generate_aws_rds_token_with_iam_role")
def test_get_iam_role_token_happy_path(mock_gen: MagicMock, sql_client: BaseSQLClient):
    """get_iam_role_token threads creds through to generate_aws_rds_token_with_iam_role."""
    mock_gen.return_value = "iam-role-token"
    sql_client.credentials = {
        "username": "db_user",
        "host": "rds.local",
        "port": 5432,
        "region": "us-east-1",
        "extra": {
            "aws_role_arn": "arn:aws:iam::1:role/r",
            "database": "db1",
            "aws_external_id": "ext",
        },
    }
    token = sql_client.get_iam_role_token()
    assert token == "iam-role-token"
    assert mock_gen.call_args.kwargs["role_arn"] == "arn:aws:iam::1:role/r"
    assert mock_gen.call_args.kwargs["external_id"] == "ext"


def test_get_iam_role_token_missing_role_arn(sql_client: BaseSQLClient):
    sql_client.credentials = {"extra": {"database": "db1"}}
    with pytest.raises(MissingRequiredFieldError, match="aws_role_arn is required"):
        sql_client.get_iam_role_token()


def test_get_iam_role_token_missing_database(sql_client: BaseSQLClient):
    sql_client.credentials = {"extra": {"aws_role_arn": "arn"}}
    with pytest.raises(MissingRequiredFieldError, match="database is required"):
        sql_client.get_iam_role_token()


# ---------- run_query guards ----------


@pytest.mark.asyncio
async def test_run_query_raises_when_engine_not_initialized(sql_client: BaseSQLClient):
    sql_client.engine = None
    with pytest.raises(EngineNotInitializedError, match="Engine is not initialized"):
        async for _ in sql_client.run_query("SELECT 1"):
            pass


@pytest.mark.asyncio
@patch("sqlalchemy.text")
@patch(
    "application_sdk.clients.sql.asyncio.get_running_loop",
    new_callable=MagicMock,
)
async def test_run_query_raises_when_cursor_unsupported(
    mock_loop: MagicMock, mock_text: Any, sql_client: BaseSQLClient
):
    """If the dialect returns a result without a DBAPI cursor, run_query must
    raise rather than silently produce zero rows."""
    mock_engine = MagicMock()
    sql_client.engine = mock_engine
    mock_engine.connect.return_value = MagicMock()

    # cursor evaluates falsy => "Cursor is not supported"
    bad_result = MagicMock()
    bad_result.cursor = None
    mock_loop.return_value.run_in_executor = AsyncMock(return_value=bad_result)

    with pytest.raises(UnsupportedCursorError, match="Cursor is not supported"):
        async for _ in sql_client.run_query("SELECT 1"):
            pass


# ---------- get_supported_sqlalchemy_url ----------


# `get_supported_sqlalchemy_url` was deleted as dead code via PR #1600
# (BLDX-1183) — it had no production callers and its only call site was
# already commented out. Tests for it removed here.


def test_get_sqlalchemy_connection_string_requires_db_config():
    client = BaseSQLClient()
    client.DB_CONFIG = None
    with pytest.raises(DBConfigNotSetError, match="DB_CONFIG is not configured"):
        client.get_sqlalchemy_connection_string()


# ---------- _execute_query_daft (drives `import daft`) ----------


def test_execute_query_daft_requires_engine(sql_client: BaseSQLClient):
    sql_client.engine = None
    with pytest.raises(EngineNotInitializedError, match="Engine is not initialized"):
        sql_client._execute_query_daft("SELECT 1", chunksize=None)


def test_execute_query_daft_with_string_engine(sql_client: BaseSQLClient):
    """When engine is a connection string, daft.read_sql is called with the string directly."""
    sql_client.engine = "postgresql://u:p@h/db"
    fake_daft = MagicMock()
    fake_daft.read_sql.return_value = "df-string-engine"
    with patch.dict("sys.modules", {"daft": fake_daft}):
        result = sql_client._execute_query_daft("SELECT 1", chunksize=10)
    assert result == "df-string-engine"
    fake_daft.read_sql.assert_called_once_with(
        "SELECT 1", "postgresql://u:p@h/db", infer_schema_length=10
    )


def test_execute_query_daft_with_engine_object(sql_client: BaseSQLClient):
    """When engine is a SQLAlchemy engine, daft.read_sql is called with engine.connect."""
    fake_engine = MagicMock()
    sql_client.engine = fake_engine
    fake_daft = MagicMock()
    fake_daft.read_sql.return_value = "df-engine-obj"
    with patch.dict("sys.modules", {"daft": fake_daft}):
        result = sql_client._execute_query_daft("SELECT 1", chunksize=None)
    assert result == "df-engine-obj"
    fake_daft.read_sql.assert_called_once_with(
        "SELECT 1", fake_engine.connect, infer_schema_length=None
    )


# ---------- _execute_query / _execute_pandas_query (drives pandas imports) ----------


def test_execute_query_requires_engine(sql_client: BaseSQLClient):
    sql_client.engine = None
    with pytest.raises(EngineNotInitializedError, match="Engine is not initialized"):
        sql_client._execute_query("SELECT 1", chunksize=None)


def test_execute_pandas_query_with_sqlalchemy_available(sql_client: BaseSQLClient):
    """When sqlalchemy is import-resolvable, _execute_pandas_query must call
    pd.read_sql_query with text(query) and the connection object directly."""
    fake_pandas = MagicMock()
    fake_pandas.read_sql_query.return_value = "df"
    fake_compat = MagicMock()
    fake_compat.import_optional_dependency.return_value = MagicMock()  # truthy
    fake_pandas.compat = MagicMock()
    fake_pandas.compat._optional = fake_compat

    fake_text = MagicMock(side_effect=lambda q: f"text({q})")
    fake_sqlalchemy = MagicMock()
    fake_sqlalchemy.text = fake_text

    conn = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "pandas": fake_pandas,
            "pandas.compat": fake_pandas.compat,
            "pandas.compat._optional": fake_compat,
            "sqlalchemy": fake_sqlalchemy,
        },
    ):
        out = sql_client._execute_pandas_query(conn, "SELECT 1", chunksize=10)
    assert out == "df"
    fake_pandas.read_sql_query.assert_called_once_with(
        "text(SELECT 1)", conn, chunksize=10
    )


def test_execute_pandas_query_falls_back_to_dbapi(sql_client: BaseSQLClient):
    """When sqlalchemy is NOT import-resolvable for pandas, fall back to the
    underlying DBAPI connection (Redshift connector path)."""
    fake_pandas = MagicMock()
    fake_pandas.read_sql_query.return_value = "df-dbapi"
    fake_compat = MagicMock()
    fake_compat.import_optional_dependency.return_value = None  # falsy
    fake_pandas.compat = MagicMock()
    fake_pandas.compat._optional = fake_compat

    fake_sqlalchemy = MagicMock()
    fake_sqlalchemy.text = MagicMock(side_effect=lambda q: q)

    conn = MagicMock()
    conn.connection = "dbapi-conn"
    with patch.dict(
        "sys.modules",
        {
            "pandas": fake_pandas,
            "pandas.compat": fake_pandas.compat,
            "pandas.compat._optional": fake_compat,
            "sqlalchemy": fake_sqlalchemy,
        },
    ):
        out = sql_client._execute_pandas_query(conn, "SELECT 1", chunksize=None)
    assert out == "df-dbapi"
    fake_pandas.read_sql_query.assert_called_once_with(
        "SELECT 1", "dbapi-conn", chunksize=None
    )


def test_read_sql_query_uses_session_connection(sql_client: BaseSQLClient):
    """_read_sql_query delegates to _execute_pandas_query with session.connection()."""
    fake_session = MagicMock()
    fake_session.connection.return_value = "conn-from-session"
    with patch.object(
        sql_client, "_execute_pandas_query", return_value="df"
    ) as mock_exec:
        out = sql_client._read_sql_query(fake_session, "SELECT 1", chunksize=5)
    assert out == "df"
    mock_exec.assert_called_once_with("conn-from-session", "SELECT 1", chunksize=5)


# ---------- _execute_async_read_operation / get_results / get_batched_results ----------


@pytest.mark.asyncio
async def test_execute_async_read_operation_rejects_string_engine(
    sql_client: BaseSQLClient,
):
    sql_client.engine = "postgresql://u:p@h/db"  # type: ignore[assignment]
    with pytest.raises(
        EngineWrongTypeError, match="Engine should be an SQLAlchemy engine"
    ):
        await sql_client._execute_async_read_operation("SELECT 1", chunksize=None)


@pytest.mark.asyncio
async def test_get_results_returns_dataframe(sql_client: BaseSQLClient):
    """get_results returns the pandas DataFrame produced by the read path."""
    import pandas as pd

    fake_df = pd.DataFrame({"a": [1, 2]})
    sql_client.engine = MagicMock()  # non-AsyncEngine, non-string

    # Patch the inner read path so we don't need to mock all of pandas
    with patch.object(
        sql_client,
        "_execute_async_read_operation",
        new=AsyncMock(return_value=fake_df),
    ):
        out = await sql_client.get_results("SELECT 1")
    assert out is fake_df


@pytest.mark.asyncio
async def test_get_results_rejects_non_dataframe(sql_client: BaseSQLClient):
    """If the read path returns something that isn't a DataFrame, get_results
    must surface the error (wrapped via rewrap)."""
    sql_client.engine = MagicMock()
    with patch.object(
        sql_client,
        "_execute_async_read_operation",
        new=AsyncMock(return_value=iter([])),  # not a DataFrame
    ):
        with pytest.raises(Exception, match="Error reading data"):
            await sql_client.get_results("SELECT 1")


@pytest.mark.asyncio
async def test_get_batched_results_wraps_errors_via_rewrap(sql_client: BaseSQLClient):
    """Any failure in the read path must be re-raised through rewrap with the
    'Error reading batched data(pandas) from SQL' prefix."""
    sql_client.engine = MagicMock()
    with (
        patch.object(
            sql_client,
            "_execute_async_read_operation",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        pytest.raises(Exception, match="Error reading batched data"),
    ):
        await sql_client.get_batched_results("SELECT 1")


@pytest.mark.asyncio
async def test_get_batched_results_returns_iterator(sql_client: BaseSQLClient):
    sql_client.engine = MagicMock()
    fake_iter = iter([])
    with patch.object(
        sql_client,
        "_execute_async_read_operation",
        new=AsyncMock(return_value=fake_iter),
    ):
        out = await sql_client.get_batched_results("SELECT 1")
    assert out is fake_iter


@pytest.mark.asyncio
async def test_execute_async_read_operation_thread_pool_branch(
    sql_client: BaseSQLClient,
):
    """When self.engine is a *sync* SQLAlchemy engine (not AsyncEngine),
    _execute_async_read_operation should fall back to a ThreadPoolExecutor and
    drive _execute_query. This also exercises the AsyncEngine/AsyncSession import."""
    sql_client.engine = MagicMock()  # plain MagicMock — not an AsyncEngine instance

    with patch.object(sql_client, "_execute_query", return_value="df-sync") as mock_eq:
        out = await sql_client._execute_async_read_operation("SELECT 1", chunksize=42)

    assert out == "df-sync"
    mock_eq.assert_called_once_with("SELECT 1", 42)


@pytest.mark.asyncio
async def test_execute_async_read_operation_async_session_branch(
    sql_client: BaseSQLClient,
):
    """When self.engine IS an AsyncEngine, _execute_async_read_operation must
    build an async session via sqlalchemy.orm.sessionmaker and call
    session.run_sync(self._read_sql_query, ...). This drives the inline imports
    at lines 496-505."""
    from sqlalchemy.ext.asyncio import AsyncEngine

    fake_engine = MagicMock(spec=AsyncEngine)
    sql_client.engine = fake_engine  # type: ignore[assignment]

    fake_session = AsyncMock()
    fake_session.run_sync = AsyncMock(return_value="df-async")

    class _SessionCtx:
        async def __aenter__(self_inner):
            return fake_session

        async def __aexit__(self_inner, exc_type, exc, tb):
            return None

    fake_sessionmaker = MagicMock(return_value=lambda: _SessionCtx())

    with patch("sqlalchemy.orm.sessionmaker", fake_sessionmaker):
        out = await sql_client._execute_async_read_operation("SELECT 1", chunksize=None)

    assert out == "df-async"
    fake_session.run_sync.assert_awaited_once()
    # First arg must be the bound _read_sql_query method
    assert fake_session.run_sync.call_args.args[0] == sql_client._read_sql_query


# ---------- AsyncBaseSQLClient — load failure + close + run_query guard ----------


@pytest.fixture
def async_sql_client_local():
    client = AsyncBaseSQLClient()
    client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"
    return client


@pytest.mark.asyncio
async def test_async_load_raises_when_db_config_missing():
    client = AsyncBaseSQLClient()
    client.DB_CONFIG = None
    with pytest.raises(DBConfigNotSetError, match="DB_CONFIG is not configured"):
        await client.load({"username": "u", "password": "p"})


@pytest.mark.asyncio
@patch("sqlalchemy.ext.asyncio.create_async_engine")
async def test_async_load_disposes_engine_on_failure(
    mock_create: MagicMock, async_sql_client_local: AsyncBaseSQLClient
):
    """Async load() must dispose the engine and re-raise as SqlClientAuthFailedError
    (consistent with sync load() — locked in by PR #1602 / BLDX-1180)."""
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    class _FailingCtx:
        async def __aenter__(self_inner):
            raise RuntimeError("auth failed")

        async def __aexit__(self_inner, exc_type, exc, tb):
            return None

    mock_engine.connect = MagicMock(return_value=_FailingCtx())
    mock_create.return_value = mock_engine

    with pytest.raises(SqlClientAuthFailedError):
        await async_sql_client_local.load({"username": "u", "password": "p"})

    mock_engine.dispose.assert_awaited_once()
    assert async_sql_client_local.engine is None


@pytest.mark.asyncio
async def test_async_close_disposes_engine(
    async_sql_client_local: AsyncBaseSQLClient,
):
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()
    async_sql_client_local.engine = mock_engine
    await async_sql_client_local.close()
    mock_engine.dispose.assert_awaited_once()
    assert async_sql_client_local.engine is None
    assert async_sql_client_local.connection is None


@pytest.mark.asyncio
async def test_async_close_idempotent_when_engine_none(
    async_sql_client_local: AsyncBaseSQLClient,
):
    async_sql_client_local.engine = None  # type: ignore[assignment]
    await async_sql_client_local.close()  # must not raise


@pytest.mark.asyncio
async def test_async_run_query_requires_engine(
    async_sql_client_local: AsyncBaseSQLClient,
):
    async_sql_client_local.engine = None  # type: ignore[assignment]
    with pytest.raises(EngineNotInitializedError, match="Engine is not initialized"):
        async for _ in async_sql_client_local.run_query("SELECT 1"):
            pass
