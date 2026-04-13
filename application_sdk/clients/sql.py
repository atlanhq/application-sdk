"""
SQL client implementation for database connections.

This module provides SQL client classes for both synchronous and asynchronous
database operations, supporting batch processing and server-side cursors.
"""

import asyncio
import concurrent
from concurrent.futures import ThreadPoolExecutor
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Optional,
    Union,
    cast,
)
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from application_sdk.clients import BaseClient
from application_sdk.clients.models import DatabaseConfig
from application_sdk.common.error_codes import ClientError
from application_sdk.common.exc_utils import rewrap
from application_sdk.common.utils import parse_credentials_extra
from application_sdk.constants import USE_SERVER_SIDE_CURSOR
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft
    import pandas as pd
    from sqlalchemy.orm import Session

    from application_sdk.credentials.types import Credential


class BaseSQLClient(BaseClient):
    """SQL client for database operations.

    This class provides functionality for connecting to and querying SQL databases,
    with support for batch processing and server-side cursors.

    Attributes:
        connection: Database connection instance.
        engine: SQLAlchemy engine instance.
        credentials (Dict[str, Any]): Database credentials.
        resolved_credentials (Dict[str, Any]): Resolved credentials after reading from secret manager.
        use_server_side_cursor (bool): Whether to use server-side cursors.
    """

    connection = None
    engine = None
    resolved_credentials: Dict[str, Any] = {}
    use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR
    DB_CONFIG: Optional[DatabaseConfig] = None

    def __init__(
        self,
        use_server_side_cursor: bool = USE_SERVER_SIDE_CURSOR,
        credentials: Dict[str, Any] = {},
        chunk_size: int = 5000,
    ):
        """
        Initialize the SQL client.

        Args:
            use_server_side_cursor (bool, optional): Whether to use server-side cursors.
                Defaults to USE_SERVER_SIDE_CURSOR.
            credentials (Dict[str, Any], optional): Database credentials. Defaults to {}.
        """
        super().__init__(credentials=credentials)
        self.use_server_side_cursor = use_server_side_cursor
        self.chunk_size = chunk_size

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load raw dict credentials and prepare engine for lazy connections.

        Builds the connection string from ``DB_CONFIG.template`` by resolving
        required parameters from *credentials* (and its ``extra`` sub-dict).
        The ``password`` parameter is automatically URL-encoded.

        Prefer ``load_with_credential()`` for new connectors — it uses the
        ``AUTH_STRATEGIES`` pattern and typed credentials.

        Args:
            credentials: Database connection credentials dict.

        Raises:
            ClientError: If credentials are invalid or engine creation fails.
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        self.credentials = credentials
        extra = parse_credentials_extra(credentials)

        # Build connection string from template + credentials
        param_values: Dict[str, Any] = {}
        for param in self.DB_CONFIG.required:
            if param == "password":
                param_values[param] = quote_plus(str(credentials.get("password") or ""))
            else:
                value = credentials.get(param) or extra.get(param)
                if value is None:
                    raise ValueError(f"{param} is required")
                param_values[param] = value

        conn_str = self.DB_CONFIG.template.format(**param_values)
        if self.DB_CONFIG.defaults:
            conn_str = self.add_url_params(conn_str, self.DB_CONFIG.defaults)
        if self.DB_CONFIG.parameters:
            dynamic = {
                k: credentials.get(k) or extra.get(k) for k in self.DB_CONFIG.parameters
            }
            conn_str = self.add_url_params(conn_str, dynamic)

        try:
            from sqlalchemy import create_engine

            self.engine = create_engine(
                conn_str,
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=True,
            )

            # Test connection briefly to validate credentials.
            # Wrapped in asyncio.to_thread because SQLAlchemy's synchronous
            # engine.connect() blocks the event loop — critical for Temporal
            # activities where blocking starves the auto-heartbeat.
            _engine = self.engine

            def _ping() -> None:
                with _engine.connect() as _:
                    pass

            await asyncio.to_thread(_ping)
            self.connection = None

        except Exception as e:
            logger.error("Error loading SQL client", exc_info=True)
            if self.engine:
                self.engine.dispose()
                self.engine = None
            raise ClientError(f"{ClientError.SQL_CLIENT_AUTH_ERROR}: {str(e)}")

    async def load_with_credential(
        self,
        credential: "Credential",
        **connection_params: str,
    ) -> None:
        """Load a typed credential using the AUTH_STRATEGIES registry.

        This is the strategy-based alternative to ``load(dict)``.  It looks
        up the strategy for ``type(credential)`` in ``AUTH_STRATEGIES``,
        builds the connection string and ``connect_args`` from the strategy,
        and creates the SQLAlchemy engine.

        Args:
            credential: A typed Credential instance (e.g. BasicCredential).
            **connection_params: Non-auth URL template params such as
                ``username``, ``host``, ``port``, ``database``.  These are
                merged with the strategy's ``build_url_params()`` output
                to fill ``DB_CONFIG.template``.

        Raises:
            ClientError: If no strategy is registered for the credential type,
                or if engine creation / connection test fails.
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        strategy = self._resolve_strategy(credential)
        conn_str = self._build_url(
            self.DB_CONFIG.template,
            strategy,
            credential,
            self.DB_CONFIG.defaults,
            **connection_params,
        )
        connect_args = {
            **self.DB_CONFIG.connect_args,
            **strategy.build_connect_args(credential),
        }

        try:
            from sqlalchemy import create_engine

            self.engine = create_engine(
                conn_str,
                connect_args=connect_args,
                pool_pre_ping=True,
            )

            _engine = self.engine

            def _ping() -> None:
                with _engine.connect() as _:
                    pass

            await asyncio.to_thread(_ping)
            self.connection = None

        except Exception as e:
            logger.error("Error loading SQL client", exc_info=True)
            if self.engine:
                self.engine.dispose()
                self.engine = None
            raise ClientError(f"{ClientError.SQL_CLIENT_AUTH_ERROR}: {str(e)}")

    async def close(self) -> None:
        """Close the database connection."""
        if self.engine:
            self.engine.dispose()
            self.engine = None
        self.connection = None  # Should already be None, but ensure cleanup

    def add_connection_params(
        self, connection_string: str, source_connection_params: Dict[str, Any]
    ) -> str:
        """Add additional connection parameters to a SQLAlchemy connection string.

        Backward-compatible alias for
        :meth:`~BaseClient.add_url_params`.
        """
        return self.add_url_params(connection_string, source_connection_params)

    async def run_query(self, query: str, batch_size: int = 100000):
        """Execute a SQL query and return results in batches using lazy connections.

        This method creates a connection on-demand, executes the query in batches,
        and automatically closes the connection when done. This prevents memory
        leaks from persistent connections.

        Args:
            query (str): SQL query to execute.
            batch_size (int, optional): Number of records to fetch in each batch.
                Defaults to 100000.

        Yields:
            List[Dict[str, Any]]: Batches of query results, where each result is
                a dictionary mapping column names to values.

        Raises:
            ValueError: If engine is not initialized.
            Exception: If query execution fails.
        """
        if not self.engine:
            raise ValueError("Engine is not initialized. Call load() first.")

        loop = asyncio.get_running_loop()
        logger.info("Running query: %s", query)

        # Use context manager for automatic connection cleanup
        with self.engine.connect() as connection:
            if self.use_server_side_cursor:
                connection = connection.execution_options(yield_per=batch_size)

            with ThreadPoolExecutor() as pool:
                from sqlalchemy import text

                cursor = await loop.run_in_executor(
                    pool, connection.execute, text(query)
                )
                if not cursor or not cursor.cursor:
                    raise ValueError("Cursor is not supported")
                column_names: List[str] = [
                    description.name.lower()
                    for description in cursor.cursor.description
                ]

                while True:
                    rows = await loop.run_in_executor(
                        pool, cursor.fetchmany, batch_size
                    )
                    if not rows:
                        break

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            # Connection automatically closed by context manager

        logger.info("Query execution completed")

    def _execute_pandas_query(
        self, conn, query, chunksize: Optional[int]
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Helper function to execute SQL query using pandas.
           The function is responsible for using import_optional_dependency method of the pandas library to import sqlalchemy
           This function helps pandas in determining weather to use the sqlalchemy connection object and constructs like text()
           or use the underlying database connection object. This has been done to make sure connectors like the Redshift connector,
           which do not support the sqlalchemy connection object, can be made compatible with the application-sdk.

        Args:
            conn: Database connection object.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        import pandas as pd
        from pandas.compat._optional import import_optional_dependency
        from sqlalchemy import text

        if import_optional_dependency("sqlalchemy", errors="ignore"):
            return pd.read_sql_query(text(query), conn, chunksize=chunksize)
        else:
            dbapi_conn = getattr(conn, "connection", None)
            return pd.read_sql_query(query, dbapi_conn, chunksize=chunksize)

    def _read_sql_query(
        self, session: "Session", query: str, chunksize: Optional[int]
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided session.

        Args:
            session: SQLAlchemy session for database operations.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        conn = session.connection()
        return self._execute_pandas_query(conn, query, chunksize=chunksize)

    def _execute_query_daft(
        self, query: str, chunksize: Optional[int]
    ) -> Union["daft.DataFrame", Iterator["daft.DataFrame"]]:
        """Execute SQL query using the provided engine and daft.

        Returns:
            Union["daft.DataFrame", Iterator["daft.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        # Daft uses ConnectorX to read data from SQL by default for supported connectors
        # If a connection string is passed, it will use ConnectorX to read data
        # For unsupported connectors and if directly engine is passed, it will use SQLAlchemy
        import daft

        if not self.engine:
            raise ValueError("Engine is not initialized. Call load() first.")

        if isinstance(self.engine, str):
            return daft.read_sql(query, self.engine, infer_schema_length=chunksize)
        return daft.read_sql(query, self.engine.connect, infer_schema_length=chunksize)

    def _execute_query(
        self, query: str, chunksize: Optional[int]
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Execute SQL query using the provided engine and pandas.

        Returns:
            Union["pd.DataFrame", Iterator["pd.DataFrame"]]: Query results as DataFrame
                or iterator of DataFrames if chunked.
        """
        if not self.engine:
            raise ValueError("Engine is not initialized. Call load() first.")

        with self.engine.connect() as conn:
            return self._execute_pandas_query(conn, query, chunksize)

    async def _execute_async_read_operation(
        self, query: str, chunksize: Optional[int]
    ) -> Union["pd.DataFrame", Iterator["pd.DataFrame"]]:
        """Helper to execute async read operation with either async session or thread executor."""
        if isinstance(self.engine, str):
            raise ValueError("Engine should be an SQLAlchemy engine object")

        from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

        async_session = None
        if self.engine and isinstance(self.engine, AsyncEngine):
            from sqlalchemy.orm import sessionmaker

            async_session = sessionmaker(
                self.engine, expire_on_commit=False, class_=AsyncSession
            )

        if async_session:
            async with async_session() as session:
                return await session.run_sync(
                    self._read_sql_query, query, chunksize=chunksize
                )
        else:
            # Run the blocking operation in a thread pool
            with concurrent.futures.ThreadPoolExecutor() as executor:
                return await asyncio.get_event_loop().run_in_executor(
                    executor, self._execute_query, query, chunksize
                )

    async def get_batched_results(
        self,
        query: str,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:  # type: ignore
        """Get query results as batched pandas DataFrames asynchronously.

        Returns:
            AsyncIterator["pd.DataFrame"]: Async iterator yielding batches of query results.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        try:
            # We cast to Iterator because passing chunk_size guarantees an Iterator return
            result = await self._execute_async_read_operation(query, self.chunk_size)
            return cast(Iterator["pd.DataFrame"], result)
        except Exception as e:
            raise rewrap(e, "Error reading batched data(pandas) from SQL") from e

    async def get_results(self, query: str) -> "pd.DataFrame":
        """Get all query results as a single pandas DataFrame asynchronously.

        Returns:
            pd.DataFrame: Query results as a DataFrame.

        Raises:
            ValueError: If engine is a string instead of SQLAlchemy engine.
            Exception: If there's an error executing the query.
        """
        try:
            result = await self._execute_async_read_operation(query, None)
            import pandas as pd

            if isinstance(result, pd.DataFrame):
                return result
            raise Exception("Unable to get pandas dataframe from SQL query results")

        except Exception as e:
            raise rewrap(e, "Error reading data(pandas) from SQL") from e


class AsyncBaseSQLClient(BaseSQLClient):
    """Asynchronous SQL client for database operations.

    This class extends BaseSQLClient to provide asynchronous database operations,
    with support for batch processing and server-side cursors. It uses SQLAlchemy's
    async engine and connection interfaces for non-blocking database operations.

    Attributes:
        connection (AsyncConnection): Async database connection instance.
        engine (AsyncEngine): Async SQLAlchemy engine instance.
        credentials (Dict[str, Any]): Database credentials.
        use_server_side_cursor (bool): Whether to use server-side cursors.
    """

    connection: "AsyncConnection"
    engine: "AsyncEngine"

    async def load(self, credentials: Dict[str, Any]) -> None:
        """Load raw dict credentials and prepare async engine for lazy connections.

        Builds the connection string from ``DB_CONFIG.template`` by resolving
        required parameters from *credentials*.  Prefer
        ``load_with_credential()`` for new connectors.

        Args:
            credentials: Database connection credentials dict.

        Raises:
            ValueError: If credentials are invalid or engine creation fails.
        """
        self.credentials = credentials
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        extra = parse_credentials_extra(credentials)

        param_values: Dict[str, Any] = {}
        for param in self.DB_CONFIG.required:
            if param == "password":
                param_values[param] = quote_plus(str(credentials.get("password") or ""))
            else:
                value = credentials.get(param) or extra.get(param)
                if value is None:
                    raise ValueError(f"{param} is required")
                param_values[param] = value

        conn_str = self.DB_CONFIG.template.format(**param_values)
        if self.DB_CONFIG.defaults:
            conn_str = self.add_url_params(conn_str, self.DB_CONFIG.defaults)
        if self.DB_CONFIG.parameters:
            dynamic = {
                k: credentials.get(k) or extra.get(k) for k in self.DB_CONFIG.parameters
            }
            conn_str = self.add_url_params(conn_str, dynamic)

        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            self.engine = create_async_engine(
                conn_str,
                connect_args=self.DB_CONFIG.connect_args,
                pool_pre_ping=True,
            )
            if not self.engine:
                raise ValueError("Failed to create async engine")

            async with self.engine.connect() as _:
                pass

            self.connection = None

        except Exception as e:
            logger.error("Error establishing database connection", exc_info=True)
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise ValueError(str(e))

    async def load_with_credential(
        self,
        credential: "Credential",
        **connection_params: str,
    ) -> None:
        """Load a typed credential using the AUTH_STRATEGIES registry (async).

        Async counterpart of ``BaseSQLClient.load_with_credential()``.

        Args:
            credential: A typed Credential instance.
            **connection_params: Non-auth URL template params such as
                ``username``, ``host``, ``port``, ``database``.

        Raises:
            ClientError: If no strategy or engine creation fails.
        """
        if not self.DB_CONFIG:
            raise ValueError("DB_CONFIG is not configured for this SQL client.")

        strategy = self._resolve_strategy(credential)
        conn_str = self._build_url(
            self.DB_CONFIG.template,
            strategy,
            credential,
            self.DB_CONFIG.defaults,
            **connection_params,
        )
        connect_args = {
            **self.DB_CONFIG.connect_args,
            **strategy.build_connect_args(credential),
        }

        try:
            from sqlalchemy.ext.asyncio import create_async_engine

            self.engine = create_async_engine(
                conn_str,
                connect_args=connect_args,
                pool_pre_ping=True,
            )
            if not self.engine:
                raise ValueError("Failed to create async engine")

            async with self.engine.connect() as _:
                pass

            self.connection = None

        except Exception as e:
            logger.error("Error establishing database connection", exc_info=True)
            if self.engine:
                await self.engine.dispose()
                self.engine = None
            raise ClientError(f"{ClientError.SQL_CLIENT_AUTH_ERROR}: {str(e)}")

    async def close(self) -> None:
        """Close the async database connection and dispose of the engine."""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
        self.connection = None

    async def run_query(self, query: str, batch_size: int = 100000):
        """Execute a SQL query asynchronously and return results in batches using lazy connections.

        This method creates an async connection on-demand, executes the query in batches,
        and automatically closes the connection when done. This prevents memory leaks
        from persistent connections.

        Args:
            query (str): SQL query to execute.
            batch_size (int, optional): Number of records to fetch in each batch.
                Defaults to 100000.

        Yields:
            List[Dict[str, Any]]: Batches of query results, where each result is
                a dictionary mapping column names to values.

        Raises:
            ValueError: If engine is not initialized.
            Exception: If query execution fails.
        """
        if not self.engine:
            raise ValueError("Engine is not initialized. Call load() first.")

        logger.info("Running query: %s", query)
        use_server_side_cursor = self.use_server_side_cursor

        # Use async context manager for automatic connection cleanup
        async with self.engine.connect() as connection:
            try:
                from sqlalchemy import text

                if use_server_side_cursor:
                    connection = await connection.execution_options(
                        yield_per=batch_size
                    )

                result = (
                    await connection.stream(text(query))
                    if use_server_side_cursor
                    else await connection.execute(text(query))
                )

                column_names = list(result.keys())

                while True:
                    rows = (
                        await result.fetchmany(batch_size)
                        if use_server_side_cursor
                        else result.cursor.fetchmany(batch_size)
                        if result.cursor
                        else None
                    )
                    if not rows:
                        break
                    yield [dict(zip(column_names, row)) for row in rows]

            except Exception as e:
                raise rewrap(e, "Error executing query") from e
            # Async connection automatically closed by context manager

        logger.info("Query execution completed")
