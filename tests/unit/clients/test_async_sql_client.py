from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient
from application_sdk.clients.sql_errors import SqlPandasResultError


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
def mock_async_engine_with_connection():
    """Common fixture for mocking async engine with proper context manager."""
    mock_engine = AsyncMock()
    mock_connection = AsyncMock()

    # Create a second connection for execution_options return
    mock_connection_with_options = AsyncMock()
    mock_connection_with_options.stream = AsyncMock()
    mock_connection_with_options.execute = AsyncMock()
    mock_connection_with_options.execution_options = AsyncMock(
        return_value=mock_connection_with_options
    )

    # Set up the original connection
    mock_connection.stream = AsyncMock()
    mock_connection.execute = AsyncMock()
    mock_connection.execution_options = AsyncMock(
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


@patch("sqlalchemy.ext.asyncio.create_async_engine")
@pytest.mark.asyncio
async def test_load_respects_pool_pre_ping_override(
    create_async_engine: Any,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    assert async_sql_client.DB_CONFIG is not None
    async_sql_client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
        pool_pre_ping=False,
    )
    mock_engine, _, _ = mock_async_engine_with_connection
    create_async_engine.return_value = mock_engine

    await async_sql_client.load({"username": "test_user", "password": "test_password"})

    create_async_engine.assert_called_once_with(
        async_sql_client.get_sqlalchemy_connection_string(),
        connect_args=async_sql_client.DB_CONFIG.connect_args,
        pool_pre_ping=False,
    )
    assert async_sql_client.engine == mock_engine
    assert async_sql_client.connection is None


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
    mock_connection.execute.assert_called_once_with(query, None)
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
    mock_connection.execution_options.assert_awaited_once_with(yield_per=2)
    mock_connection_with_options.stream.assert_called_once_with(query, None)
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
    with pytest.raises(SqlPandasResultError) as exc_info:
        async for batch in async_sql_client.run_query(query):
            results.extend(batch)
    assert exc_info.value.message == "Error executing SQL query"
    assert isinstance(exc_info.value.cause, Exception)


# ---------- colon-safe query execution (CONNECT-260) ----------


@pytest.mark.asyncio
@patch("sqlalchemy.text", side_effect=lambda q: q)  # type: ignore
async def test_run_query_escapes_colons_client_side(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    """The client-side execute path escapes literal colons before text()."""
    mock_engine, mock_connection, _ = mock_async_engine_with_connection
    async_sql_client.engine = mock_engine

    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["a"]
    mock_result.cursor = MagicMock()
    mock_result.cursor.fetchmany = MagicMock(side_effect=[[("v",)], []])
    mock_connection.execute = AsyncMock(return_value=mock_result)
    async_sql_client.use_server_side_cursor = False

    async for _ in async_sql_client.run_query("RLIKE '^(?:cdl)'"):
        pass

    mock_text.assert_called_once_with("RLIKE '^(?\\:cdl)'")
    mock_connection.execute.assert_called_once_with("RLIKE '^(?\\:cdl)'", None)


@pytest.mark.asyncio
@patch("sqlalchemy.text", side_effect=lambda q: q)  # type: ignore
async def test_run_query_escapes_colons_server_side(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    """The server-side stream path escapes literal colons before text()."""
    mock_engine, mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    async_sql_client.engine = mock_engine

    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["a"]
    mock_result.fetchmany = AsyncMock(side_effect=[[("v",)], []])
    mock_connection_with_options.stream = AsyncMock(return_value=mock_result)
    async_sql_client.use_server_side_cursor = True

    async for _ in async_sql_client.run_query("RLIKE '^(?:cdl)'"):
        pass

    mock_text.assert_called_once_with("RLIKE '^(?\\:cdl)'")
    mock_connection_with_options.stream.assert_called_once_with(
        "RLIKE '^(?\\:cdl)'", None
    )


# ---------- retryability of the unclassified-exception wrap (CONAT-747) ----------


@pytest.mark.asyncio
@patch("sqlalchemy.text", side_effect=lambda q: q)  # type: ignore
async def test_run_query_wrap_is_retryable_server_side(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    """An unclassified driver exception must NOT be marked non-retryable.

    The activity that calls run_query declares its own Temporal retry policy
    (e.g. maximum_attempts=3). Wrapping every escaping exception as a
    non-retryable InternalError made that policy dead code, so a transient
    source-side failure became a hard failure on the first attempt.
    """
    mock_engine, _mock_connection, mock_connection_with_options = (
        mock_async_engine_with_connection
    )
    async_sql_client.engine = mock_engine

    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1"]
    # Mimics psycopg.errors.InternalError_: a transient concurrent-DDL race.
    mock_result.fetchmany = AsyncMock(
        side_effect=[Exception("could not open relation with OID 328245688")]
    )
    mock_connection_with_options.stream = AsyncMock(return_value=mock_result)
    async_sql_client.use_server_side_cursor = True

    with pytest.raises(SqlPandasResultError) as exc_info:
        async for _ in async_sql_client.run_query("SELECT * FROM t"):
            pass

    assert exc_info.value.effective_retryable is True
    assert exc_info.value.to_failure_details().retryable is True


@pytest.mark.asyncio
@patch("sqlalchemy.text", side_effect=lambda q: q)  # type: ignore
async def test_run_query_wrap_is_retryable_client_side(
    mock_text: MagicMock,
    async_sql_client: AsyncBaseSQLClient,
    mock_async_engine_with_connection,
):
    """The client-side cursor path carries the same retryability."""
    mock_engine, mock_connection, _ = mock_async_engine_with_connection
    async_sql_client.engine = mock_engine

    mock_result = MagicMock()
    mock_result.keys.side_effect = lambda: ["col1"]
    mock_result.cursor = MagicMock()
    mock_result.cursor.fetchmany = MagicMock(side_effect=Exception("connection lost"))
    mock_connection.execute = AsyncMock(return_value=mock_result)
    async_sql_client.use_server_side_cursor = False

    with pytest.raises(SqlPandasResultError) as exc_info:
        async for _ in async_sql_client.run_query("SELECT * FROM t"):
            pass

    assert exc_info.value.effective_retryable is True


_IAM_URL = "postgresql+psycopg://u:{token}@127.0.0.1:1/db"


class _StopConnect(Exception):
    """Raised by the recorder so no connect ever reaches a socket."""


def _record_connect_params(sync_engine: Any) -> list[dict[str, Any]]:
    from sqlalchemy import event

    seen: list[dict[str, Any]] = []

    def _record(
        dialect: object, conn_rec: object, cargs: object, cparams: dict[str, Any]
    ) -> None:
        seen.append(dict(cparams))
        raise _StopConnect

    event.listen(sync_engine, "do_connect", _record)
    return seen


@pytest.mark.asyncio
async def test_an_async_refresh_builds_the_token_off_the_event_loop() -> None:
    """An async engine connects on the loop; the STS-bound build must not run there."""
    pytest.importorskip("psycopg")
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    from application_sdk.clients.sql import _FreshIamToken
    from tests.unit.loop_probe import LoopProbe

    probe = LoopProbe()
    now = [1000.0]

    def _build() -> str:
        probe.block()  # stands in for the STS AssumeRole
        return _IAM_URL.format(token="token-1")

    engine = create_async_engine(_IAM_URL.format(token="token-0"))
    event.listen(
        engine.sync_engine,
        "do_connect",
        _FreshIamToken(
            _build,
            _IAM_URL.format(token="token-0"),
            now[0],
            on_loop=True,
            clock=lambda: now[0],
        ),
    )
    seen = _record_connect_params(engine.sync_engine)
    now[0] += 11 * 60

    async def _connect() -> None:
        with pytest.raises(_StopConnect):
            async with engine.connect():
                pass

    await probe.run(_connect)

    probe.assert_loop_stayed_free("refreshing the RDS IAM token on an async connect")
    assert seen[0]["password"] == "token-1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_installs_the_on_loop_refresh_on_the_sync_engine() -> None:
    pytest.importorskip("psycopg")
    from sqlalchemy.ext.asyncio import create_async_engine

    from application_sdk.clients.sql import _FreshIamToken

    class _IamAsyncClient(AsyncBaseSQLClient):
        DB_CONFIG = DatabaseConfig(template="unused://{host}", required=["host"])

        def get_sqlalchemy_connection_string(self) -> str:
            return _IAM_URL.format(token="token-0")

    real = create_async_engine(_IAM_URL.format(token="token-0"))

    class _Ping:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: object) -> None:
            return None

    async_engine = MagicMock()
    async_engine.sync_engine = real.sync_engine
    async_engine.connect = MagicMock(return_value=_Ping())
    async_engine.dispose = AsyncMock()

    client = _IamAsyncClient()
    with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=async_engine):
        await client.load({"host": "h", "authType": "iam_role"})

    listeners = [
        fn
        for fn in real.sync_engine.dialect.dispatch.do_connect
        if isinstance(fn, _FreshIamToken)
    ]
    assert len(listeners) == 1
    assert listeners[0]._on_loop is True
    await real.dispose()
